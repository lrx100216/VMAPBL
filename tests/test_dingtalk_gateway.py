"""单元测试：钉钉 Stream 网关工具与签名转发逻辑。"""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from server import webhook_auth
from tools import dingtalk_stream_gateway


def test_gateway_instance_lock_excludes_other_process_and_recovers(tmp_path):
    path = tmp_path / "gateway.lock"
    script = ("from pathlib import Path; from tools.dingtalk_stream_gateway "
              "import gateway_instance_lock; import sys; "
              "ctx=gateway_instance_lock(Path(sys.argv[1])); ctx.__enter__()")
    with dingtalk_stream_gateway.gateway_instance_lock(path):
        blocked = subprocess.run([sys.executable, "-c", script, str(path)],
                                 capture_output=True, text=True)
        assert blocked.returncode != 0
        assert "钉钉网关已在运行" in blocked.stderr
    after = subprocess.run([sys.executable, "-c", script, str(path)],
                           capture_output=True, text=True)
    assert after.returncode == 0, after.stderr


def test_compute_dingtalk_headers_unsigned():
    headers = dingtalk_stream_gateway.compute_dingtalk_headers(None, b"{}")
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert "X-VMAPBL-Stream-Signature" not in headers


def test_compute_dingtalk_headers_signed():
    secret = "test_dingtalk_secret_123"
    headers = dingtalk_stream_gateway.compute_dingtalk_headers(secret, b"{}")
    assert "X-VMAPBL-Stream-Signature" in headers
    assert webhook_auth.verify_webhook(
        headers, b'{"text":"changed"}', dingtalk_secret=secret
    ) == (False, "stream_signature_mismatch")
    ok, reason = webhook_auth.verify_webhook(
        headers, b"{}", dingtalk_secret=secret
    )
    assert ok is True, f"Webhook 验签应当成功，实际理由: {reason}"
    assert webhook_auth.verify_webhook(
        headers, b"{}", dingtalk_secret=secret
    ) == (False, "stream_request_replayed")


def test_dingtalk_stream_forwarder_empty_message():
    forwarder = dingtalk_stream_gateway.DingTalkStreamForwarder(
        target_url="http://127.0.0.1:8321/api/webhook/family_reply"
    )
    res = forwarder.handle_bot_message({"text": {"content": "   "}})
    assert res["status"] == "ignored_empty"


def test_dingtalk_stream_forwarder_forward_mock():
    forwarder = dingtalk_stream_gateway.DingTalkStreamForwarder(
        target_url="http://127.0.0.1:8321/api/webhook/family_reply",
        secret="sec_test",
    )

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = forwarder.handle_bot_message({
            "text": {"content": "奶奶，今天降温记得添衣服！"},
            "senderNick": "大女儿",
        })

        assert res["ok"] is True
        assert res["status"] == 200
        # 验证发送的 request
        call_args, call_kwargs = mock_urlopen.call_args
        req = call_args[0]
        assert req.full_url == "http://127.0.0.1:8321/api/webhook/family_reply"
        data = json.loads(req.data.decode("utf-8"))
        assert data["text"] == "奶奶，今天降温记得添衣服！"
        assert data["sender"] == "大女儿"


@pytest.mark.parametrize(("response", "expected"), [
    ({"ok": True}, 200),
    ({"ok": False}, 500),
    ({"status": "ignored_empty"}, 200),
])
async def test_stream_sdk_callback_reports_real_forwarding_result(monkeypatch, response, expected):
    client = MagicMock()
    sdk = SimpleNamespace(
        AckMessage=SimpleNamespace(STATUS_OK=200, STATUS_SYSTEM_EXCEPTION=500),
        ChatbotHandler=object,
        Credential=MagicMock(),
        DingTalkStreamClient=MagicMock(return_value=client),
    )
    monkeypatch.setitem(sys.modules, "dingtalk_stream", sdk)
    monkeypatch.setitem(sys.modules, "dingtalk_stream.chatbot",
                        SimpleNamespace(ChatbotMessage=SimpleNamespace(TOPIC="/robot")))
    monkeypatch.setattr(dingtalk_stream_gateway.DingTalkStreamForwarder,
                        "handle_bot_message", lambda self, data: response)
    dingtalk_stream_gateway.run_stream_client("app", "secret", "http://localhost", "sign")
    sdk.Credential.assert_called_once_with("app", "secret")
    topic, handler = client.register_callback_handler.call_args.args
    assert topic == "/robot"
    code, _ = await handler.process(SimpleNamespace(data={"text": "测试"}))
    assert code == expected
