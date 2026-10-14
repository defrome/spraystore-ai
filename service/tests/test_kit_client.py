import httpx
import pytest

import kit_client
from kit_client import KitClient, KitDisabled, KitError, _Throttle

TOKEN = "SECRET-TOKEN-123"


def make(handler, **kw):
    sleeps = []
    c = KitClient(token=TOKEN, transport=httpx.MockTransport(handler),
                  throttle=kw.pop("throttle", _Throttle(1000)), sleep=sleeps.append, **kw)
    return c, sleeps


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("KIT_API_TOKEN", raising=False)
    assert not kit_client.is_enabled()
    with pytest.raises(KitDisabled):
        KitClient()


def test_get_only_and_bearer():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": 1})

    c, _ = make(h)
    c.get("/v1/store")
    assert [r.method for r in seen] == ["GET"]
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    # в клиенте нет методов для изменяющих запросов
    for name in ("post", "put", "patch", "delete", "request", "send"):
        assert not hasattr(c, name)
    with pytest.raises(ValueError):
        c.get("/v2/anything")


def test_retry_on_429_and_5xx_then_success():
    codes = iter([429, 500, 503, 200])

    def h(req):
        code = next(codes)
        return httpx.Response(code, json={"ok": 1} if code == 200 else {"code": "X", "message": "m", "trace_id": "t"},
                              headers={"Retry-After": "1"} if code == 429 else {})

    c, sleeps = make(h)
    assert c.get("/v1/store") == {"ok": 1}
    assert len(sleeps) == 3 and sleeps[0] >= 1       # Retry-After учтён


def test_no_retry_on_4xx_and_trace_id():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(401, json={"code": "AUTHENTICATION_ERROR", "message": "bad", "trace_id": "abc123"})

    c, _ = make(h)
    with pytest.raises(KitError) as ei:
        c.get("/v1/store")
    assert len(calls) == 1
    assert ei.value.trace_id == "abc123" and ei.value.code == "AUTHENTICATION_ERROR"
    assert "abc123" in str(ei.value)


def test_gives_up_after_retries_and_token_not_in_exception():
    def h(req):
        return httpx.Response(500, json={"code": "UNKNOWN_ERROR", "message": "boom", "trace_id": "tt"})

    c, _ = make(h)
    with pytest.raises(KitError) as ei:
        c.get("/v1/store")
    assert ei.value.status == 500 and TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)


def test_network_error_retried_and_sanitized():
    def h(req):
        raise httpx.ConnectError("fail", request=req)

    c, sleeps = make(h)
    with pytest.raises(KitError) as ei:
        c.get("/v1/store")
    assert TOKEN not in str(ei.value) and len(sleeps) == kit_client.MAX_RETRIES


def test_throttle_spaces_requests():
    t = _Throttle(3)
    import time
    start = time.monotonic()
    for _ in range(4):
        t.wait()
    assert time.monotonic() - start >= 3 / 3 - 0.05    # 4 вызова при 3 rps — не быстрее ~1 с


def test_iter_pages():
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": i} for i in range(100, 130)]}
    queries = []

    def h(req):
        q = dict(req.url.params)
        queries.append(q)
        return httpx.Response(200, json={"items": pages[int(q["page"])]})

    c, _ = make(h)
    assert len(list(c.iter_pages("/v1/things", "items", status="ACTIVE"))) == 130
    assert queries[0]["per_page"] == "100" and queries[0]["status"] == "ACTIVE" and len(queries) == 2


def test_iter_pages_repeats_list_param():
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"items": []})

    c, _ = make(h)
    list(c.iter_pages("/v1/variants", "items", status=["PUBLISHED"]))
    assert "status=PUBLISHED" in seen[0]
