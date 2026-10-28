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
from server.webhook_auth import dingtalk_signature, feishu_signature

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("BUTLER_TOKEN", raising=False)
    monkeypatch.delenv("DINGTALK_SIGN_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_SIGN_SECRET", raising=False)
    yield
    bus.clear_subscriptions()


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
