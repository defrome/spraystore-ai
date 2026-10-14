import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import kb


@pytest.fixture
def adm(rag, tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_LOG_DB", str(tmp_path / "log.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setattr(rag, "DB_PATH", str(tmp_path / "chroma"))
    import sys
    sys.modules.pop("api", None)
    import api
    with api.db() as con:
        con.executescript(api.SCHEMA)
        api.migrate(con)
        kb.seed(con, {"доставка": "Самовывоз из ПВЗ в Реутове.\n\nСрок указан в карточке."})
    rag.build_index(force=True)
    c = TestClient(api.app)
    c.headers["Authorization"] = "Bearer secret"
    c.api = api
    yield c
    sys.modules.pop("api", None)


def add_message(api, mid="m1", question="Где ПВЗ?", confident=0, fb=None):
    with api.db() as con:
        con.execute("INSERT INTO messages (id, session_id, ts, question, answer, model, prompt_version, sources,"
                    " top_score, confident, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, "s", time.time(), question, "Не знаю", "m", "v", '["доставка"]', 0.5, confident, 10))
        if fb:
            con.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?)",
                        (mid, "s", fb, json.dumps(["Неточный ответ"]), "плохо", 0, 1, 1, 1))


def test_auth(adm, monkeypatch):
    assert TestClient(adm.api.app).get("/api/admin/ping").status_code == 401
    assert TestClient(adm.api.app).get("/api/admin/ping", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert adm.get("/api/admin/ping").status_code == 200
    monkeypatch.setenv("ADMIN_TOKEN", "")
    assert adm.get("/api/admin/ping").status_code == 503      # токен не задан — админка выключена


def test_bruteforce_limit(adm):
    import admin
    admin._fails.clear()
    anon = TestClient(adm.api.app)
    codes = [anon.get("/api/admin/ping", headers={"Authorization": "Bearer x"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    admin._fails.clear()


def test_seed_once(adm):
    assert [a["title"] for a in adm.get("/api/admin/kb").json()["items"]] == ["доставка"]
    with adm.api.db() as con:
        for a in kb.list_articles(con):
            kb.delete(con, a["id"])
        assert kb.seed(con, {"снова": "текст"}) == 0      # после удаления встроенная база не возвращается
        assert kb.list_articles(con) == []


def test_kb_crud_reindexes(adm, rag):
    r = adm.post("/api/admin/kb", json={"title": "Гарантия", "body": "Гарантия на краскопульты 12 месяцев."})
    assert r.status_code == 200 and r.json()["chunks"] >= 2
    aid = r.json()["article"]["id"]
    assert {h["topic"] for h in rag.retrieve("гарантия 12 месяцев", k=5)} >= {"Гарантия"}
    assert adm.post("/api/admin/kb", json={"title": "Гарантия", "body": "x"}).status_code == 409
    r = adm.put(f"/api/admin/kb/{aid}", json={"title": "Гарантия", "body": "Гарантия 24 месяца.", "kind": "article", "enabled": True})
    assert r.status_code == 200
    got = adm.get(f"/api/admin/kb/{aid}").json()
    assert got["body"] == "Гарантия 24 месяца." and len(got["history"]) == 1 and "12 месяцев" in got["history"][0]["body"]
    # выключенный материал уходит из индекса
    adm.put(f"/api/admin/kb/{aid}", json={"title": "Гарантия", "body": "Гарантия 24 месяца.", "kind": "article", "enabled": False})
    assert "Гарантия" not in {h["topic"] for h in rag.retrieve("гарантия", k=10)}
    assert adm.delete(f"/api/admin/kb/{aid}").json()["deleted"]["title"] == "Гарантия"
    assert adm.get(f"/api/admin/kb/{aid}").status_code == 404


def test_qa_is_boosted(adm, rag):
    adm.post("/api/admin/kb", json={"title": "Где ПВЗ", "body": "Реутов, Железнодорожная 11.", "kind": "qa"})
    hits = rag.retrieve("Где ПВЗ", k=5)
    assert hits[0]["kind"] == "qa" and hits[0]["text"].startswith("В: Где ПВЗ")


def test_inbox_flow(adm):
    add_message(adm.api, "a", "нет ответа", confident=0)
    add_message(adm.api, "b", "дизлайк", confident=1, fb="dislike")
    add_message(adm.api, "c", "всё хорошо", confident=1, fb="like")        # не попадает во входящие
    j = adm.get("/api/admin/inbox").json()
    assert {i["id"]: i["reason"] for i in j["items"]} == {"a": "lowconf", "b": "dislike"}
    assert j["counts"] == {"new": 2, "done": 0, "ignored": 0}
    d = adm.get("/api/admin/inbox/b").json()
    assert d["reasons"] == ["Неточный ответ"] and d["comment"] == "плохо" and d["sources"] == ["доставка"]
    adm.put("/api/admin/inbox/a/review", json={"status": "done"})
    assert [i["id"] for i in adm.get("/api/admin/inbox").json()["items"]] == ["b"]
    assert [i["id"] for i in adm.get("/api/admin/inbox?status=done").json()["items"]] == ["a"]
    adm.put("/api/admin/inbox/a/review", json={"status": "new"})
    assert adm.get("/api/admin/inbox").json()["counts"]["new"] == 2
    assert adm.get("/api/admin/inbox/zzz").status_code == 404


def test_sandbox(adm, monkeypatch):
    r = adm.post("/api/admin/sandbox", json={"question": "самовывоз ПВЗ Реутов"}).json()
    assert r["hits"] and r["hits"][0]["topic"] == "доставка" and "answer" not in r and "=== КОНТЕКСТ ===" in r["prompt"]
    monkeypatch.setattr(adm.api.rag, "answer", lambda q, h=None: {"text": "Ответ", "hits": [], "products": []})
    assert adm.post("/api/admin/sandbox", json={"question": "x", "generate": True}).json()["answer"] == "Ответ"


def test_eval_cases(adm, monkeypatch):
    cid = adm.post("/api/admin/eval", json={"question": "самовывоз ПВЗ Реутов", "expect_topic": "доставка", "expect_text": "реутов"}).json()["id"]
    r = adm.post(f"/api/admin/eval/{cid}/run").json()
    assert r["ok"] is True and r["checks"][1]["ok"] is None            # без модели факты пропущены
    monkeypatch.setattr(adm.api.rag, "answer", lambda q, h=None: {"text": "Реутов", "hits": [], "products": []})
    assert adm.post(f"/api/admin/eval/{cid}/run?answer=true").json()["ok"] is True
    monkeypatch.setattr(adm.api.rag, "answer", lambda q, h=None: {"text": "не знаю", "hits": [], "products": []})
    r = adm.post(f"/api/admin/eval/{cid}/run?answer=true").json()
    assert r["ok"] is False and "реутов" in r["checks"][1]["detail"]
    bad = adm.post("/api/admin/eval", json={"question": "самовывоз", "expect_topic": "нет такого"}).json()["id"]
    assert adm.post(f"/api/admin/eval/{bad}/run").json()["ok"] is False


def test_stats(adm):
    add_message(adm.api, "a", "q1", confident=0)
    add_message(adm.api, "b", "q1", confident=0)
    add_message(adm.api, "c", "q2", confident=1, fb="dislike")
    s = adm.get("/api/admin/stats").json()
    assert s["total"] == 3 and s["lowconf"] == 2 and s["dislike"] == 1
    assert s["unanswered"][0]["count"] == 2 and s["dislike_reasons"] == [{"reason": "Неточный ответ", "count": 1}]


def test_admin_page_not_leaking(adm):
    anon = TestClient(adm.api.app)
    assert anon.get("/admin").status_code == 200 and "noindex" in anon.get("/admin").headers["x-robots-tag"]
    assert anon.get("/service/.env").status_code == 404


def test_chunker_keeps_words_and_text(rag):
    text = "Первое предложение про краскопульты. " * 4 + "\n\n" + "Слово " * 120
    chunks = rag.chunk_text(text, max_chars=200)
    assert all(len(c) <= 260 for c in chunks)
    assert all(not c.startswith((".", " ")) and c for c in chunks)
    joined = " ".join(chunks)
    assert "краскопульты." in joined and "!" not in joined      # знаки препинания не подменяются
    assert rag.chunk_text("  \n\n  ") == []
    assert rag.chunk_text("Коротко.") == ["Коротко."]
