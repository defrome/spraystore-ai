import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import main as rag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_INFO_MARK = "нет такой информации"
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.80"))
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]


@asynccontextmanager
async def lifespan(_: FastAPI):
    rag.build_index(force=False)
    yield


app = FastAPI(title="SprayStore RAG", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


class HistoryItem(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    message: str
    history: list[HistoryItem] = []


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
    return {"ok": True, "chunks": rag.get_collection().count()}


@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        res = rag.answer(req.message, to_llm_history(req.history, req.message))
    except Exception as e:
        print(f"[chat] ошибка: {e!r}")
        return {"text": "Сейчас не получается ответить. Давайте подключу специалиста.",
                "confident": False, "forceHandoff": True}
    top = max((h["score"] for h in res["hits"]), default=0.0)
    confident = NO_INFO_MARK not in res["text"].lower() and top >= MIN_SCORE
    return {"text": res["text"], "confident": confident}


# фронт отдаём с того же origin, чтобы fetch('/api/chat') работал без CORS.
# Отдаём только страницы, а не всю папку репо — иначе наружу уйдёт service/.env.
@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "index.html"))


@app.get("/embed-example.html")
def embed_example():
    return FileResponse(os.path.join(ROOT, "embed-example.html"))
