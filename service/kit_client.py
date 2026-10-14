"""Тонкий READ-ONLY клиент Яндекс KIT API.

Безопасность: токен KIT даёт полный доступ (удаление, отмена заказов, массовое обновление цен),
поэтому в клиенте есть ТОЛЬКО GET: метода для других HTTP-глаголов нет, а единственная
точка выхода `get()` вызывает request("GET", ...) с жёстко заданным глаголом. Токен читается из env KIT_API_TOKEN
и нигде не попадает в логи и тексты исключений.
"""
import os
import threading
import time
from typing import Iterator

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

BASE_URL = "https://api.kit.yandex.net"
RATE_LIMIT_RPS = 3          # лимит KIT: 3 запроса/сек на магазин
MAX_RETRIES = 4
PER_PAGE_MAX = 100
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class KitDisabled(Exception):
    """Нет KIT_API_TOKEN — каталог выключен. Вызывающий код ловит и работает без каталога."""


class KitError(Exception):
    """Ошибка KIT: {message, code, trace_id}. Сообщение собираем сами — без заголовков и токена."""

    def __init__(self, status: int, code: str = "", message: str = "", trace_id: str = ""):
        self.status, self.code, self.message, self.trace_id = status, code, message, trace_id
        super().__init__(f"KIT {status} {code}: {message} (trace_id={trace_id or '-'})")


class _Throttle:
    """Общий для процесса ограничитель частоты: не чаще rps запросов в секунду, потокобезопасный."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


# один троттлер на процесс: лимит считается на магазин, а не на экземпляр клиента
_THROTTLE = _Throttle(RATE_LIMIT_RPS)


def get_token() -> str | None:
    return (os.environ.get("KIT_API_TOKEN") or "").strip() or None


def is_enabled() -> bool:
    return get_token() is not None


class KitClient:
    def __init__(self, token: str | None = None, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None, throttle: _Throttle | None = None,
                 sleep=time.sleep):
        token = token or get_token()
        if not token:
            raise KitDisabled("KIT_API_TOKEN не задан — каталог выключен")
        self._throttle = throttle or _THROTTLE
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url, timeout=TIMEOUT, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    # ---- единственный способ ходить в KIT ----
    def get(self, path: str, **params) -> dict:
        """GET /v1/... -> JSON. Ретраи на 429/5xx и сетевые сбои с экспоненциальным backoff."""
        if not path.startswith("/v1/"):
            raise ValueError("только пути /v1/...")
        params = {k: v for k, v in params.items() if v is not None}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._throttle.wait()
            try:
                r = self._http.request("GET", path, params=params)
            except httpx.TransportError as e:
                # в текст кладём только тип ошибки: httpx не светит заголовки, но перестрахуемся
                last = KitError(0, "NETWORK", type(e).__name__)
                self._backoff(attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = self._error(r)
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                raise self._error(r)    # 400/401/403/404 — ретраить бессмысленно
            try:
                return r.json()
            except ValueError:
                raise KitError(r.status_code, "BAD_JSON", "ответ не JSON")
        assert last is not None
        raise last

    def iter_pages(self, path: str, items_key: str, per_page: int = PER_PAGE_MAX, **params) -> Iterator[dict]:
        """Обходит страницы (page с 1, per_page до 100) и отдаёт элементы списка items_key.
        Конец — пустая или неполная страница (у части ресурсов нет total_count)."""
        per_page = min(per_page, PER_PAGE_MAX)
        page = 1
        while True:
            data = self.get(path, page=page, per_page=per_page, **params)
            items = data.get(items_key) or []
            yield from items
            total = data.get("total_count")
            if len(items) < per_page or (isinstance(total, int) and page * per_page >= total):
                return
            page += 1

    def _backoff(self, attempt: int, retry_after: str | None = None):
        delay = min(0.5 * 2 ** attempt, 8.0)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 30.0))
            except ValueError:
                pass
        if attempt < MAX_RETRIES:
            self._sleep(delay)

    @staticmethod
    def _error(r: httpx.Response) -> KitError:
        try:
            j = r.json()
            return KitError(r.status_code, str(j.get("code", "")), str(j.get("message", ""))[:300],
                            str(j.get("trace_id", "")))
        except Exception:
            return KitError(r.status_code, "", "ошибка без JSON-тела")

    def close(self):
        self._http.close()


_client: KitClient | None = None
_client_lock = threading.Lock()


def get_client() -> KitClient:
    """Общий клиент процесса. Без токена — KitDisabled."""
    global _client
    with _client_lock:
        if _client is None:
            _client = KitClient()
        return _client
