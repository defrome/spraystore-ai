import os
import re
import uuid
import chromadb
from openai import OpenAI
from sentence_transformers import SentenceTransformer

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

llm = OpenAI(
    api_key=os.environ["GENAPI_KEY"],
    base_url=os.getenv("GENAPI_BASE_URL", "https://proxy.gen-api.ru/v1"),
)

embedder = SentenceTransformer("intfloat/multilingual-e5-small")

KNOWLEDGE_BASE = {
    "о компании": """
        SprayStore (ООО «СПРЕЙСТОУН», ИНН 7727443287) — интернет-магазин
        краскопультов и окрасочного оборудования для дома и профессиональных задач.
        Специализация — механизированная окраска: подбор оборудования, сопел
        и комплектующих под конкретный материал и объём работ.
        Работаем с брендами Wagner, TecMaster, Aeropro и другими.
        В каталоге не только краскопульты, но и Airless- и HVLP-системы,
        а также инструменты, аксессуары и запчасти.
        Помогаем настроить оборудование и подобрать расходники.
        При первом заказе действует скидка 10% по промокоду HELLO.
    """,
    "доставка и самовывоз": """
        Доступен самовывоз из пункта выдачи заказов.
        Адрес ПВЗ: 143960, Московская область, г. Реутов,
        Железнодорожная ул., д. 11, помещ. V.
        Режим работы ПВЗ: с 09:00 до 17:00 (московское время) в рабочие дни.
        Ориентировочный срок доставки указан в карточке каждого товара.
        При получении заказа обязательно проверьте целостность упаковки,
        отсутствие механических повреждений и соответствие комплектации.
        Для получения товара может потребоваться документ, удостоверяющий личность.
    """,
    "каталог и услуги": """
        Основные категории товаров:
        — Пневматические краскопульты (HP, HVLP, LVLP, LVMP)
        — Электрические краскопульты (воздушные и безвоздушные)
        — Airless-системы (безвоздушного распыления)
        — Компрессоры и воздушные шланги
        — Сопла, дюзы, иглы, бачки, ремкомплекты
        — Аксессуары: манометры, фильтры, переходники, маски, плёнка
        — Запасные части для краскопультов

        Услуги:
        — Подбор оборудования под задачу (что красите, чем, какой объём)
        — Подбор сопла и комплектующих под материал
        — Помощь в настройке оборудования
        — Консультация по расходникам и запчастям
    """,
    "контакты": """
        Интернет-магазин: https://spraystore.ru
        Пункт выдачи заказов: 143960, Московская область, г. Реутов,
        Железнодорожная ул., д. 11, помещ. V.
        Режим работы ПВЗ: пн–пт, 09:00–17:00 (МСК).
        Юридическое лицо: ООО «СПРЕЙСТОУН», ИНН 7727443287.
        Для связи используйте форму обратной связи на сайте или чат с оператором.
    """,
    "краскопульты: типы и системы распыления": """
        По принципу распыления краскопульты делятся на:
        — Воздушные (пневматические и электрические компрессорного типа) —
          краска распыляется потоком воздуха.
        — Безвоздушные (Airless) — краска подаётся под высоким давлением
          через сопло без воздуха, даёт ровное покрытие на больших площадях.

        Пневматические краскопульты по системе смешения делятся на классы:
        — HP (High Pressure) — высокое давление (1,2–1,5 атм), низкий расход
          воздуха. Бюджетный вариант, но больше тумана и перерасход краски.
        — HVLP (High Volume Low Pressure) — низкое давление (~0,7 атм),
          большой объём воздуха (>300 л/мин). Высокий перенос материала
          (до 75%), меньше тумана. Подходит для тонких равномерных покрытий.
        — LVLP (Low Volume Low Pressure) — низкое давление (0,7–1,2 атм)
          и малый объём воздуха. Перенос до 80%, экономичен.
        — LVMP (Low Volume Medium Pressure) — промежуточный вариант,
          часто используется для грунтов и шпатлёвок.

        Для дома и небольших объёмов чаще берут электрические краскопульты.
        Для профессиональной окраски и тонких покрытий — пневматические HVLP/LVLP.
    """,
    "краскопульты: выбор сопла (дюзы)": """
        Диаметр сопла подбирается под материал:
        — 0,8–1,0 мм — жидкие составы: морилки, лаки, пропитки, тонкие эмали.
        — 1,2–1,3 мм — базовые эмали, прозрачные лаки, акриловые краски.
        — 1,5–1,6 мм — акриловые краски, лаки, грунты, универсальные работы.
        — 1,8–2,0 мм — грунтовки, жидкие шпатлёвки, водоэмульсионные краски.
        — 2,5–3,0 мм — густые материалы: шпатлёвки, раптор, антигравий,
          текстурные покрытия.

        Если красите разные материалы, берите краскопульт с набором сопел
        или докупайте сопла отдельно.
    """,
    "краскопульты: подбор под материал": """
        Для водоэмульсионной краски: электрический краскопульт компрессорного
        типа мощностью 600–800 Вт с соплом 1,8–2,6 мм, либо пневматический HP/HVLP
        с соплом 1,8–2,0 мм.

        Для лака и прозрачных покрытий: HVLP или LVLP с соплом 1,0–1,3 мм.
        Даёт тонкий ровный слой без наплывов.

        Для грунта: LVMP или HVLP с соплом 1,5–1,8 мм, либо HP с соплом 1,7–2,0 мм.

        Для жидкой шпатлёвки: краскопульт с соплом 2,0–2,5 мм,
        желательно с верхним бачком.

        Для раптора и антигравия: краскопульт с соплом 2,5 мм и специальной
        головой (например, MP375), часто используется ISPRAY ST31 RP.

        Для красок по дереву и антисептиков: HVLP или электрический
        с соплом 1,5–1,8 мм.
    """,
    "краскопульты: на что смотреть при выборе": """
        — Материал корпуса и деталей: долговечны хромированные сплавы алюминия
          и элементы из нержавеющей стали.
        — Объём бачка: для долгой работы без дозаправки берите 0,6–1,0 л.
        — Расположение бачка: верхний — удобнее для точных работ и малых объёмов,
          нижний — для больших объёмов и густых материалов.
        — Диаметр сопла: должен соответствовать вашему материалу (см. выше).
        — Расход воздуха: компрессор должен выдавать на 15–20% больше,
          чем потребляет краскопульт.
        — Наличие регулировок: факел, подача краски, подача воздуха.
        — Вязкость материала: проверяйте по вискозиметру (DIN) перед покраской.
    """,
    "частые вопросы": """
        В: Можно ли красить водоэмульсионкой пневматическим краскопультом?
        О: Да, но нужно сопло 1,8–2,0 мм и достаточная производительность
        компрессора. Для больших площадей лучше Airless-система.

        В: Чем HVLP отличается от LVLP?
        О: HVLP даёт большой объём воздуха при низком давлении и высокий перенос
        (до 75%). LVLP экономичнее по воздуху и даёт перенос до 80%,
        но требует более точной настройки.

        В: Какой краскопульт выбрать для авто?
        О: Для базовых эмалей и лаков — HVLP/LVLP с соплом 1,2–1,3 мм.
        Для грунтов — 1,5–1,8 мм. Для раптора — 2,5 мм.

        В: Нужен ли компрессор для пневматического краскопульта?
        О: Да, пневматический краскопульт работает от компрессора.
        Электрический компрессорного типа — нет, у него встроенная турбина.

        В: Чем чистить краскопульт?
        О: Промывайте сразу после работы подходящим растворителем
        (вода — для водоэмульсионки, растворитель — для эмалей и лаков).
        Разбирайте голову и сопло, чистите каналы.
    """,
}

_UNITS = re.compile(r"(?<=[.!?])\s+|\n")


def _split_words(units: list[str], max_chars: int):
    """Предложение длиннее лимита (нет точек) режем по словам — но не посреди слова."""
    for u in units:
        while len(u) > max_chars:
            cut = u.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            yield u[:cut]
            u = u[cut:].lstrip()
        yield u


def _split_long(para: str, max_chars: int, overlap: int) -> list[str]:
    """Длинный абзац режем по предложениям/строкам; хвост предыдущего куска (целое предложение) идёт в следующий."""
    chunks, cur, last = [], "", ""
    for unit in (u.strip() for u in _split_words(_UNITS.split(para), max_chars)):
        if not unit:
            continue
        if cur and len(cur) + len(unit) + 1 > max_chars:
            chunks.append(cur)
            # перекрытие — только целое предложение, чтобы не резать слова
            cur = (last + " " + unit) if last and len(last) <= overlap else unit
        else:
            cur = (cur + " " + unit) if cur else unit
        last = unit
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text: str, max_chars: int = 500, overlap: int = 120) -> list[str]:
    """Режем по абзацам (строки списка сохраняются), длинные абзацы — по предложениям."""
    paragraphs = []
    for p in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        if lines:
            paragraphs.append("\n".join(lines))
    chunks, buf = [], ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_split_long(para, max_chars, overlap))
        elif buf and len(buf) + len(para) + 2 > max_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para) if buf else para
    if buf:
        chunks.append(buf)
    return chunks


# Источник статей. По умолчанию — словарь выше; сервис подменяет его на БД (редактируется в /admin).
# Провайдер возвращает [{"id", "title", "body", "kind": "article"|"qa"}].
KB_PROVIDER = None
QA_BOOST = 0.03   # эталонные пары «вопрос — ответ», добавленные из админки, выигрывают у обычных статей


def load_articles() -> list[dict]:
    if KB_PROVIDER:
        return KB_PROVIDER()
    return [{"id": t, "title": t, "body": b, "kind": "article"} for t, b in KNOWLEDGE_BASE.items()]


def build_chunks() -> list[dict]:
    result = []
    for art in load_articles():
        body = art["body"]
        if art.get("kind") == "qa":
            body = f"В: {art['title']}\nО: {body}"
        for i, piece in enumerate(chunk_text(body)):
            # e5 требует префикс "passage: "; название статьи в эмбеддинге улучшает поиск
            result.append({
                "id": f"{art.get('id', art['title'])}::{i}",
                "topic": art["title"],
                "kind": art.get("kind", "article"),
                "text": piece,
                "embed_input": f"passage: {art['title']}. {piece}",
            })
    return result

DB_PATH = os.getenv("CHROMA_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_spraystore"))
COLLECTION = "spraystore"

def get_collection():
    client = chromadb.PersistentClient(path=DB_PATH)
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def _vec(x) -> list[float]:
    return x.tolist() if hasattr(x, "tolist") else list(x)


def build_index(force: bool = False):
    """force=True — привести индекс в соответствие с базой: обновить изменённое, удалить лишнее.
    Индекс не пустеет на время пересборки, поэтому чат можно не останавливать."""
    col = get_collection()
    if col.count() > 0 and not force:
        print(f"[index] уже собран: {col.count()} чанков")
        return col

    chunks = build_chunks()
    if chunks:
        col.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            metadatas=[{"topic": c["topic"], "kind": c["kind"]} for c in chunks],
            embeddings=[_vec(embedder.encode(c["embed_input"], normalize_embeddings=True)) for c in chunks],
        )
    stale = set(col.get(include=[])["ids"]) - {c["id"] for c in chunks}
    if stale:
        col.delete(ids=sorted(stale))
    print(f"[index] собрано {col.count()} чанков")
    return col

def retrieve(question: str, k: int = 4) -> list[dict]:
    col = get_collection()
    if col.count() == 0:
        return []
    # e5 требует префикс "query: " для запроса
    q_vec = _vec(embedder.encode("query: " + question, normalize_embeddings=True))
    res = col.query(query_embeddings=[q_vec], n_results=min(k, col.count()))
    hits = []
    for text, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        kind = meta.get("kind", "article")
        hits.append({"text": text, "topic": meta["topic"], "kind": kind,
                     "score": 1 - dist + (QA_BOOST if kind == "qa" else 0.0)})
    return sorted(hits, key=lambda h: -h["score"])


def build_context(hits: list[dict]) -> str:
    return "\n\n---\n\n".join(f"[{h['topic']}]\n{h['text']}" for h in hits)

MODEL = "deepseek-v4-1-flash"
PROMPT_VERSION = "v3"   # менять при правках SYSTEM_PROMPT — попадает в лог для разбора дизлайков
# context — ищем товары сами и кладём в контекст (предсказуемо, не требует tool calling у прокси);
# tools — модель сама вызывает search_products; auto — пробуем tools, при ошибке API откатываемся на context
CATALOG_MODE = os.getenv("CATALOG_MODE", "context").lower()

SYSTEM_PROMPT = """Ты — умный ассистент интернет-магазина SprayStore (spraystore.ru).
Отвечай вежливо, кратко, по делу, на русском языке.
Используй ТОЛЬКО информацию из блока «Контекст» ниже.
Если ответа в контексте нет — честно скажи: «У меня нет такой информации, уточните у оператора».
Не выдумывай факты, цены и наличие товара.

Рекомендации товаров:
— Рекомендуй ТОЛЬКО товары из блока «Товары из каталога». Называй товар его полным названием, как в каталоге.
— Цену и наличие бери только оттуда. Если у товара помечено «актуальность цены и наличия не проверена» — скажи об этом.
— Кратко объясни, почему товар подходит под задачу клиента: опирайся на его характеристики и на статьи контекста
  (подбор сопла и типа краскопульта под материал и т. п.).
— Не больше 3 рекомендаций. НЕ вставляй в текст ссылки и URL: карточки товаров со ссылками показываются под ответом сами.
— Отвечай компактно: сначала 1–2 предложения по сути и сразу рекомендации (название, цена, наличие, почему подходит).
  Не пересказывай статьи целиком и не задавай больше одного-двух уточняющих вопросов.
— Если подходящего товара в каталоге нет (блок пуст или не подходит под задачу или бюджет) — честно скажи об этом
  и предложи уточнить у оператора. Не придумывай товары из головы.

=== КОНТЕКСТ ===
{context}
=== КОНЕЦ КОНТЕКСТА ==="""

TOOLS_NOTE = ("=== Товары из каталога ===\nТовары в контекст не загружены: чтобы найти товар, цену или наличие, "
              "вызови функцию search_products. Рекомендуй только то, что она вернула.")


def _messages(context: str, history, question: str) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=context)}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})
    return messages


def answer(question: str, history: list | None = None, k: int = 4, debug: bool = False) -> dict:
    """Возвращает {"text": ответ, "hits": найденные чанки, "products": товары каталога в контексте}."""
    hits = retrieve(question, k=k)
    if debug:
        for h in hits:
            print(f"  [{h['score']:.3f}] {h['topic']}: {h['text'][:80]}...")

    context = build_context(hits)

    products: list = []
    text = None
    catalog_on = False
    try:
        import kit_client
        catalog_on = kit_client.is_enabled()   # нет токена — каталог выключен, всё как раньше
    except Exception:
        catalog_on = False

    if catalog_on and CATALOG_MODE in ("tools", "auto"):
        try:
            import catalog_chat
            text, products = catalog_chat.run_tool_loop(
                llm, MODEL, _messages(context + "\n\n---\n\n" + TOOLS_NOTE, history, question))
        except Exception as e:
            if CATALOG_MODE == "tools":
                raise
            print(f"[catalog] tool calling не сработал ({e!r}), откат на режим context")
            text, products = None, []

    if text is None:
        if catalog_on:
            try:
                import catalog_chat
                products = catalog_chat.prepare_products(question, history)
                if catalog_chat.is_product_query(question, history):
                    context += "\n\n---\n\n" + catalog_chat.format_products_block(products)
            except Exception as e:   # каталог не должен ронять чат
                print(f"[catalog] поиск товаров не удался: {e!r}")
                products = []
        r = llm.chat.completions.create(
            model=MODEL,
            messages=_messages(context, history, question),
            temperature=0.3,
        )
        text = r.choices[0].message.content
    return {"text": text, "hits": hits, "products": products}

def ask(question: str, history: list | None = None, k: int = 4, debug: bool = False) -> str:
    return answer(question, history, k=k, debug=debug)["text"]

if __name__ == "__main__":
    build_index(force=False)  # force=True — пересобрать после правок базы
    history = []
    print("Ассистент SprayStore готов. 'exit' — выход, 'debug' — включить показ найденных чанков.\n")
    debug = False
    while True:
        q = input("Вопрос: ").strip()
        if not q:
            continue
        if q.lower() in ("exit", "quit", "выход"):
            break
        if q.lower() == "debug":
            debug = not debug
            print(f"[debug: {debug}]")
            continue
        answer = ask(q, history, debug=debug)
        print(f"\n{answer}\n")
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": answer})
        history = history[-8:]