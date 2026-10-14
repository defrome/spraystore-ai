"""API админки: /api/admin/*. Доступ по Bearer-токену из ADMIN_TOKEN (не задан — админка выключена)."""
import hmac
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import kb

FAIL_LIMIT, FAIL_WINDOW = 10, 300   # 10 неверных токенов за 5 минут с одного адреса -> 429
_fails: dict[str, list[float]] = defaultdict(list)


class ArticleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    kind: Literal["article", "qa"] = "article"
    enabled: bool = True


class ReviewIn(BaseModel):
    status: Literal["new", "done", "ignored"]
    article_id: int | None = None


class SandboxIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generate: bool = False


class EvalIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    expect_topic: str = Field("", max_length=200)
    expect_text: str = Field("", max_length=2000)


def make_router(db, rag, min_score) -> APIRouter:
    """db — контекстный менеджер соединения, rag — модуль main, min_score — функция порога уверенности."""
    reindex_lock = threading.Lock()

    def require(request: Request, authorization: str = Header(default="")):
        token = os.getenv("ADMIN_TOKEN", "")
        if not token:
            raise HTTPException(503, "ADMIN_TOKEN не задан — админка выключена")
        ip = request.client.host if request.client else "?"
        now = time.time()
        _fails[ip] = [t for t in _fails[ip] if now - t < FAIL_WINDOW]
        if len(_fails[ip]) >= FAIL_LIMIT:
            raise HTTPException(429, "Слишком много попыток, подождите несколько минут")
        given = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            _fails[ip].append(now)
            raise HTTPException(401, "Неверный токен")

    router = APIRouter(prefix="/api/admin", dependencies=[Depends(require)])

    def conn():
        """Контекст с доступом к колонкам по имени."""
        class _Ctx:
            def __enter__(self):
                self.cm = db()
                self.con = self.cm.__enter__()
                self.con.row_factory = sqlite3.Row
                return self.con

            def __exit__(self, *a):
                return self.cm.__exit__(*a)
        return _Ctx()

    def reindex() -> int:
        with reindex_lock:
            return rag.build_index(force=True).count()

    # ── проверка токена ──
    @router.get("/ping")
    def ping():
        return {"ok": True}

    # ── входящие ──
    @router.get("/inbox")
    def inbox(status: Literal["new", "done", "ignored"] = "new", limit: int = 100):
        where = where_map()[status]
        with conn() as con:
            rows = con.execute(f"""
                SELECT m.id, m.ts, m.question, m.answer, m.top_score, m.confident,
                       f.value, f.reasons, f.comment, f.escalated
                FROM messages m
                LEFT JOIN feedback f ON f.message_id = m.id
                LEFT JOIN review r ON r.message_id = m.id
                WHERE (f.value IN ('dislike','report') OR m.confident = 0) AND {where}
                ORDER BY m.ts DESC LIMIT ?""", (max(1, min(limit, 300)),)).fetchall()
            counts = {s: con.execute(f"""
                SELECT COUNT(*) FROM messages m LEFT JOIN feedback f ON f.message_id = m.id
                LEFT JOIN review r ON r.message_id = m.id
                WHERE (f.value IN ('dislike','report') OR m.confident = 0) AND {w}""").fetchone()[0]
                for s, w in where_map().items()}
        return {"items": [_inbox_item(r) for r in rows], "counts": counts}

    def where_map():
        return {"new": "r.status IS NULL", "done": "r.status='done'", "ignored": "r.status='ignored'"}

    def _inbox_item(r) -> dict:
        reason = r["value"] if r["value"] in ("dislike", "report") else "lowconf"
        return {"id": r["id"], "ts": r["ts"], "question": r["question"], "answer": r["answer"],
                "top_score": r["top_score"], "reason": reason,
                "reasons": json.loads(r["reasons"] or "[]"), "comment": r["comment"] or "",
                "escalated": bool(r["escalated"])}

    @router.get("/inbox/{message_id}")
    def inbox_detail(message_id: str):
        with conn() as con:
            r = con.execute("""
                SELECT m.id, m.ts, m.session_id, m.question, m.answer, m.sources, m.top_score, m.confident,
                       f.value, f.reasons, f.comment, f.escalated
                FROM messages m LEFT JOIN feedback f ON f.message_id = m.id WHERE m.id=?""",
                            (message_id,)).fetchone()
            if not r:
                raise HTTPException(404, "Сообщение не найдено")
            prev = []
            if r["session_id"]:
                prev = [{"question": p["question"], "answer": p["answer"]} for p in con.execute(
                    "SELECT question, answer FROM messages WHERE session_id=? AND ts<? ORDER BY ts DESC LIMIT 3",
                    (r["session_id"], r["ts"])).fetchall()][::-1]
        item = _inbox_item(r)
        item["sources"] = json.loads(r["sources"] or "[]")
        item["previous"] = prev
        return item

    @router.put("/inbox/{message_id}/review")
    def set_review(message_id: str, body: ReviewIn):
        with conn() as con:
            if body.status == "new":
                con.execute("DELETE FROM review WHERE message_id=?", (message_id,))
            else:
                con.execute("INSERT OR REPLACE INTO review VALUES (?,?,?,?)",
                            (message_id, body.status, body.article_id, time.time()))
        return {"ok": True}

    # ── база знаний ──
    @router.get("/kb")
    def kb_list():
        with conn() as con:
            return {"items": kb.list_articles(con)}

    @router.get("/kb/{article_id}")
    def kb_get(article_id: int):
        with conn() as con:
            try:
                art = kb.get(con, article_id)
            except kb.NotFound:
                raise HTTPException(404, "Материал не найден")
            art["history"] = kb.history(con, article_id)
        return art

    @router.post("/kb")
    def kb_create(body: ArticleIn):
        with conn() as con:
            try:
                art = kb.create(con, body.title, body.body, body.kind, body.enabled)
            except kb.Duplicate:
                raise HTTPException(409, "Материал с таким названием уже есть")
        return {"article": art, "chunks": reindex()}

    @router.put("/kb/{article_id}")
    def kb_update(article_id: int, body: ArticleIn):
        with conn() as con:
            try:
                art = kb.update(con, article_id, body.title, body.body, body.kind, body.enabled)
            except kb.NotFound:
                raise HTTPException(404, "Материал не найден")
            except kb.Duplicate:
                raise HTTPException(409, "Материал с таким названием уже есть")
        return {"article": art, "chunks": reindex()}

    @router.delete("/kb/{article_id}")
    def kb_delete(article_id: int):
        with conn() as con:
            try:
                old = kb.delete(con, article_id)
            except kb.NotFound:
                raise HTTPException(404, "Материал не найден")
        return {"deleted": old, "chunks": reindex()}

    @router.post("/reindex")
    def reindex_now():
        return {"chunks": reindex()}

    # ── песочница ──
    @router.post("/sandbox")
    def sandbox(body: SandboxIn):
        hits = rag.retrieve(body.question, k=6)
        used = 4   # столько фрагментов чат кладёт в контекст
        out = {
            "min_score": min_score(),
            "hits": [{"topic": h["topic"], "kind": h.get("kind", "article"), "score": round(h["score"], 4),
                      "text": h["text"], "used": i < used} for i, h in enumerate(hits)],
            "prompt": rag.SYSTEM_PROMPT.format(context=rag.build_context(hits[:used])),
            "prompt_version": rag.PROMPT_VERSION, "model": rag.MODEL,
        }
        if body.generate:
            started = time.time()
            try:
                res = rag.answer(body.question, [])
            except Exception as e:
                raise HTTPException(502, f"Модель недоступна: {e}")
            out["answer"] = res["text"]
            out["products"] = [getattr(p, "name", str(p)) for p in (res.get("products") or [])]
            out["latency_ms"] = int((time.time() - started) * 1000)
        return out

    # ── контрольный набор ──
    @router.get("/eval")
    def eval_list():
        with conn() as con:
            rows = con.execute("SELECT id, question, expect_topic, expect_text FROM eval_cases ORDER BY id").fetchall()
        return {"items": [dict(r) for r in rows]}

    @router.post("/eval")
    def eval_create(body: EvalIn):
        with conn() as con:
            cur = con.execute("INSERT INTO eval_cases (question, expect_topic, expect_text, created_at)"
                              " VALUES (?,?,?,?)",
                              (body.question.strip(), body.expect_topic.strip(), body.expect_text.strip(), time.time()))
            return {"id": cur.lastrowid}

    @router.put("/eval/{case_id}")
    def eval_update(case_id: int, body: EvalIn):
        with conn() as con:
            con.execute("UPDATE eval_cases SET question=?, expect_topic=?, expect_text=? WHERE id=?",
                        (body.question.strip(), body.expect_topic.strip(), body.expect_text.strip(), case_id))
        return {"ok": True}

    @router.delete("/eval/{case_id}")
    def eval_delete(case_id: int):
        with conn() as con:
            con.execute("DELETE FROM eval_cases WHERE id=?", (case_id,))
        return {"ok": True}

    @router.post("/eval/{case_id}/run")
    def eval_run(case_id: int, answer: bool = False):
        with conn() as con:
            c = con.execute("SELECT * FROM eval_cases WHERE id=?", (case_id,)).fetchone()
        if not c:
            raise HTTPException(404, "Проверка не найдена")
        hits = rag.retrieve(c["question"], k=4)
        checks = []
        if c["expect_topic"]:
            ok = any(h["topic"] == c["expect_topic"] for h in hits)
            checks.append({"name": "Нужный материал найден", "ok": ok,
                           "detail": f"в выдаче {', '.join(dict.fromkeys(h['topic'] for h in hits)) or 'ничего'}"})
        res_text = None
        needles = [x.strip() for x in c["expect_text"].splitlines() if x.strip()]
        if needles:
            if not answer:
                checks.append({"name": "Ответ содержит факты", "ok": None, "detail": "пропущено: без вызова модели"})
            else:
                try:
                    res_text = rag.answer(c["question"], [])["text"]
                except Exception as e:
                    raise HTTPException(502, f"Модель недоступна: {e}")
                missing = [n for n in needles if n.casefold() not in res_text.casefold()]
                checks.append({"name": "Ответ содержит факты", "ok": not missing,
                               "detail": ("нет: " + "; ".join(missing)) if missing else "все на месте"})
        if not checks:
            checks.append({"name": "Условия не заданы", "ok": None, "detail": "добавьте материал или факты"})
        verdicts = [k["ok"] for k in checks if k["ok"] is not None]
        return {"id": case_id, "ok": (all(verdicts) if verdicts else None), "checks": checks, "answer": res_text}

    # ── статистика ──
    @router.get("/stats")
    def stats(days: int = 30):
        days = max(1, min(days, 365))
        since = time.time() - days * 86400
        with conn() as con:
            one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
            total = one("SELECT COUNT(*) FROM messages WHERE ts>=?", since)
            lowconf = one("SELECT COUNT(*) FROM messages WHERE ts>=? AND confident=0", since)
            fb = {v: one("SELECT COUNT(*) FROM feedback f JOIN messages m ON m.id=f.message_id"
                         " WHERE m.ts>=? AND f.value=?", since, v) for v in ("like", "dislike", "report")}
            csat = con.execute("SELECT AVG(score), COUNT(*) FROM csat WHERE ts>=?", (since,)).fetchone()
            latency = one("SELECT AVG(latency_ms) FROM messages WHERE ts>=?", since)
            top = con.execute("SELECT MIN(question) q, COUNT(*) n FROM messages WHERE ts>=? AND confident=0"
                              " GROUP BY lower(trim(question)) ORDER BY n DESC, MAX(ts) DESC LIMIT 10",
                              (since,)).fetchall()
            reasons: dict[str, int] = defaultdict(int)
            for (raw,) in con.execute("SELECT f.reasons FROM feedback f JOIN messages m ON m.id=f.message_id"
                                      " WHERE m.ts>=? AND f.value='dislike'", (since,)):
                for r in json.loads(raw or "[]"):
                    reasons[r] += 1
            daily = con.execute("""SELECT strftime('%Y-%m-%d', m.ts, 'unixepoch') d, COUNT(*) n,
                                   SUM(CASE WHEN f.value='dislike' THEN 1 ELSE 0 END) bad
                                   FROM messages m LEFT JOIN feedback f ON f.message_id=m.id
                                   WHERE m.ts>=? GROUP BY d ORDER BY d""", (since,)).fetchall()
        return {"days": days, "total": total, "lowconf": lowconf, **fb,
                "csat_avg": round(csat[0], 2) if csat[0] is not None else None, "csat_n": csat[1],
                "latency_ms": int(latency) if latency else None,
                "unanswered": [{"question": r["q"], "count": r["n"]} for r in top],
                "dislike_reasons": sorted(({"reason": k, "count": v} for k, v in reasons.items()),
                                          key=lambda x: -x["count"]),
                "daily": [{"day": r["d"], "count": r["n"], "bad": r["bad"] or 0} for r in daily]}

    return router
