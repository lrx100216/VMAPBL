"""外部通知平台回执、有限重试和旧 send 契约。"""
import asyncio

import httpx
import pytest

from core import config
from core.log import EventLog, set_default
from skills import notify


@pytest.fixture
def delivery_env(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "delivery.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 0.01, "max_attempts": 3, "retry_backoff_s": 0}
    })
    yield log
    set_default(None)
    log.close()


def _urls(monkeypatch, *, dingtalk=False, feishu=False):
    monkeypatch.setenv("DINGTALK_WEBHOOK",
                       "https://oapi.dingtalk.com/robot/send" if dingtalk else "")
    monkeypatch.setenv("FEISHU_WEBHOOK",
                       "https://open.feishu.cn/bot" if feishu else "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")


async def test_dingtalk_business_error_is_not_delivered(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"errcode": 310000})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.attempted is True
    assert result.delivered is False
    assert result.channels["dingtalk"] == "failed"
    assert len(calls) == 1  # 明确的业务拒绝不重试
    assert delivery_env.recent()[0]["kind"] == "notify_failed"


async def test_network_failure_retries_then_succeeds(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("模拟超时")
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is True
    assert result.channels["dingtalk"] == "delivered"
    assert len(calls) == 3
    assert delivery_env.recent()[0]["kind"] == "notify_delivered"


async def test_server_error_retries_then_succeeds(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            return httpx.Response(503, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is True
    assert len(calls) == 3


async def test_http_client_error_does_not_retry(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(400, json={"errcode": 400})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is False
    assert len(calls) == 1


async def test_one_successful_channel_marks_delivery(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True, feishu=True)

    async def fake_post(self, url, **kwargs):
        if "dingtalk" in str(url):
            return httpx.Response(200, json={"errcode": 310000})
        return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is True
    assert result.channels == {"dingtalk": "failed", "feishu": "delivered"}


async def test_no_channel_is_not_external_delivery(delivery_env, monkeypatch):
    _urls(monkeypatch)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.msg_id is not None
    assert result.attempted is False
    assert result.delivered is False


async def test_old_send_contract_survives_external_failure(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)

    async def fake_post(self, url, **kwargs):
        raise httpx.ConnectTimeout("模拟超时")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    msg_id = await notify.send("普通消息", wait_dispatch=True)
    assert isinstance(msg_id, int)  # 旧调用方仍获得本地消息 ID


async def test_redirect_with_success_body_is_not_delivered(
        delivery_env, monkeypatch):
    """3xx 不是平台确认；即使响应体长得像成功也不能武装跌倒静默。"""
    _urls(monkeypatch, dingtalk=True)

    async def fake_post(self, url, **kwargs):
        return httpx.Response(302, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is False
    assert result.channels["dingtalk"] == "failed"


async def test_boolean_zero_code_is_not_delivered(delivery_env, monkeypatch):
    """`false == 0` 在 Python 中成立，但畸形回执必须按失败处理。"""
    _urls(monkeypatch, dingtalk=True)

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json={"errcode": False})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is False


async def test_no_channel_records_failed_delivery_audit(
        delivery_env, monkeypatch):
    """未配置渠道时也要留下“未外部送达”的审计记录。"""
    _urls(monkeypatch)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is False
    assert delivery_env.recent()[0]["kind"] == "notify_failed"
    assert delivery_env.recent()[0]["detail"]["channels"] == {}


async def test_receipt_trims_message_like_legacy_send(
        delivery_env, monkeypatch):
    """本地落库与外发必须使用同一份去空白文本。"""
    _urls(monkeypatch, dingtalk=True)
    sent_payloads = []

    async def fake_post(self, url, **kwargs):
        sent_payloads.append(kwargs["json"]["text"]["content"])
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("  疑似跌倒  ", urgent=True)
    assert result.delivered is True
    assert delivery_env.list_messages()[0]["text"] == "疑似跌倒"
    assert sent_payloads == ["🚨【紧急预警】疑似跌倒"]


async def test_legacy_send_is_still_fire_and_forget(
        delivery_env, monkeypatch):
    """旧 send 默认不得等待重试链；后台任务可被 wait_pending 排空。"""
    _urls(monkeypatch, dingtalk=True)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_post(self, url, **kwargs):
        started.set()
        await gate.wait()
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    msg_id = await notify.send("普通消息")
    assert isinstance(msg_id, int)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    gate.set()
    await notify.wait_pending()


async def test_invalid_retry_config_reports_no_attempt(
        delivery_env, monkeypatch):
    """配置错误导致请求未发出时，回执不得声称已经尝试。"""
    _urls(monkeypatch, dingtalk=True)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 0.01, "max_attempts": "bad", "retry_backoff_s": 0}
    })

    async def fake_post(self, url, **kwargs):
        raise AssertionError("配置解析失败时不得发请求")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.attempted is False
    assert result.delivered is False
