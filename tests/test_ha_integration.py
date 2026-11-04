"""Home Assistant REST API 双向桥接单元测试与容灾用例。"""
from __future__ import annotations

import httpx
import pytest

from skills import ha


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_ha_disabled_returns_not_available():
    client = ha.HomeAssistantClient(enabled=False, base_url="http://ha.local:8123", token="token123")
    res = await client.call_service("light", "turn_on", target={"entity_id": "light.test"})
    assert res["ok"] is False
    assert res["reason"] == "ha_disabled"


@pytest.mark.asyncio
async def test_ha_call_service_success():
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "content": request.read().decode("utf-8"),
        })
        return httpx.Response(200, json=[{"entity_id": "light.night_light", "state": "on"}])

    transport = _mock_transport(handle)
    client = ha.HomeAssistantClient(
        enabled=True,
        base_url="http://ha.test:8123",
        token="my-token",
        transport=transport,
    )

    res = await client.call_service(
        domain="light",
        service="turn_on",
        service_data={"brightness_pct": 30},
        target={"entity_id": "light.night_light"},
    )

    assert res["ok"] is True
    assert len(calls) == 1
    req = calls[0]
    assert req["method"] == "POST"
    assert req["url"] == "http://ha.test:8123/api/services/light/turn_on"
    assert "Bearer my-token" in req["headers"]["authorization"]
    import json
    data = json.loads(req["content"])
    assert data["brightness_pct"] == 30
    assert data["entity_id"] == "light.night_light"


@pytest.mark.asyncio
async def test_ha_get_state_and_set_state():
    states = {"binary_sensor.door_contact": {"state": "off", "attributes": {"friendly_name": "门磁"}}}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/states/binary_sensor.door_contact":
            return httpx.Response(200, json=states["binary_sensor.door_contact"])
        if request.method == "POST" and path == "/api/states/sensor.vmapbl_occupancy":
            return httpx.Response(200, json={"entity_id": "sensor.vmapbl_occupancy", "state": "on"})
        return httpx.Response(404, text="Not Found")

    transport = _mock_transport(handle)
    client = ha.HomeAssistantClient(
        enabled=True,
        base_url="http://ha.test:8123",
        token="my-token",
        transport=transport,
    )

    state = await client.get_state("binary_sensor.door_contact")
    assert state is not None
    assert state["state"] == "off"

    missing = await client.get_state("binary_sensor.nonexistent")
    assert missing is None

    set_res = await client.set_state("sensor.vmapbl_occupancy", "on", attributes={"confidence": 0.98})
    assert set_res["ok"] is True


@pytest.mark.asyncio
async def test_ha_network_timeout_shielded():
    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out")

    transport = _mock_transport(handle)
    client = ha.HomeAssistantClient(
        enabled=True,
        base_url="http://ha.test:8123",
        token="my-token",
        transport=transport,
    )

    res = await client.call_service("light", "turn_on", target={"entity_id": "light.night_light"})
    assert res["ok"] is False
    assert "timed out" in res["error"].lower()


@pytest.mark.asyncio
async def test_getup_night_light_ha_linkage(monkeypatch):
    """验证老人起夜动作成功调用 Home Assistant 小夜灯服务。"""
    from actions.getup import console_blink, night_light_on

    calls = []
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"entity_id": "light.night_light"}])

    client = ha.HomeAssistantClient(
        enabled=True,
        base_url="http://ha.test:8123",
        token="my-token",
        transport=_mock_transport(handle),
    )
    monkeypatch.setattr(ha, "default", lambda: client)

    res = await night_light_on({})
    assert res == "ha_light_on"
    assert "/api/services/light/turn_on" in calls

    # 当 HA 禁用时优雅回退
    client.enabled = False
    res_disabled = await console_blink({})
    assert res_disabled == "blinked"

