"""单元测试：智能家电控制意图解析、场景执行与 Home Assistant 联动。"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from actions import appliance
from skills import ha, intent


def test_parse_appliance_intent_climate_variations():
    # 1. 太热了 -> 制冷
    p1 = appliance.parse_appliance_intent("太热了")
    assert p1["domain"] == "climate"
    assert p1["service"] == "set_hvac_mode"
    assert p1["service_data"]["hvac_mode"] == "cool"

    # 2. 精确设置温度
    p2 = appliance.parse_appliance_intent("把空调调到25度")
    assert p2["domain"] == "climate"
    assert p2["service"] == "set_temperature"
    assert p2["service_data"]["temperature"] == 25.0

    # 3. 适老安全温度钳制（16度 -> 钳制为22度）
    p3 = appliance.parse_appliance_intent("空调调到16度")
    assert p3["service_data"]["temperature"] == 22.0

    # 4. 适老安全温度钳制（32度 -> 钳制为28度）
    p4 = appliance.parse_appliance_intent("空调太冷了调到32度")
    assert p4["service_data"]["temperature"] == 28.0

    # 5. 关闭空调
    p5 = appliance.parse_appliance_intent("把空调关了")
    assert p5["domain"] == "climate"
    assert p5["service"] == "turn_off"

    # 6. 查询状态
    p6 = appliance.parse_appliance_intent("空调现在开着吗")
    assert p6["domain"] == "climate"
    assert p6["intent"] == "query"


def test_parse_appliance_intent_lighting_and_fan():
    # 灯光打开与关闭
    p_light_on = appliance.parse_appliance_intent("帮我把灯打开")
    assert p_light_on["domain"] == "light"
    assert p_light_on["service"] == "turn_on"
    assert p_light_on["service_data"]["brightness_pct"] == 80

    p_light_off = appliance.parse_appliance_intent("把房间灯关掉")
    assert p_light_off["domain"] == "light"
    assert p_light_off["service"] == "turn_off"

    p_light_query = appliance.parse_appliance_intent("灯关了吗")
    assert p_light_query["domain"] == "light"
    assert p_light_query["intent"] == "query"

    # 风扇与开关
    p_fan_on = appliance.parse_appliance_intent("开一下电风扇")
    assert p_fan_on["domain"] == "switch"
    assert p_fan_on["service"] == "turn_on"

    p_fan_off = appliance.parse_appliance_intent("把风扇关掉")
    assert p_fan_off["domain"] == "switch"
    assert p_fan_off["service"] == "turn_off"

    # 无关文本
    p_none = appliance.parse_appliance_intent("今天晚上吃什么好呢")
    assert p_none["domain"] == "unknown"


def test_local_intent_routing_appliance():
    # 验证 fast-path 本地 0ms 意图识别
    assert intent.classify_local("开灯") == "appliance"
    assert intent.classify_local("把空调调到26度") == "appliance"
    assert intent.classify_local("太热了帮我开下空调") == "appliance"
    assert intent.classify_local("给儿子发消息") == "message"
    assert intent.classify_local("拍张照片看说明书") == "photo"
    assert intent.classify_local("讲个笑话") is None


@pytest.mark.asyncio
async def test_appliance_control_unknown_and_ha_offline():
    with patch("skills.tts.speak", new_callable=AsyncMock) as mock_speak:
        # 未知意图
        res = await appliance.appliance_control({"text": "天空为什么是蓝色的"})
        assert res["status"] == "unrecognized"
        assert "没太听清" in res["spoken"]
        mock_speak.assert_called_once()

    # HA 离线状态
    mock_ha = MagicMock()
    mock_ha.is_available.return_value = False
    with patch("skills.ha.default", return_value=mock_ha), \
         patch("skills.tts.speak", new_callable=AsyncMock) as mock_speak:
        res = await appliance.appliance_control({"text": "开灯"})
        assert res["status"] == "ha_offline"
        assert "暂未连接" in res["spoken"]
        mock_speak.assert_called_once()


@pytest.mark.asyncio
async def test_appliance_control_query_and_action_success():
    mock_ha = MagicMock()
    mock_ha.is_available.return_value = True
    mock_ha.get_state = AsyncMock(return_value={
        "state": "on",
        "attributes": {"friendly_name": "起夜小夜灯"}
    })
    mock_ha.call_service = AsyncMock(return_value={"ok": True})

    # 1. 状态查询
    with patch("skills.ha.default", return_value=mock_ha), \
         patch("skills.tts.speak", new_callable=AsyncMock) as mock_speak:
        res = await appliance.appliance_control({"text": "灯开着吗"})
        assert res["status"] == "queried"
        assert "开着的" in res["spoken"]
        mock_ha.get_state.assert_awaited_once()

    # 2. 执行控制（开灯）
    with patch("skills.ha.default", return_value=mock_ha), \
         patch("skills.tts.speak", new_callable=AsyncMock) as mock_speak:
        res = await appliance.appliance_control({"text": "把灯打开"})
        assert res["status"] == "ok"
        assert "已经为您打开了" in res["spoken"]
        mock_ha.call_service.assert_awaited_once_with(
            domain="light",
            service="turn_on",
            service_data={"brightness_pct": 80},
            target={"entity_id": "light.night_light"}
        )


@pytest.mark.asyncio
async def test_appliance_control_service_failure():
    mock_ha = MagicMock()
    mock_ha.is_available.return_value = True
    mock_ha.call_service = AsyncMock(return_value={"ok": False, "error": "Gateway Timeout"})

    with patch("skills.ha.default", return_value=mock_ha), \
         patch("skills.tts.speak", new_callable=AsyncMock) as mock_speak:
        res = await appliance.appliance_control({"text": "关空调"})
        assert res["status"] == "failed"
        assert "超时" in res["spoken"]
        assert res["error"] == "Gateway Timeout"


def test_appliance_scene_yaml_validity():
    scene_path = pathlib.Path(__file__).parent.parent / "scenes" / "appliance_control.yaml"
    assert scene_path.exists(), "appliance_control.yaml must exist"
    with open(scene_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["id"] == "appliance_control"
    assert any(t.get("intent") == "appliance" for t in data.get("trigger", []))
    assert any(a.get("name") == "appliance_control" for a in data.get("action", []))
