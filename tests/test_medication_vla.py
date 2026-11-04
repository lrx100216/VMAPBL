"""单元测试：场景4 服药提醒 VLA (Vision-Language-Action) 多模态动作理解。

验证通过通用视觉语言大模型理解老人服药动作（拿药盒/药瓶/倒水吞服），
无需针对特定药盒训练 YOLO 小物体检测器（models/pillbox.pt）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np
import pytest

from actions import medication
from core.state import StateStore


@pytest.fixture(autouse=True)
def clean_medication():
    medication.reset_singletons()
    yield
    medication.reset_singletons()


@pytest.mark.asyncio
async def test_vla_medication_detected_requires_voice_confirmation(monkeypatch):
    st = StateStore()
    monkeypatch.setattr(medication, "_get_state", lambda: st)

    # 模拟未部署 pillbox.pt 检测器
    mock_detector = MagicMock()
    mock_detector.available.return_value = False
    mock_detector._weights_path.return_value = "models/pillbox.pt"
    monkeypatch.setattr(medication, "_get_detector", lambda: mock_detector)

    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(medication, "_grab", lambda: fake_frame)

    # 模拟 VLA 视觉语言大模型识别到服药动作
    async def fake_json_call(frame, prompt, schema, scene_id=""):
        return {
            "taking_medication": True,
            "holding_medicine": True,
            "drinking_water": True,
            "description": "手持药板就温水吞服",
            "confidence": 0.92,
            "uncertain": False,
        }

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)

    spoken: list[str] = []
    async def fake_speak(text):
        spoken.append(text)

    monkeypatch.setattr("skills.tts.speak", fake_speak)
    monkeypatch.setattr("skills.stt.listen", AsyncMock(return_value="还没吃"))
    monkeypatch.setattr("skills.notify.send", AsyncMock())

    result = await medication.medication_watch({"use_vla": True}, slot="morning")

    assert result == "voice_pending"
    assert st.medication_taken["morning"] is False
    assert any("药吃了吗" in s for s in spoken)


@pytest.mark.asyncio
async def test_vla_medication_uncertain_falls_back_to_voice(monkeypatch):
    st = StateStore()
    monkeypatch.setattr(medication, "_get_state", lambda: st)

    mock_detector = MagicMock()
    mock_detector.available.return_value = False
    monkeypatch.setattr(medication, "_get_detector", lambda: mock_detector)

    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(medication, "_grab", lambda: fake_frame)

    # 模拟 VLA 画面看不清（如老人背对镜头）
    async def fake_json_call(frame, prompt, schema, scene_id=""):
        return {
            "taking_medication": False,
            "holding_medicine": False,
            "drinking_water": False,
            "description": "看不清手部动作",
            "confidence": 0.2,
            "uncertain": True,
        }

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)

    # 兜底到语音确认
    monkeypatch.setattr("skills.stt.listen", AsyncMock(return_value="药吃过了"))
    spoken: list[str] = []
    monkeypatch.setattr("skills.tts.speak", AsyncMock(side_effect=lambda s: spoken.append(s)))

    result = await medication.medication_watch({"use_vla": True}, slot="noon")

    assert result == "voice_taken"
    assert st.medication_taken["noon"] is True
    assert any("药吃了吗" in s for s in spoken)


@pytest.mark.asyncio
async def test_vla_medication_exception_shielding_graceful_voice(monkeypatch):
    st = StateStore()
    monkeypatch.setattr(medication, "_get_state", lambda: st)

    mock_detector = MagicMock()
    mock_detector.available.return_value = False
    monkeypatch.setattr(medication, "_get_detector", lambda: mock_detector)

    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(medication, "_grab", lambda: fake_frame)

    # 模拟网络断开，VLA 调用报错
    async def fake_json_call_err(*args, **kwargs):
        raise ConnectionResetError("VLM connection reset")

    monkeypatch.setattr("skills.vision.json_call", fake_json_call_err)
    monkeypatch.setattr("skills.stt.listen", AsyncMock(return_value="药吃好了"))
    monkeypatch.setattr("skills.tts.speak", AsyncMock())

    # 不崩溃，优雅退回语音确认
    result = await medication.medication_watch({"use_vla": True}, slot="evening")
    assert result == "voice_taken"
    assert st.medication_taken["evening"] is True


def test_vla_medication_schema_and_prompt_structure():
    assert "taking_medication" in medication._VLA_MEDICATION_SCHEMA
    assert "holding_medicine" in medication._VLA_MEDICATION_SCHEMA
    assert "confidence" in medication._VLA_MEDICATION_SCHEMA
    assert "服药" in medication._VLA_MEDICATION_PROMPT
