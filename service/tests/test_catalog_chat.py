import json
import types

import pytest

import catalog
import catalog_chat as cc
from catalog import Product


def P(**kw):
    d = dict(id="1", name="Wagner FLEXiO 590", sku="W-590", category="Краскопульты", price=9990.0, stock=4,
             in_stock=True, url="https://x.ru/p/1", characteristics={"Диаметр сопла": "1.3 мм"}, live=True)
    d.update(kw)
    return Product(**d)


# ---------- эвристики

@pytest.mark.parametrize("q", ["Посоветуйте краскопульт для лака", "сколько стоит Wagner W 100", "чем красить забор",
                               "есть ли в наличии компрессор", "нужен до 10 000 ₽", "хочу купить краскопульт"])
def test_product_queries(q):
    assert cc.is_product_query(q)


@pytest.mark.parametrize("q", ["Как работает самовывоз?", "Где находится пункт выдачи", "Чем чистить краскопульт?"])
def test_non_product_queries(q):
    assert not cc.is_product_query(q)


def test_followup_inherits_topic():
    h = [{"role": "user", "content": "Посоветуйте краскопульт для лака"}, {"role": "assistant", "content": "..."}]
    assert cc.is_product_query("а подешевле?", h)
    assert not cc.is_product_query("а где вы находитесь?", [{"role": "user", "content": "Привет"}])
    assert cc.build_query("а подешевле?", h).startswith("Посоветуйте краскопульт")


@pytest.mark.parametrize("text,val", [("краскопульт для лака до 10 000 ₽", 10000), ("дешевле 5000 руб", 5000),
                                      ("бюджет 8к", 8000), ("до 15 тыс", 15000), ("сопло до 2 мм", None),
                                      ("до 3 штук", None), ("просто текст", None)])
def test_extract_max_price(text, val):
    assert cc.extract_max_price(text) == val


# ---------- блок контекста

def test_format_block():
    b = cc.format_products_block([P(), P(id="2", name="Старый", live=False, in_stock=False, stock=0, price=None, url="")])
    assert "=== Товары из каталога ===" in b
    assert "Wagner FLEXiO 590 (арт. W-590) — 9 990 ₽; в наличии (4 шт.); данные сверены" in b
    assert "Диаметр сопла: 1.3 мм" in b and "Ссылка: https://x.ru/p/1" in b
    assert "цена не указана; нет в наличии; актуальность цены и наличия не проверена" in b
    assert "не найдено" in cc.format_products_block([])
    assert cc.format_products_block([], searched=False) == ""


def test_mentioned_products():
    ps = [P(), P(id="2", name="TecMaster 800", sku="T-800"), P(id="3", name="Третий", sku="")]
    t = "Рекомендую wagner  flexio 590 и модель T-800."
    assert [p.id for p in cc.mentioned_products(t, ps)] == ["1", "2"]
    assert cc.mentioned_products("Подходящих товаров нет", ps) == []


# ---------- tool calling (мок OpenAI)

def tool_call(name, args, cid="c1"):
    return types.SimpleNamespace(id=cid, function=types.SimpleNamespace(name=name, arguments=args if isinstance(args, str) else json.dumps(args)))


def resp(content=None, calls=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=calls))])


class FakeLLM:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_tool_loop_executes_and_returns_products(monkeypatch):
    seen = {}

    def fake_search(query, max_price=None, category=None, in_stock_only=True, k=5, **kw):
        seen.update(query=query, max_price=max_price, in_stock_only=in_stock_only)
        return [P()]

    monkeypatch.setattr(catalog, "search_products", fake_search)
    llm = FakeLLM(resp(calls=[tool_call("search_products", {"query": "лак", "max_price": 10000})]), resp("Берите Wagner FLEXiO 590"))
    text, prods = cc.run_tool_loop(llm, "m", [{"role": "user", "content": "q"}])
    assert text == "Берите Wagner FLEXiO 590" and [p.id for p in prods] == ["1"]
    assert seen == {"query": "лак", "max_price": 10000, "in_stock_only": True}
    # во втором запросе модель видит assistant(tool_calls) и ответ инструмента с реальными данными
    msgs = llm.calls[1]["messages"]
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "search_products"
    tool_msg = msgs[-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "9990" in tool_msg["content"]


def test_tool_loop_no_tool_call_and_bad_args(monkeypatch):
    assert cc.run_tool_loop(FakeLLM(resp("Привет")), "m", [])[0] == "Привет"
    llm = FakeLLM(resp(calls=[tool_call("search_products", "{не json")]), resp("ок"))
    text, prods = cc.run_tool_loop(llm, "m", [])
    assert text == "ок" and "невалидный" in llm.calls[1]["messages"][-1]["content"]
    llm = FakeLLM(resp(calls=[tool_call("rm_rf", {})]), resp("ок"))
    cc.run_tool_loop(llm, "m", [])
    assert "неизвестная функция" in llm.calls[1]["messages"][-1]["content"]


def test_tool_loop_iteration_limit(monkeypatch):
    monkeypatch.setattr(catalog, "search_products", lambda *a, **k: [])
    loop = [resp(calls=[tool_call("search_products", {"query": "x"})]) for _ in range(3)]
    llm = FakeLLM(*loop, resp("итог"))
    text, _ = cc.run_tool_loop(llm, "m", [], max_iter=3)
    assert text == "итог" and len(llm.calls) == 4 and "tools" not in llm.calls[3]


def test_tools_expose_only_readonly_search():
    assert {t["function"]["name"] for t in cc.TOOLS} == {"search_products", "get_product"}


# ---------- интеграция в main.answer: context / tools / auto / без токена

@pytest.fixture
def wired(rag, monkeypatch):
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: [{"text": "Сопло 1,0–1,3 мм для лака", "topic": "сопло", "score": 0.9}])
    monkeypatch.setenv("KIT_API_TOKEN", "t")
    searched = []
    monkeypatch.setattr(catalog, "search_products", lambda q, **kw: (searched.append((q, kw)), [P()])[1])
    return rag, searched


def test_context_mode_adds_products_block(wired):
    rag, searched = wired
    rag.CATALOG_MODE = "context"
    rag.llm = FakeLLM(resp("Wagner FLEXiO 590"))
    res = rag.answer("Посоветуйте краскопульт для лака до 10 000 ₽", [])
    system = rag.llm.calls[0]["messages"][0]["content"]
    assert "=== Товары из каталога ===" in system and "9 990 ₽" in system and "Сопло 1,0" in system
    assert searched[0][1]["max_price"] == 10000
    assert [p.id for p in res["products"]] == ["1"] and "tools" not in rag.llm.calls[0]


def test_context_mode_non_product_question_no_search(wired):
    rag, searched = wired
    rag.CATALOG_MODE = "context"
    rag.llm = FakeLLM(resp("Самовывоз в Реутове"))
    res = rag.answer("Где находится пункт выдачи?", [])
    assert searched == [] and res["products"] == []
    assert "=== Товары из каталога ===" not in rag.llm.calls[0]["messages"][0]["content"]


def test_auto_falls_back_to_context_on_api_error(wired):
    rag, searched = wired
    rag.CATALOG_MODE = "auto"
    rag.llm = FakeLLM(RuntimeError("tools not supported"), resp("Wagner FLEXiO 590"))
    res = rag.answer("Посоветуйте краскопульт для лака", [])
    assert res["text"] == "Wagner FLEXiO 590" and [p.id for p in res["products"]] == ["1"]
    assert "tools" in rag.llm.calls[0] and "tools" not in rag.llm.calls[1]
    assert "=== Товары из каталога ===" in rag.llm.calls[1]["messages"][0]["content"]


def test_tools_mode_raises_on_api_error(wired):
    rag, _ = wired
    rag.CATALOG_MODE = "tools"
    rag.llm = FakeLLM(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        rag.answer("Посоветуйте краскопульт", [])


def test_tools_mode_uses_tool_results(wired):
    rag, _ = wired
    rag.CATALOG_MODE = "tools"
    rag.llm = FakeLLM(resp(calls=[tool_call("search_products", {"query": "лак"})]), resp("Wagner FLEXiO 590"))
    res = rag.answer("Посоветуйте краскопульт", [])
    assert [p.id for p in res["products"]] == ["1"] and "search_products" in rag.llm.calls[0]["messages"][0]["content"]


def test_without_token_behaves_as_before(wired, monkeypatch):
    rag, searched = wired
    monkeypatch.delenv("KIT_API_TOKEN")
    rag.CATALOG_MODE = "auto"
    rag.llm = FakeLLM(resp("Ответ"))
    res = rag.answer("Посоветуйте краскопульт для лака", [])
    assert searched == [] and res == {"text": "Ответ", "hits": res["hits"], "products": []}
    assert "tools" not in rag.llm.calls[0] and "=== Товары из каталога ===" not in rag.llm.calls[0]["messages"][0]["content"]
    assert rag.PROMPT_VERSION == "v3"
