import httpx
import pytest

import catalog
from catalog import Dicts, Product, normalize_variant
from conftest import load_fixture
from kit_client import KitClient, _Throttle

FX = load_fixture("kit_catalog.json")


def ctx():
    return Dicts(
        chars={c["id"]: (c["title"], c["unit"]) for c in FX["characteristics"]},
        categories={"cat-1": "Краскопульты", "cat-2": "Краскопульты / Электрические", "cat-3": "Компрессоры"},
        product_cats={p["id"]: p["category_ids"] for p in FX["products"]},
        files={f["id"]: f["url"] for f in FX["files"] if f["type"] == "IMAGE"},
        base_url=FX["store"]["b2c_url"])


# ---------- нормализатор

def test_normalize_full():
    p = normalize_variant(FX["variants"][0], ctx())
    assert p.id == "var-1" and p.sku == "W-590" and p.brand == "Wagner"
    assert p.price == 9990.50 and p.old_price == 12990.0
    assert p.stock == 4 and p.in_stock                       # (5-2)+1
    assert p.category == "Краскопульты / Электрические"
    assert p.characteristics == {"Диаметр сопла": "1.3 мм", "Тип": "HVLP, электрический"}
    assert p.image == "https://cdn.example.com/a.jpg"        # по display_sequence, пропуская неизвестный файл
    assert p.url == "https://demo.kit.yandex.ru/products/flexio-590"
    assert "<" not in p.description and "тонкий факел" in p.description


def test_normalize_stock_and_price_fallbacks():
    p2 = normalize_variant(FX["variants"][1], ctx())
    assert p2.stock == 0 and not p2.in_stock and p2.price == 7490.0 and p2.old_price is None
    assert p2.url == "https://demo.kit.yandex.ru/products/tm-800"
    p3 = normalize_variant(FX["variants"][2], ctx())
    assert p3.price == 22000.0 and p3.old_price == 25000.0   # promotion_price вместо final_price


def test_normalize_tolerates_missing_fields():
    p = normalize_variant({"id": "x"})
    assert p.id == "x" and p.price is None and p.stock is None and not p.in_stock and p.url == ""
    assert normalize_variant({"name": "без id"}) is None
    p = normalize_variant({"id": "y", "pricing": {"final_price": "мусор"}, "stocks": [{}], "media": None, "characteristics": None})
    assert p.price is None and p.stock == 0


def test_hidden_not_in_stock():
    raw = dict(FX["variants"][0], status="HIDDEN")
    assert not normalize_variant(raw, ctx()).in_stock


# ---------- индекс + поиск (временный Chroma, стаб-эмбеддер)

def build(col):
    prods = [normalize_variant(v, ctx()) for v in FX["variants"]]
    return catalog.index_products(prods, col), prods


def test_index_and_search_filters(col, monkeypatch):
    stats, _ = build(col)
    assert stats["total"] == 3 and col.count() == 3
    # живую сверку отключаем — тут проверяем только индекс
    r = catalog.search_products("краскопульт для лака", in_stock_only=False, live=False, col=col)
    assert r[0].id == "var-1"
    # in_stock_only отсекает var-2 (остаток 0)
    r = catalog.search_products("краскопульт для забора", live=False, col=col)
    assert "var-2" not in [p.id for p in r]
    # max_price
    r = catalog.search_products("краскопульт", max_price=8000, in_stock_only=False, live=False, col=col)
    assert [p.id for p in r] == ["var-2"]
    # category — подстрока без регистра
    r = catalog.search_products("оборудование", category="компрессор", in_stock_only=False, live=False, col=col)
    assert [p.id for p in r] == ["var-3"]
    assert catalog.search_products("   ", col=col) == []


def test_reindex_skips_unchanged_and_removes_gone(col, monkeypatch):
    _, prods = build(col)
    calls = []
    orig = catalog._embed
    monkeypatch.setattr(catalog, "_embed", lambda texts: (calls.append(len(texts)), orig(texts))[1])
    prods[0].price = 1.0                 # цена не влияет на документ -> без пере-эмбеддинга
    s = catalog.index_products(prods[:2], col)
    assert s == {"total": 2, "embedded": 0, "removed": 1} and calls == []
    assert col.count() == 2
    got = catalog.get_product("var-1", live=False, col=col)
    assert got.price == 1.0


def test_search_empty_index_and_errors(col):
    assert catalog.search_products("лак", col=col) == []


# ---------- живые данные

def kit_client_for(handler):
    return KitClient(token="t", transport=httpx.MockTransport(handler), throttle=_Throttle(1000), sleep=lambda s: None)


def test_live_refresh_cache_and_fallback(monkeypatch):
    monkeypatch.setattr(catalog, "_live_cache", {})
    calls = []

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"id": "var-1", "status": "PUBLISHED", "pricing": {"final_price": "8000"},
                                         "stocks": [{"quantity": 2, "reserved": 0}]})

    c = kit_client_for(h)
    p = normalize_variant(FX["variants"][0], ctx())
    catalog.refresh_live([p], c)
    assert p.live is True and p.price == 8000.0 and p.stock == 2
    p2 = normalize_variant(FX["variants"][0], ctx())
    catalog.refresh_live([p2], c)               # из TTL-кэша
    assert len(calls) == 1 and p2.price == 8000.0

    monkeypatch.setattr(catalog, "_live_cache", {})
    bad = kit_client_for(lambda r: httpx.Response(404, json={"code": "NOT_FOUND", "message": "no", "trace_id": "z"}))
    p3 = normalize_variant(FX["variants"][0], ctx())
    catalog.refresh_live([p3], bad)             # ошибка — данные индекса сохраняются
    assert p3.live is False and p3.price == 9990.50


def test_live_refresh_without_token_marks_unchecked(monkeypatch):
    monkeypatch.setattr(catalog, "_live_cache", {})
    monkeypatch.setattr(catalog.kit_client, "_client", None)
    p = normalize_variant(FX["variants"][0], ctx())
    catalog.refresh_live([p])
    assert p.live is False


def test_search_drops_sold_out_after_live_check(col, monkeypatch):
    build(col)
    monkeypatch.setattr(catalog, "refresh_live", lambda ps, client=None: [setattr(p, "live", True) or setattr(p, "in_stock", False) for p in ps])
    assert catalog.search_products("краскопульт", col=col) == []


# ---------- синхронизация целиком на моке KIT

def kit_handler(req):
    path, q = req.url.path, dict(req.url.params)
    page, per = int(q.get("page", 1)), int(q.get("per_page", 25))
    key = {"/v1/categories": "categories", "/v1/characteristics": "characteristics", "/v1/products": "products",
           "/v1/files": "files", "/v1/variants": "variants"}.get(path)
    if path == "/v1/store":
        return httpx.Response(200, json=FX["store"])
    if key:
        if key == "variants":
            assert q["status"] == "PUBLISHED"
        return httpx.Response(200, json={key: FX[key][(page - 1) * per: page * per], "total_count": len(FX[key])})
    return httpx.Response(404, json={"code": "NOT_FOUND", "message": path, "trace_id": "t"})


def test_sync_catalog_end_to_end(col):
    stats = catalog.sync_catalog(kit_client_for(kit_handler), col)
    assert stats["total"] == 3 and col.count() == 3
    p = catalog.get_product("var-1", live=False, col=col)
    assert p.category == "Краскопульты / Электрические" and p.url.startswith("https://demo.kit")


def test_sync_failure_keeps_old_index(col):
    catalog.sync_catalog(kit_client_for(kit_handler), col)

    def broken(req):
        if req.url.path == "/v1/variants":
            return httpx.Response(500, json={"code": "UNKNOWN_ERROR", "message": "x", "trace_id": "tr"})
        return kit_handler(req)

    with pytest.raises(Exception):
        catalog.sync_catalog(kit_client_for(broken), col)
    assert col.count() == 3
    assert catalog.safe_sync() is False       # без токена/клиента не падает
