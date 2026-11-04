"""家人端 API/WebSocket token 与钉钉/飞书回调签名（安全加固）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app
from server.webhook_auth import (
    clear_nonce_cache, dingtalk_signature, feishu_signature, verify_webhook)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("BUTLER_TOKEN", raising=False)
    monkeypatch.delenv("DINGTALK_SIGN_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_SIGN_SECRET", raising=False)
    clear_nonce_cache()
    yield
    bus.clear_subscriptions()
    clear_nonce_cache()


def _app(tmp_path):
    st = StateStore()
    log = EventLog(tmp_path / "a.db")
    engine = SceneEngine(tmp_path / "scenes", st, log)
    return create_app(state=st, engine=engine, log=log)


async def test_api_stays_open_without_configured_token(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        assert (await c.get("/api/state")).status_code == 200


async def test_api_requires_bearer_when_token_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTLER_TOKEN", "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        assert (await c.get("/api/state")).status_code == 401
        assert (await c.get("/api/state", headers={
            "Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get("/api/state", headers={
            "Authorization": "Bearer s3cret"})).status_code == 200
        assert (await c.get("/api/state?token=s3cret")).status_code == 200
        # 健康探针保持公开，供 launchd/看门狗使用
        assert (await c.get("/healthz")).status_code == 200


def test_ws_requires_token_and_rejects_foreign_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTLER_TOKEN", "s3cret")
    client = TestClient(_app(tmp_path))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    with client.websocket_connect("/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                "/ws?token=s3cret", headers={"origin": "https://evil.example"}):
            pass


async def test_dingtalk_callback_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("DINGTALK_SIGN_SECRET", "dt-secret")
    events = []
    bus.subscribe("family.msg_to_elder", lambda ev: events.append(ev))
    payload = json.dumps({"text": "妈，吃饭了吗", "senderNick": "儿子"},
                         ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time() * 1000))
    headers = {"Content-Type": "application/json", "timestamp": ts}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        bad = await c.post("/api/webhook/family_reply", content=payload,
                           headers={**headers, "sign": "wrong"})
        assert bad.status_code == 401
        good = await c.post("/api/webhook/family_reply", content=payload,
                            headers={**headers,
                                     "sign": dingtalk_signature("dt-secret", ts)})
        assert good.status_code == 200
    # 失败的请求不得产生任何播报事件
    assert len(events) == 1


async def test_stream_gateway_signed_message_routes_only_once(tmp_path, monkeypatch):
    from tools.dingtalk_stream_gateway import compute_dingtalk_headers

    monkeypatch.setenv("BUTLER_TOKEN", "test-console-token")
    monkeypatch.setenv("DINGTALK_SIGN_SECRET", "test-stream-secret")
    events = []
    bus.subscribe("family.msg_to_elder", lambda event: events.append(event))
    body = json.dumps({"text": "这是一条本地测试留言"}).encode()
    headers = compute_dingtalk_headers("test-stream-secret", body)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as client:
        path = "/api/webhook/family_reply"
        assert (await client.post(path, content=body + b" ", headers=headers)).status_code == 401
        assert (await client.post(path, content=body, headers=headers)).status_code == 200
        assert (await client.post(path, content=body, headers=headers)).status_code == 401
    assert len(events) == 1


async def test_dingtalk_callback_rejects_stale_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("DINGTALK_SIGN_SECRET", "dt-secret")
    events = []
    bus.subscribe("family.msg_to_elder", lambda ev: events.append(ev))
    ts = str(int((time.time() - 600) * 1000))  # 10 分钟前
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply",
                            json={"text": "妈，吃饭了吗"},
                            headers={"timestamp": ts,
                                     "sign": dingtalk_signature("dt-secret", ts)})
        assert resp.status_code == 401
    assert events == []


async def test_dingtalk_callback_missing_signature_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DINGTALK_SIGN_SECRET", "dt-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply", json={"text": "妈，吃饭了吗"})
        assert resp.status_code == 401


async def test_feishu_callback_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_SIGN_SECRET", "fs-secret")
    events = []
    bus.subscribe("family.msg_to_elder", lambda ev: events.append(ev))
    payload = json.dumps({"text": {"content": "饭菜在锅里"}},
                         ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    nonce = "n123"
    headers = {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": feishu_signature("fs-secret", ts, nonce, payload),
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        good = await c.post("/api/webhook/family_reply", content=payload,
                            headers=headers)
        assert good.status_code == 200
        bad = await c.post("/api/webhook/family_reply", content=payload,
                           headers={**headers, "X-Lark-Signature": "bad"})
        assert bad.status_code == 401
    assert len(events) == 1


async def test_feishu_url_verification_with_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_SIGN_SECRET", "fs-secret")
    payload = json.dumps({"type": "url_verification",
                          "challenge": "challenge-123"}).encode("utf-8")
    ts = str(int(time.time()))
    nonce = "n1"
    headers = {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": feishu_signature("fs-secret", ts, nonce, payload),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply", content=payload,
                            headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "challenge-123"}


@pytest.mark.parametrize("headers,secrets", [
    ({"timestamp": "1"}, {"feishu_secret": "configured-secret"}),
    ({"x-lark-signature": "unsigned"}, {"dingtalk_secret": "configured-secret"}),
])
def test_other_platform_headers_cannot_bypass_configured_signature(
        headers, secrets):
    ok, _reason = verify_webhook(
        headers, '{"text":"伪造留言"}'.encode("utf-8"), **secrets)
    assert ok is False


async def test_webhook_fails_closed_when_butler_token_set_and_secrets_unset(
        tmp_path, monkeypatch):
    monkeypatch.setenv("BUTLER_TOKEN", "prod-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply",
                            json={"text": "伪造消息"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "webhook_secrets_unconfigured"


def test_webhook_replay_protection_rejects_duplicate():
    """验证重放相同请求被拒绝（AUDIT-020）。"""
    secret = "test-secret"
    ts = str(int(time.time()))
    nonce = "unique-nonce-1"
    body = b'{"text":"hello"}'
    sig = feishu_signature(secret, ts, nonce, body)
    headers = {
        "x-lark-signature": sig,
        "x-lark-request-timestamp": ts,
        "x-lark-request-nonce": nonce,
    }

    # 首次校验成功
    ok1, reason1 = verify_webhook(headers, body, feishu_secret=secret)
    assert ok1 is True
    assert reason1 == "ok"

    # 重放相同请求被拦截
    ok2, reason2 = verify_webhook(headers, body, feishu_secret=secret)
    assert ok2 is False
    assert reason2 == "feishu_request_replayed"


async def test_cross_origin_mutation_rejected(tmp_path):
    """验证非法来源的跨域 POST 请求被 403 拦截（AUDIT-019）。"""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.post("/api/msg_to_elder", json={"text": "测试"},
                            headers={"Origin": "https://malicious-site.com"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "cross_origin_forbidden"
