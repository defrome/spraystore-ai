import os
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

def chunk_text(text: str, max_chars: int = 500, overlap: int = 80) -> list[str]:
    """Режем по абзацам, длинные абзацы — по предложениям."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""

    for para in paragraphs:
        # абзац длиннее лимита — режем на предложения
        if len(para) > max_chars:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            sentences = [s.strip() for s in para.replace("!", ".").replace("?", ".").split(".") if s.strip()]
            cur = ""
            for s in sentences:
                if len(cur) + len(s) + 2 <= max_chars:
                    cur = (cur + ". " + s).strip(". ") if cur else s
                else:
                    chunks.append(cur.strip())
                    # перекрытие: последние overlap символов уносим в следующий чанк
                    cur = (cur[-overlap:] + " " + s).strip() if overlap else s
            if cur:
                chunks.append(cur.strip())
        else:
            if len(buf) + len(para) + 2 <= max_chars:
                buf = (buf + "\n\n" + para).strip() if buf else para
            else:
                chunks.append(buf.strip())
                buf = para

    if buf:
        chunks.append(buf.strip())
    return chunks


def build_chunks() -> list[dict]:
    result = []
    for topic, text in KNOWLEDGE_BASE.items():
        for i, piece in enumerate(chunk_text(text)):
            # e5 требует префикс "passage: " для документов
            result.append({
                "id": f"{topic}::{i}",
                "topic": topic,
                "text": piece,
                "embed_input": "passage: " + piece,
            })
    return result

DB_PATH = "./chroma_spraystore"
COLLECTION = "spraystore"

def get_collection():
    client = chromadb.PersistentClient(path=DB_PATH)
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

def build_index(force: bool = False):
    col = get_collection()
    if col.count() > 0 and not force:
        print(f"[index] уже собран: {col.count()} чанков")
        return col

    if force and col.count() > 0:
        col.delete(where={})  # очистить

    chunks = build_chunks()
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[{"topic": c["topic"]} for c in chunks],
        embeddings=[embedder.encode(c["embed_input"], normalize_embeddings=True).tolist()
                    for c in chunks],
    )
    print(f"[index] собрано {col.count()} чанков")
    return col

def retrieve(question: str, k: int = 4) -> list[dict]:
    col = get_collection()
    # e5 требует префикс "query: " для запроса
    q_vec = embedder.encode("query: " + question, normalize_embeddings=True).tolist()
    res = col.query(query_embeddings=[q_vec], n_results=k)
    hits = []
    for text, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        hits.append({"text": text, "topic": meta["topic"], "score": 1 - dist})
    return hits

SYSTEM_PROMPT = """Ты — умный ассистент интернет-магазина SprayStore (spraystore.ru).
Отвечай вежливо, кратко, по делу, на русском языке.
Используй ТОЛЬКО информацию из блока «Контекст» ниже.
Если ответа в контексте нет — честно скажи: «У меня нет такой информации, уточните у оператора».
Не выдумывай факты, цены и наличие товара.

=== КОНТЕКСТ ===
{context}
=== КОНЕЦ КОНТЕКСТА ==="""

def ask(question: str, history: list | None = None, k: int = 4, debug: bool = False) -> str:
    hits = retrieve(question, k=k)
    if debug:
        for h in hits:
            print(f"  [{h['score']:.3f}] {h['topic']}: {h['text'][:80]}...")

    context = "\n\n---\n\n".join(f"[{h['topic']}]\n{h['text']}" for h in hits)

    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=context)}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    r = llm.chat.completions.create(
        model="deepseek-v4-1-flash",
        messages=messages,
        temperature=0.3,
    )
    return r.choices[0].message.content

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