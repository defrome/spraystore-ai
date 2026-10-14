"""База знаний в SQLite: статьи и эталонные пары «вопрос — ответ». Правится из /admin.

Модуль работает с переданным соединением и ничего не знает про FastAPI и Chroma.
"""
import textwrap
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'article', enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS kb_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER NOT NULL, title TEXT, body TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS review (
    message_id TEXT PRIMARY KEY, status TEXT NOT NULL, article_id INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS eval_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, expect_topic TEXT, expect_text TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

KINDS = ("article", "qa")
HISTORY_KEEP = 20


class Duplicate(Exception):
    pass


class NotFound(Exception):
    pass


def seed(con, base: dict) -> int:
    """Один раз заливаем встроенную базу. Если потом всё удалили — повторно не заливаем."""
    if con.execute("SELECT 1 FROM meta WHERE key='kb_seeded'").fetchone():
        return 0
    now = time.time()
    n = 0
    if con.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0] == 0:
        for title, body in base.items():
            con.execute("INSERT INTO kb_articles (title, body, kind, enabled, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?)", (title, textwrap.dedent(body).strip(), "article", 1, now, now))
            n += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('kb_seeded', '1')")
    return n


def _row(r) -> dict:
    return {"id": r["id"], "title": r["title"], "body": r["body"], "kind": r["kind"],
            "enabled": bool(r["enabled"]), "updated_at": r["updated_at"]}


def provider(con_factory):
    """Функция для main.KB_PROVIDER: только включённые материалы."""
    def load():
        with con_factory() as con:
            rows = con.execute("SELECT id, title, body, kind, enabled, updated_at FROM kb_articles"
                               " WHERE enabled=1 ORDER BY id").fetchall()
        return [{"id": r[0], "title": r[1], "body": r[2], "kind": r[3]} for r in rows]
    return load


def list_articles(con) -> list[dict]:
    rows = con.execute("SELECT id, title, body, kind, enabled, updated_at FROM kb_articles"
                       " ORDER BY kind, title COLLATE NOCASE").fetchall()
    return [{"id": r[0], "title": r[1], "kind": r[3], "enabled": bool(r[4]), "updated_at": r[5],
             "size": len(r[2]), "preview": " ".join(r[2].split())[:140]} for r in rows]


def get(con, article_id: int) -> dict:
    r = con.execute("SELECT id, title, body, kind, enabled, updated_at FROM kb_articles WHERE id=?",
                    (article_id,)).fetchone()
    if not r:
        raise NotFound(article_id)
    return {"id": r[0], "title": r[1], "body": r[2], "kind": r[3], "enabled": bool(r[4]), "updated_at": r[5]}


def create(con, title: str, body: str, kind: str = "article", enabled: bool = True) -> dict:
    now = time.time()
    try:
        cur = con.execute("INSERT INTO kb_articles (title, body, kind, enabled, created_at, updated_at)"
                          " VALUES (?,?,?,?,?,?)", (title.strip(), body.strip(), kind, int(enabled), now, now))
    except Exception as e:   # sqlite3.IntegrityError
        if "UNIQUE" in str(e):
            raise Duplicate(title) from e
        raise
    return get(con, cur.lastrowid)


def update(con, article_id: int, title: str, body: str, kind: str, enabled: bool) -> dict:
    old = get(con, article_id)
    if (old["title"], old["body"]) != (title.strip(), body.strip()):
        con.execute("INSERT INTO kb_history (article_id, title, body, ts) VALUES (?,?,?,?)",
                    (article_id, old["title"], old["body"], time.time()))
        con.execute("DELETE FROM kb_history WHERE article_id=? AND id NOT IN"
                    " (SELECT id FROM kb_history WHERE article_id=? ORDER BY id DESC LIMIT ?)",
                    (article_id, article_id, HISTORY_KEEP))
    try:
        con.execute("UPDATE kb_articles SET title=?, body=?, kind=?, enabled=?, updated_at=? WHERE id=?",
                    (title.strip(), body.strip(), kind, int(enabled), time.time(), article_id))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise Duplicate(title) from e
        raise
    return get(con, article_id)


def delete(con, article_id: int) -> dict:
    old = get(con, article_id)
    con.execute("DELETE FROM kb_articles WHERE id=?", (article_id,))
    con.execute("DELETE FROM kb_history WHERE article_id=?", (article_id,))
    return old


def history(con, article_id: int) -> list[dict]:
    rows = con.execute("SELECT id, title, body, ts FROM kb_history WHERE article_id=? ORDER BY id DESC",
                       (article_id,)).fetchall()
    return [{"id": r[0], "title": r[1], "body": r[2], "ts": r[3]} for r in rows]
