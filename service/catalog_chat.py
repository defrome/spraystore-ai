"""Мост между каталогом и нейронкой: определение «товарного» запроса, блок товаров для контекста,
function calling (tools) с лимитом итераций, поиск упомянутых в ответе товаров.

Модуль не импортирует main.py (LLM-клиент передаётся параметром) — поэтому тестируется без сети и моделей.
Нейронке отдаём только узкие read-only функции: search_products и get_product.
"""
import json
import re

import catalog
from catalog import Product

MAX_TOOL_ITERATIONS = 3
MAX_CARDS = 3

# ---------------------------------------------------------------- «товарный» запрос (эвристика)

_PRODUCT_RE = re.compile(
    r"подбер|подобрать|подбор|посоветуй|порекомендуй|рекоменд|что лучше|что взять|чем красить|чем покрасить|"
    r"какой (краскопульт|компрессор|аппарат|пистолет|шланг|распылитель)|какую? (взять|выбрать|купить)|"
    r"куп(ить|лю|ил)|цен[аыеу]\b|сколько стоит|стоимост|почём|налич|в продаже|есть ли\b|"
    r"дешев|дорог|бюджет|недорог|"
    r"для (краски|лака|грунт|забор|фасад|стен|потолк|мебел|авто|дерев|двер|металл)|"
    r"красить|покрасить|окрасить|лакировать|"
    r"\d\s*(₽|руб|р\b|тыс|к\b)", re.I)


def is_product_query(question: str, history: list | None = None) -> bool:
    """Нужен ли поиск по каталогу. Короткие уточнения («а подешевле?») наследуют тему из прошлой реплики."""
    if _PRODUCT_RE.search(question):
        return True
    if history and len(question) < 40:
        prev = [h["content"] for h in history if h.get("role") == "user"]
        return bool(prev) and bool(_PRODUCT_RE.search(prev[-1]))
    return False


_PRICE_RE = re.compile(
    r"(?:до|дешевле|не дороже|не более|максимум|бюджет(?:ом)?|в пределах)\s*(?:до\s*)?"
    r"(\d[\d\s  ]*)\s*(тыс\w*|к\b|₽|руб\w*|р\b)?\s*(мм|л\b|атм|вт|см|м\b|бар)?", re.I)


def extract_max_price(text: str) -> float | None:
    """«до 10 000 ₽», «дешевле 5000», «бюджет 8к» -> число. Размеры («до 2 мм») и мелкие числа игнорируем."""
    for m in _PRICE_RE.finditer(text):
        if m.group(3):      # «до 2 мм», «до 0,8 атм» — это не цена
            continue
        digits = re.sub(r"[\s  ]", "", m.group(1))
        if not digits.isdigit():
            continue
        val = float(digits)
        if m.group(2) and m.group(2).lower().startswith(("тыс", "к")):
            val *= 1000
        if val >= 300:
            return val
    return None


def build_query(question: str, history: list | None = None) -> str:
    """Для коротких уточнений добавляем прошлый вопрос пользователя — иначе поиск потеряет тему."""
    if history and len(question) < 40:
        prev = [h["content"] for h in history if h.get("role") == "user"]
        if prev:
            return f"{prev[-1]}. {question}"
    return question


def prepare_products(question: str, history: list | None = None, k: int = 5) -> list[Product]:
    """Ветка «context»: сами решаем, что запрос товарный, и сами ищем."""
    if not is_product_query(question, history):
        return []
    max_price = extract_max_price(question)
    if max_price is None and history:   # «а до 10 000?» могло прозвучать раньше
        for h in reversed([x for x in history if x.get("role") == "user"][-2:]):
            max_price = extract_max_price(h["content"])
            if max_price:
                break
    return catalog.search_products(build_query(question, history), max_price=max_price, k=k)


# ---------------------------------------------------------------- блок «Товары из каталога»

def _rub(v: float | None) -> str:
    return "цена не указана" if v is None else f"{v:,.0f}".replace(",", " ") + " ₽"


def format_product(i: int, p: Product) -> str:
    stock = f"в наличии ({p.stock} шт.)" if p.in_stock and p.stock else ("в наличии" if p.in_stock else "нет в наличии")
    price = _rub(p.price) + (f" (без скидки {_rub(p.old_price)})" if p.old_price else "")
    fresh = "данные сверены с каталогом только что" if p.live else "актуальность цены и наличия не проверена"
    lines = [f"{i}. {p.name}" + (f" (арт. {p.sku})" if p.sku else "") + f" — {price}; {stock}; {fresh}"]
    if p.category:
        lines.append(f"   Категория: {p.category}")
    if p.characteristics:
        lines.append("   Характеристики: " + "; ".join(f"{k}: {v}" for k, v in list(p.characteristics.items())[:10]))
    if p.url:
        lines.append(f"   Ссылка: {p.url}")
    return "\n".join(lines)


def format_products_block(products: list[Product], searched: bool = True) -> str:
    """Блок для «Контекста». searched=True и пусто -> явно говорим, что поиск ничего не нашёл."""
    if not products:
        return ("=== Товары из каталога ===\nПодходящих товаров в каталоге не найдено."
                if searched else "")
    return "=== Товары из каталога ===\n" + "\n".join(format_product(i, p) for i, p in enumerate(products, 1))


def mentioned_products(text: str, products: list[Product], limit: int = MAX_CARDS) -> list[Product]:
    """Какие из товаров контекста модель реально назвала в ответе (по названию или артикулу) — их и показываем карточками."""
    norm = lambda s: re.sub(r"[\s ]+", " ", s).casefold()
    t = norm(text)
    out = []
    for p in products:
        if (p.name and norm(p.name) in t) or (len(p.sku) >= 3 and norm(p.sku) in t):
            out.append(p)
    return out[:limit]


# ---------------------------------------------------------------- function calling

TOOLS = [
    {"type": "function", "function": {
        "name": "search_products",
        "description": "Поиск товаров в каталоге SprayStore по описанию задачи или названию. "
                       "Возвращает реальные цены и наличие. Вызывай, когда клиент просит подобрать/купить товар или спрашивает цену.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Что ищем, своими словами: «краскопульт для лака», «компрессор 50 л»"},
            "max_price": {"type": "number", "description": "Максимальная цена в рублях, если клиент назвал бюджет"},
            "category": {"type": "string", "description": "Часть названия категории, если нужно сузить"},
            "in_stock_only": {"type": "boolean", "description": "Только товары в наличии (по умолчанию true)"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_product",
        "description": "Актуальные данные одного товара по id из результатов search_products.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
]


def _tool_view(p: Product) -> dict:
    return {"id": p.id, "name": p.name, "sku": p.sku, "category": p.category, "price_rub": p.price,
            "in_stock": p.in_stock, "stock": p.stock, "characteristics": dict(list(p.characteristics.items())[:10]),
            "url": p.url, "price_stock_verified": bool(p.live)}


def execute_tool(name: str, args: dict, found: dict[str, Product]) -> str:
    """Выполняет вызов функции модели -> JSON-строка результата. found копит товары для карточек."""
    try:
        if name == "search_products":
            res = catalog.search_products(
                str(args.get("query", "")),
                max_price=args.get("max_price") if isinstance(args.get("max_price"), (int, float)) else None,
                category=args.get("category") or None,
                in_stock_only=args.get("in_stock_only", True) is not False, k=5)
            for p in res:
                found[p.id] = p
            return json.dumps({"products": [_tool_view(p) for p in res]} if res
                              else {"products": [], "note": "ничего подходящего не найдено"}, ensure_ascii=False)
        if name == "get_product":
            p = catalog.get_product(str(args.get("id", "")))
            if p is None:
                return json.dumps({"error": "товар не найден"}, ensure_ascii=False)
            found[p.id] = p
            return json.dumps(_tool_view(p), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"поиск недоступен: {type(e).__name__}"}, ensure_ascii=False)
    return json.dumps({"error": f"неизвестная функция {name}"}, ensure_ascii=False)


def run_tool_loop(llm, model: str, messages: list[dict], max_iter: int = MAX_TOOL_ITERATIONS,
                  temperature: float = 0.3) -> tuple[str, list[Product]]:
    """Цикл «модель вызвала функцию -> выполнили -> вернули результат -> ответ».
    Исключения API не глотаем: вызывающий код (режим auto) откатится на ветку context."""
    msgs = list(messages)
    found: dict[str, Product] = {}
    for _ in range(max_iter):
        r = llm.chat.completions.create(model=model, messages=msgs, tools=TOOLS,
                                        tool_choice="auto", temperature=temperature)
        m = r.choices[0].message
        calls = getattr(m, "tool_calls", None)
        if not calls:
            return m.content or "", list(found.values())
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                result = json.dumps({"error": "аргументы — невалидный JSON"}, ensure_ascii=False)
            else:
                result = execute_tool(c.function.name, args, found)
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": result})
    # лимит итераций исчерпан — просим финальный ответ без функций
    r = llm.chat.completions.create(model=model, messages=msgs, temperature=temperature)
    return r.choices[0].message.content or "", list(found.values())
