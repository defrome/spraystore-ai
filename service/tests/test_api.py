import sqlite3

import pytest
from fastapi.testclient import TestClient

from catalog import Product


@pytest.fixture
def api(rag, tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_LOG_DB", str(tmp_path / "log.db"))
    import sys
    sys.modules.pop("api", None)
    import api as api_mod
    with api_mod.db() as con:       # lifespan не запускаем (он строит боевой индекс) — готовим БД вручную
        con.executescript(api_mod.SCHEMA)
        api_mod.migrate(con)
    yield api_mod
    sys.modules.pop("api", None)


def test_chat_contract_without_products(api, monkeypatch):
    monkeypatch.setattr(api.rag, "answer", lambda q, h: {"text": "Привет", "hits": [{"topic": "т", "text": "", "score": 0.9}], "products": []})
    j = TestClient(api.app).post("/api/chat", json={"message": "hi", "session_id": "s"}).json()
    assert set(j) == {"text", "confident", "message_id", "sources"} and j["confident"] is True


def test_chat_cards_and_log(api, monkeypatch):
    p = Product(id="v1", name="Wagner FLEXiO 590", price=9990.0, in_stock=True, url="https://x.ru/p", image="https://x.ru/i.jpg")
    other = Product(id="v2", name="Другой")
    monkeypatch.setattr(api.rag, "answer", lambda q, h: {"text": "Рекомендую Wagner FLEXiO 590 — 9 990 ₽", "hits": [], "products": [p, other]})
    j = TestClient(api.app).post("/api/chat", json={"message": "лак", "session_id": "s"}).json()
    assert j["cards"] == [{"id": "v1", "name": "Wagner FLEXiO 590", "price": 9990.0, "url": "https://x.ru/p",
                           "in_stock": True, "image": "https://x.ru/i.jpg"}]
    assert j["confident"] is True       # статей нет, но ответ опирается на товар
    assert j["text"].endswith("Ссылка: [Открыть на сайте ↗](https://x.ru/p)")   # ссылка добавляется всегда
    with sqlite3.connect(api.LOG_DB) as con:
        assert con.execute("SELECT products FROM messages WHERE id=?", (j["message_id"],)).fetchone()[0] == '["v1"]'


def test_migration_on_old_db(api, tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, ts REAL, question TEXT, answer TEXT, "
                "model TEXT, prompt_version TEXT, sources TEXT, top_score REAL, confident INTEGER, latency_ms INTEGER)")
    api.migrate(con)
    api.migrate(con)     # повторно — без ошибки
    assert "products" in {r[1] for r in con.execute("PRAGMA table_info(messages)")}
