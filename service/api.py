import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import admin
import catalog
import catalog_chat
import kb
import main as rag

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DB = os.getenv("CHAT_LOG_DB", os.path.join(HERE, "chat_log.db"))
NO_INFO_MARK = "нет такой информации"
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.80"))
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, session_id TEXT, ts REAL, question TEXT, answer TEXT,
    model TEXT, prompt_version TEXT, sources TEXT, top_score REAL, confident INTEGER, latency_ms INTEGER,
    products TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    message_id TEXT PRIMARY KEY, session_id TEXT, value TEXT, reasons TEXT, comment TEXT,
    escalated INTEGER, user_msgs_before INTEGER, rated_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS csat (
    session_id TEXT PRIMARY KEY, score INTEGER, ts REAL
);
""" + kb.SCHEMA


@contextmanager
def db():
    con = sqlite3.connect(LOG_DB, timeout=10)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def migrate(con):
    """Безопасная миграция уже созданной БД: добавляем новые nullable-колонки."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    if "products" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN products TEXT")   # id рекомендованных товаров (JSON)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with db() as con:
        con.executescript(SCHEMA)
        migrate(con)
        kb.seed(con, rag.KNOWLEDGE_BASE)   # первый запуск: встроенная база попадает в БД и дальше правится в /admin
    rag.build_index(force=True)           # индекс всегда соответствует базе
    catalog.start_background_sync()   # без KIT_API_TOKEN ничего не делает
    yield
    catalog.stop_background_sync()


rag.KB_PROVIDER = kb.provider(db)   # статьи берём из БД, а не из словаря в коде

app = FastAPI(title="SprayStore RAG", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


app.include_router(admin.make_router(db, rag, lambda: MIN_SCORE))


class HistoryItem(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    history: list[HistoryItem] = []


class FeedbackIn(BaseModel):
    message_id: str
    session_id: str | None = None
    value: Literal["like", "dislike", "report"] | None = None   # None — оценку сняли
    reasons: list[str] = Field(default_factory=list, max_length=10)
    comment: str = Field("", max_length=1000)
    escalated: bool = False
    user_msgs_before: int | None = None


class CsatIn(BaseModel):
    session_id: str
    score: int = Field(ge=1, le=5)


def to_llm_history(history: list[HistoryItem], message: str) -> list[dict]:
    """История фронта -> формат OpenAI. Системные сообщения и реплики оператора отбрасываем."""
    roles = {"user": "user", "bot": "assistant"}
    items = [{"role": roles[h.role], "content": h.text} for h in history if h.role in roles]
    # фронт уже добавил текущий вопрос в историю — он уйдёт отдельным сообщением
    if items and items[-1]["role"] == "user" and items[-1]["content"] == message:
        items.pop()
    return items[-8:]


@app.get("/api/health")
def health():
    return {"ok": True, "chunks": rag.get_collection().count(), "catalog": {**catalog.status(), "products": catalog.count()}}


@app.post("/api/chat")
def chat(req: ChatRequest):
    started = time.time()
    try:
        res = rag.answer(req.message, to_llm_history(req.history, req.message))
    except Exception as e:
        print(f"[chat] ошибка: {e!r}")
        raise HTTPException(502, "llm unavailable")   # фронт покажет «Не удалось получить ответ» + «Повторить»

    hits = res["hits"]
    top = max((h["score"] for h in hits), default=0.0)
    # карточки — только товары, которые модель реально назвала в ответе
    products = catalog_chat.mentioned_products(res["text"], res.get("products") or [])
    # уверенность: статьи нашлись ИЛИ ответ опирается на товары каталога
    confident = NO_INFO_MARK not in res["text"].lower() and (top >= MIN_SCORE or bool(products))
    sources = []
    if confident:
        for h in hits:
            if h["score"] >= MIN_SCORE - 0.05 and h["topic"] not in [x["title"] for x in sources]:
                sources.append({"title": h["topic"]})
        sources = sources[:3]

    message_id = str(uuid.uuid4())
    try:
        with db() as con:
            con.execute(
                "INSERT INTO messages (id, session_id, ts, question, answer, model, prompt_version, sources,"
                " top_score, confident, latency_ms, products) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, req.session_id, started, req.message, res["text"], rag.MODEL, rag.PROMPT_VERSION,
                 json.dumps([h["topic"] for h in hits], ensure_ascii=False), top, int(confident),
                 int((time.time() - started) * 1000),
                 json.dumps([p.id for p in products]) if products else None),
            )
    except Exception as e:   # лог не должен ронять ответ
        print(f"[log] не записали сообщение: {e!r}")
    out = {"text": res["text"], "confident": confident, "message_id": message_id, "sources": sources}
    if products:   # поле cards — только когда есть товары; формат {id, name, price, url, image?, in_stock}
        out["cards"] = [p.to_card() for p in products]
    return out


@app.post("/api/feedback")
def feedback(fb: FeedbackIn):
    now = time.time()
    with db() as con:
        if fb.value is None:
            con.execute("DELETE FROM feedback WHERE message_id = ?", (fb.message_id,))
        else:
            con.execute(
                """INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET value=excluded.value, reasons=excluded.reasons,
                   comment=excluded.comment, escalated=excluded.escalated, updated_at=excluded.updated_at""",
                (fb.message_id, fb.session_id, fb.value, json.dumps(fb.reasons, ensure_ascii=False), fb.comment,
                 int(fb.escalated), fb.user_msgs_before, now, now),
            )
    return {"ok": True}


@app.post("/api/csat")
def csat(c: CsatIn):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO csat VALUES (?,?,?)", (c.session_id, c.score, time.time()))
    return {"ok": True}


# фронт отдаём с того же origin, чтобы fetch('/api/chat') работал без CORS.
# Отдаём только страницы, а не всю папку репо — иначе наружу уйдёт service/.env.
@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "index.html"))


@app.get("/embed-example.html")
def embed_example():
    return FileResponse(os.path.join(ROOT, "embed-example.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(ROOT, "admin.html"), headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store"})
