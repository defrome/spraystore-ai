"""Каталог товаров из Яндекс KIT: синхронизация -> индекс Chroma (коллекция `products`) -> поиск.

Всё read-only (см. kit_client.py). Без KIT_API_TOKEN каталог выключен: функции поиска возвращают [],
синхронизация бросает KitDisabled, сервис работает как раньше.

Ручной запуск (из корня репо):
    python service/catalog.py --sync          # загрузить товары и обновить индекс
    python service/catalog.py --stats         # сколько товаров в индексе
    python service/catalog.py --show "краскопульт для лака" [--max-price 10000]
"""
import argparse
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

import chromadb

import kit_client
from kit_client import KitClient, KitDisabled, KitError

HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_PATH = os.getenv("CHROMA_PATH", os.path.join(HERE, "chroma_spraystore"))
PRODUCTS_COLLECTION = "products"     # отдельно от `spraystore` (статьи), чтобы не ломать RAG
SYNC_INTERVAL_MIN = float(os.getenv("CATALOG_SYNC_INTERVAL_MIN", "60"))
LIVE_TTL_SEC = float(os.getenv("CATALOG_LIVE_TTL_SEC", "180"))
MIN_SCORE = float(os.getenv("CATALOG_MIN_SCORE", "0.70"))   # ниже — товар не считаем релевантным
STORE_URL = os.getenv("CATALOG_STORE_URL", "").rstrip("/")  # переопределить домен витрины (по умолчанию b2c_url из KIT)


# ---------------------------------------------------------------- модель

@dataclass
class Product:
    id: str
    name: str
    sku: str = ""
    brand: str = ""
    category: str = ""
    price: float | None = None          # итоговая цена, ₽
    old_price: float | None = None      # цена до скидки, если отличается
    stock: int | None = None            # доступный остаток (quantity - reserved) по всем складам
    in_stock: bool = False
    url: str = ""
    image: str = ""
    characteristics: dict[str, str] = field(default_factory=dict)   # {"Диаметр сопла": "1,4 мм", ...}
    description: str = ""
    status: str = ""
    live: bool | None = None            # True — цена/наличие сверены с KIT только что; False — из индекса; None — не определено
    score: float = 0.0

    def to_card(self) -> dict:
        """Формат карточки для фронта (/api/chat -> cards)."""
        card = {"id": self.id, "name": self.name, "price": self.price, "url": self.url, "in_stock": self.in_stock}
        if self.image:
            card["image"] = self.image
        return card


class Dicts:
    """Справочники KIT, нужные нормализатору: id -> значение."""

    def __init__(self, chars=None, categories=None, product_cats=None, files=None, base_url=""):
        self.chars: dict[str, tuple[str, str]] = chars or {}        # characteristic_id -> (title, unit)
        self.categories: dict[str, str] = categories or {}          # category_id -> "Родитель / Дочерняя"
        self.product_cats: dict[str, list[str]] = product_cats or {}  # product_id -> [category_id]
        self.files: dict[str, str] = files or {}                    # file_id -> url
        self.base_url: str = base_url


# ---------------------------------------------------------------- нормализатор
# ЕДИНСТВЕННОЕ место, знающее формат ответа KIT для товара. Поля взяты из публичной OpenAPI-спеки
# (yandex.ru/dev/kit/ru/openapi/kit-swagger.openapi.json). Допущения, которые стоит проверить
# на первом реальном ответе, помечены «ДОПУЩЕНИЕ».

_TAG_RE = re.compile(r"<[^>]+>")


def _num(v) -> float | None:
    """Цены в KIT приходят строками-decimal ("12990.00")."""
    if v in (None, ""):
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def _clean(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()
    return text[:limit]


def _join_url(base: str, rel: str) -> str:
    if not rel:
        return ""
    if rel.startswith(("http://", "https://")):
        return rel
    return (base.rstrip("/") + "/" + rel.lstrip("/")) if base else ""


def normalize_variant(raw: dict, ctx: Dicts | None = None) -> Product | None:
    """Сырой Variant из KIT -> Product. Терпим к отсутствию любых полей, кроме id."""
    ctx = ctx or Dicts()
    vid = str(raw.get("id") or "").strip()
    if not vid:
        return None

    # цена. ДОПУЩЕНИЕ: «итоговая» = pricing.final_price; если её нет — самая «скидочная» из имеющихся
    pr = raw.get("pricing") or {}
    price = next((p for p in (_num(pr.get(k)) for k in
                              ("final_price", "promotion_price", "manual_discount_price", "price")) if p is not None), None)
    base_price = _num(pr.get("price"))
    old_price = base_price if (base_price is not None and price is not None and base_price > price) else None

    # остаток. ДОПУЩЕНИЕ: доступно = сумма (quantity - reserved) по складам; нет складов -> 0
    stocks = raw.get("stocks")
    stock = None if stocks is None else sum(max((s.get("quantity") or 0) - (s.get("reserved") or 0), 0) for s in stocks)
    status = str(raw.get("status") or "")
    # ДОПУЩЕНИЕ: покупать можно только PUBLISHED; если статуса нет в ответе — не блокируем
    in_stock = bool(stock) and status in ("PUBLISHED", "")

    # характеристики: {название: "значение(я) + единица"}
    chars: dict[str, str] = {}
    for c in raw.get("characteristics") or []:
        title, unit = ctx.chars.get(str(c.get("characteristic_id")), (str(c.get("characteristic_id") or ""), ""))
        vals = c.get("values") or ([c["value"]] if c.get("value") else [])
        vals = [str(v) for v in vals if str(v).strip()]
        if title and vals:
            chars[title] = ", ".join(vals) + (f" {unit}" if unit else "")

    # категория: у товара её нет, она на продукте (product_id -> category_ids)
    cat_ids = ctx.product_cats.get(str(raw.get("product_id") or ""), [])
    category = next((ctx.categories[c] for c in cat_ids if c in ctx.categories), "")

    # картинка: media[type=IMAGE] по display_sequence -> image_id -> url из справочника файлов
    image = ""
    media = sorted((m for m in raw.get("media") or [] if m.get("type") == "IMAGE"),
                   key=lambda m: m.get("display_sequence") or 0)
    for m in media:
        image = ctx.files.get(str(m.get("image_id") or ""), "")
        if image:
            break

    # ДОПУЩЕНИЕ: ссылка = b2c_url магазина + relative_link_url
    url = _join_url(STORE_URL or ctx.base_url, raw.get("relative_link_url") or "")

    return Product(
        id=vid, name=str(raw.get("name") or raw.get("seo_h1") or "").strip(), sku=str(raw.get("sku") or ""),
        brand=str(raw.get("brand") or ""), category=category, price=price, old_price=old_price,
        stock=stock, in_stock=in_stock, url=url, image=image, characteristics=chars,
        description=_clean(raw.get("description") or ""), status=status,
    )


def apply_live(p: Product, raw: dict) -> Product:
    """Накатывает на товар из индекса свежие цену/наличие из GET /v1/variants/{id}."""
    fresh = normalize_variant(raw, Dicts())
    if fresh is None:
        return p
    p.price, p.old_price, p.stock, p.status = fresh.price, fresh.old_price, fresh.stock, fresh.status
    p.in_stock = fresh.in_stock
    p.live = True
    return p


# ---------------------------------------------------------------- документ для эмбеддинга

def product_document(p: Product) -> str:
    """Человекочитаемый текст товара — по нему ищем семантически."""
    parts = [p.name]
    if p.brand:
        parts.append(f"Бренд: {p.brand}")
    if p.category:
        parts.append(f"Категория: {p.category}")
    if p.characteristics:
        parts.append("Характеристики: " + "; ".join(f"{k}: {v}" for k, v in list(p.characteristics.items())[:20]))
    if p.description:
        parts.append(p.description[:400])
    return ". ".join(parts)


# ---------------------------------------------------------------- индекс (Chroma)

_embedder = None
_chroma_clients: dict[str, "chromadb.api.ClientAPI"] = {}
_chroma_lock = threading.Lock()


def set_embedder(embedder):
    """Подмена эмбеддера (тесты). Нужен объект с encode(list[str]|str, normalize_embeddings=True)."""
    global _embedder
    _embedder = embedder


def _get_embedder():
    global _embedder
    if _embedder is None:
        import main as rag    # тяжёлый импорт (модель e5) — только когда реально нужен
        _embedder = rag.embedder
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    vecs = _get_embedder().encode(texts, normalize_embeddings=True)
    return [list(map(float, v)) for v in vecs]


def get_collection(path: str | None = None):
    path = path or CHROMA_PATH
    with _chroma_lock:
        if path not in _chroma_clients:
            _chroma_clients[path] = chromadb.PersistentClient(path=path)
        return _chroma_clients[path].get_or_create_collection(
            name=PRODUCTS_COLLECTION, metadata={"hnsw:space": "cosine"})


def _meta(p: Product, doc_hash: str) -> dict:
    # price в metadata только если известна — тогда фильтр $lte автоматически отсекает товары без цены
    m = {"id": p.id, "category": p.category, "in_stock": int(p.in_stock), "doc_hash": doc_hash,
         "product": json.dumps(asdict(p), ensure_ascii=False)}
    if p.price is not None:
        m["price"] = float(p.price)
    return m


def _from_meta(meta: dict, score: float) -> Product:
    d = json.loads(meta["product"])
    d["score"] = score
    return Product(**{k: v for k, v in d.items() if k in Product.__dataclass_fields__})


def index_products(products: list[Product], col=None) -> dict:
    """Обновляет индекс: эмбеддим только новые/изменившиеся документы, у остальных обновляем
    metadata (цена/наличие), исчезнувшие товары удаляем."""
    col = col or get_collection()
    existing = col.get(include=["metadatas"])
    old_hash = {i: (m or {}).get("doc_hash") for i, m in zip(existing["ids"], existing["metadatas"])}

    to_embed, same = [], []
    for p in products:
        doc = "passage: " + product_document(p)
        h = hashlib.md5(doc.encode()).hexdigest()
        (same if old_hash.get(p.id) == h else to_embed).append((p, doc, h))

    if to_embed:
        vecs = _embed([d for _, d, _ in to_embed])
        col.upsert(ids=[p.id for p, _, _ in to_embed], documents=[d for _, d, _ in to_embed],
                   embeddings=vecs, metadatas=[_meta(p, h) for p, _, h in to_embed])
    if same:
        col.update(ids=[p.id for p, _, _ in same], metadatas=[_meta(p, h) for p, _, h in same])
    gone = [i for i in old_hash if i not in {p.id for p in products}]
    if gone:
        col.delete(ids=gone)
    return {"total": len(products), "embedded": len(to_embed), "removed": len(gone)}


# ---------------------------------------------------------------- синхронизация

_status = {"last_ok": None, "last_error": None, "count": None}
_sync_lock = threading.Lock()


def load_dicts(client: KitClient) -> Dicts:
    """Справочники для нормализатора. Каждый обход — отдельные страницы по 100 (лимит 3 rps учтён в клиенте)."""
    d = Dicts()
    try:
        d.base_url = (client.get("/v1/store").get("b2c_url") or "")
    except KitError as e:
        print(f"[catalog] /v1/store не прочитан ({e}); ссылки на товары будут пустыми")
    cats = {c["id"]: c for c in client.iter_pages("/v1/categories", "categories", status="ACTIVE")}

    def path(cid, depth=0):
        c = cats.get(cid)
        if not c or depth > 6:
            return ""
        parent = path(c.get("parent_id"), depth + 1) if c.get("parent_id") else ""
        return f"{parent} / {c.get('title', '')}" if parent else c.get("title", "")

    d.categories = {cid: path(cid) for cid in cats}
    d.chars = {c["id"]: (c.get("title", ""), c.get("unit", ""))
               for c in client.iter_pages("/v1/characteristics", "characteristics", status="ACTIVE")}
    d.product_cats = {p["id"]: p.get("category_ids") or [] for p in client.iter_pages("/v1/products", "products")}
    # картинки: справочник файлов (id -> публичный url), на 100 штук один запрос
    d.files = {f["id"]: f.get("url", "") for f in client.iter_pages("/v1/files", "files") if f.get("type") == "IMAGE"}
    return d


def sync_catalog(client: KitClient | None = None, col=None) -> dict:
    """Полная синхронизация. Любая ошибка вылетает ДО правки индекса — прошлый индекс остаётся.
    Обходим /v1/variants (а не фиды): в фидах нет характеристик/остатков в удобном виде.
    status=PUBLISHED: скрытые и архивные товары не рекомендуем."""
    client = client or kit_client.get_client()
    with _sync_lock:
        ctx = load_dicts(client)
        products = []
        for raw in client.iter_pages("/v1/variants", "variants", status=["PUBLISHED"]):
            p = normalize_variant(raw, ctx)
            if p and p.name:
                products.append(p)
        stats = index_products(products, col)
        _status.update(last_ok=time.time(), last_error=None, count=len(products))
        print(f"[catalog] синхронизировано: {stats}")
        return stats


def safe_sync() -> bool:
    """Обёртка для фона: ошибки логируем (с trace_id), индекс не трогаем."""
    try:
        sync_catalog()
        return True
    except KitDisabled:
        return False
    except Exception as e:
        _status["last_error"] = f"{type(e).__name__}: {e}"
        print(f"[catalog] синхронизация не удалась, остаётся прошлый индекс: {e}")
        return False


_stop = threading.Event()
_thread: threading.Thread | None = None


def start_background_sync(interval_min: float | None = None) -> bool:
    """Фоновый поток: синк сразу после старта и далее раз в interval_min. Без токена — не стартует."""
    global _thread
    if not kit_client.is_enabled():
        print("[catalog] KIT_API_TOKEN не задан — каталог выключен")
        return False
    if _thread and _thread.is_alive():
        return True
    interval = (interval_min if interval_min is not None else SYNC_INTERVAL_MIN) * 60
    _stop.clear()

    def loop():
        while not _stop.is_set():
            safe_sync()
            _stop.wait(interval)

    _thread = threading.Thread(target=loop, name="catalog-sync", daemon=True)
    _thread.start()
    return True


def stop_background_sync():
    _stop.set()


def status() -> dict:
    return dict(_status, enabled=kit_client.is_enabled())


def count(col=None) -> int:
    try:
        return (col or get_collection()).count()
    except Exception:
        return 0


# ---------------------------------------------------------------- живые данные

_live_cache: dict[str, tuple[float, dict]] = {}
_live_lock = threading.Lock()


def _fetch_live(pid: str, client: KitClient | None = None, now=time.time) -> dict:
    with _live_lock:
        hit = _live_cache.get(pid)
        if hit and now() - hit[0] < LIVE_TTL_SEC:
            return hit[1]
    raw = (client or kit_client.get_client()).get(f"/v1/variants/{pid}")
    with _live_lock:
        _live_cache[pid] = (now(), raw)
    return raw


def refresh_live(products: list[Product], client: KitClient | None = None) -> list[Product]:
    """Сверяет цену/наличие с KIT точечно (с TTL-кэшем). Ошибка — оставляем данные индекса, live=False."""
    for p in products:
        try:
            apply_live(p, _fetch_live(p.id, client))
        except KitDisabled:
            p.live = False
        except Exception as e:
            p.live = False
            print(f"[catalog] живые данные для {p.id} не получены: {e}")
    return products


# ---------------------------------------------------------------- поиск

def search_products(query: str, max_price: float | None = None, category: str | None = None,
                    in_stock_only: bool = True, k: int = 5, live: bool = True, col=None) -> list[Product]:
    """Семантический поиск по индексу + точечная сверка цены/наличия.
    Возвращает [] если каталог пуст/выключен или ничего релевантного."""
    try:
        col = col or get_collection()
        if col.count() == 0 or not query.strip():
            return []
        conds = []
        if in_stock_only:
            conds.append({"in_stock": 1})
        if max_price is not None:
            conds.append({"price": {"$lte": float(max_price)}})
        where = conds[0] if len(conds) == 1 else ({"$and": conds} if conds else None)
        # категорию фильтруем в Python (подстрока без регистра), поэтому берём с запасом
        n = min(col.count(), k * (8 if category else 3))
        q_vec = _embed(["query: " + query])[0]
        res = col.query(query_embeddings=[q_vec], n_results=n, where=where, include=["metadatas", "distances"])
    except Exception as e:
        print(f"[catalog] поиск не удался: {e}")
        return []

    found = []
    for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
        score = 1 - dist
        if score < MIN_SCORE:
            continue
        p = _from_meta(meta, score)
        if category and category.casefold() not in p.category.casefold():
            continue
        found.append(p)
        if len(found) >= k:
            break
    if live and found:
        refresh_live(found)
        # после сверки товар мог закончиться / подорожать выше лимита
        found = [p for p in found
                 if not (in_stock_only and p.live and not p.in_stock)
                 and not (max_price is not None and p.live and p.price is not None and p.price > max_price)]
    return found


def get_product(product_id: str, live: bool = True, col=None) -> Product | None:
    try:
        res = (col or get_collection()).get(ids=[product_id], include=["metadatas"])
    except Exception:
        return None
    if not res["ids"]:
        return None
    p = _from_meta(res["metadatas"][0], 1.0)
    if live:
        refresh_live([p])
    return p


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Каталог KIT -> индекс товаров")
    ap.add_argument("--sync", action="store_true", help="загрузить товары из KIT и обновить индекс")
    ap.add_argument("--stats", action="store_true", help="сколько товаров в индексе")
    ap.add_argument("--show", metavar="ЗАПРОС", help="поиск по индексу (без сверки с KIT)")
    ap.add_argument("--max-price", type=float)
    a = ap.parse_args()
    if a.sync:
        try:
            sync_catalog()
        except KitDisabled as e:
            raise SystemExit(f"{e}. Положите токен в service/.env (см. README).")
        except KitError as e:
            raise SystemExit(f"Ошибка KIT: {e}")
    if a.stats:
        print(f"товаров в индексе: {count()}")
    if a.show:
        for p in search_products(a.show, max_price=a.max_price, in_stock_only=False, live=False):
            print(f"[{p.score:.2f}] {p.name} | {p.price} ₽ | остаток {p.stock} | {p.category} | {p.url}")
    if not (a.sync or a.stats or a.show):
        ap.print_help()
