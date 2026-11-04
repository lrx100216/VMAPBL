"""M3 语音链与场景 8/1/2 的单测。

覆盖：tts 半双工互斥与尾静默、VAD 静音不误触发、OCR 实证读字、
vision.json_call 重试/不确定语义、messages 表、speech 触发器路由、
speak expr 动态播报、三个场景技能的状态机。

硬件依赖说明：全部测试离线可跑——真实 say/ffmpeg/摄像头/麦克风/网络
都不触碰（tts 由 conftest 拦截，stt/vision/llm 用 monkeypatch 替换，
OCR 用 cv2 现画的图走本机 Vision 框架）。
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from core import bus
from core.log import EventLog, set_default
from core.scene_engine import SceneEngine, SceneError
from core.state import StateStore


def make_engine(tmp_path, scenes: dict[str, str],
                skills: dict | None = None) -> tuple[SceneEngine, EventLog]:
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    for fname, text in scenes.items():
        (scenes_dir / fname).write_text(text, encoding="utf-8")
    log = EventLog(tmp_path / "test.db")
    return SceneEngine(scenes_dir, StateStore(), log,
                       skills=skills or {}), log


# ---------- skills/tts.py：半双工互斥 ----------

async def test_speak_mutex_tail_and_flag(monkeypatch):
    from core.state import state
    from skills import tts
    monkeypatch.setattr(tts, "_tts_settings", lambda: {"tail_silence_ms": 50})
    active = 0
    max_active = 0
    calls: list[str] = []

    async def fake_runner(text):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(text)
        assert state.speaking is True  # 播报期间半双工标志必须在位
        await asyncio.sleep(0.02)
        active -= 1

    monkeypatch.setattr(tts, "_runner", fake_runner)
    state.speaking = False
    t0 = time.time()
    await asyncio.gather(tts.speak("第一句"), tts.speak("第二句"))
    elapsed = time.time() - t0
    assert max_active == 1                    # 互斥：两次播报绝不重叠
    assert calls == ["第一句", "第二句"]      # 排队且不乱序
    assert state.speaking is False            # 收尾后标志必须复位
    assert elapsed >= 0.02 * 2 + 0.05 * 2 - 0.01  # 各自尾静默 50ms 都生效


async def test_speak_empty_is_noop(_spoken):
    from skills.tts import speak
    await speak("   ")
    assert _spoken == []


# ---------- sensors/vad.py ----------

def test_vad_silence_never_triggers():
    from sensors.vad import VadStream
    v = VadStream()
    events = []
    for _ in range(30):  # 0.5s 全零静音
        events += v.feed(np.zeros(1600, dtype=np.float32))
    assert events == []


# ---------- skills/stt.py ----------

async def test_listen_no_speech_returns_empty(monkeypatch):
    from skills import stt

    async def fake_record(timeout_s):
        return None

    monkeypatch.setattr(stt, "record_utterance", fake_record)
    assert await stt.listen(0.1) == ""


async def test_listen_too_short_returns_empty(monkeypatch):
    from skills import stt

    async def fake_record(timeout_s):
        return np.zeros(800, dtype=np.float32)  # 0.05s < 0.1s 下限

    monkeypatch.setattr(stt, "record_utterance", fake_record)
    assert await stt.listen(0.1) == ""


async def test_listen_transcribes_segment(monkeypatch):
    from skills import stt

    async def fake_record(timeout_s):
        return np.zeros(16000, dtype=np.float32)

    monkeypatch.setattr(stt, "record_utterance", fake_record)
    monkeypatch.setattr(stt, "_transcribe_sync", lambda seg: "奶奶你好")
    assert await stt.listen(0.1) == "奶奶你好"


# ---------- skills/ocr.py（真实 Vision 框架） ----------

def test_ocr_reads_rendered_text():
    import cv2
    from skills.ocr import recognize_text
    img = np.zeros((480, 640, 3), np.uint8)
    cv2.putText(img, "HELLO 123", (60, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 2.5, (255, 255, 255), 5)
    lines = recognize_text(img)
    assert any("HELLO" in t for t, _c in lines), f"OCR 没读出 HELLO: {lines}"


# ---------- skills/vision.py：json_call 语义 ----------

def _tiny_frame():
    return np.zeros((64, 64, 3), np.uint8)


async def test_json_call_parses_fenced_json(monkeypatch):
    from skills import vision
    monkeypatch.setattr(vision, "_chat_sync", lambda m, t: (
        '前缀废话```json\n{"posture": "B"}\n```后缀', {}))
    data = await vision.json_call(_tiny_frame(), "测试", {})
    assert data["posture"] == "B" and data["uncertain"] is False


async def test_json_call_retries_once_then_succeeds(monkeypatch):
    from skills import vision
    calls = {"n": 0}

    def fake(messages, max_tokens):
        calls["n"] += 1
        return ("不是 JSON", {}) if calls["n"] == 1 else ('{"a": 1}', {})

    monkeypatch.setattr(vision, "_chat_sync", fake)
    data = await vision.json_call(_tiny_frame(), "p", {})
    assert data["a"] == 1 and calls["n"] == 2


async def test_json_call_uncertain_after_two_bad_outputs(monkeypatch):
    from skills import vision
    monkeypatch.setattr(vision, "_chat_sync", lambda m, t: ("垃圾", {}))
    data = await vision.json_call(_tiny_frame(), "p", {})
    assert data["uncertain"] is True


async def test_json_call_never_raises(monkeypatch):
    from skills import vision

    def boom(m, t):
        raise RuntimeError("网络断了")

    monkeypatch.setattr(vision, "_chat_sync", boom)
    data = await vision.json_call(_tiny_frame(), "p", {})
    assert data["uncertain"] is True and "网络断了" in data["error"]


async def test_json_call_none_frame_and_local_backend():
    from skills.vision import json_call
    assert (await json_call(None, "p", {}))["uncertain"] is True
    assert (await json_call(_tiny_frame(), "p", {}, backend="local"))["uncertain"] is True


# ---------- core/log.py：messages 表 ----------

def test_messages_roundtrip(tmp_path):
    log = EventLog(tmp_path / "m.db")
    id1 = log.save_message("to_family", "晚上过去吃饭", sender="老人",
                           recipient="儿子")
    id2 = log.save_message("to_elder", "吃了吗", sender="家人")
    msgs = log.list_messages()
    assert [m["id"] for m in msgs] == [id2, id1]  # 新→旧
    assert msgs[1]["recipient"] == "儿子"
    assert msgs[1]["direction"] == "to_family"
    log.mark_message(id1, "replied")
    assert log.list_messages("to_family")[0]["status"] == "replied"
    with pytest.raises(ValueError, match="方向"):
        log.save_message("bogus", "x")
    log.close()


# ---------- 引擎：speak expr 与 speech 触发器 ----------

async def test_speak_expr_uses_result(tmp_path, _spoken):
    async def decide(ctx):
        return {"speech": "动态播报内容"}

    scene = ("id: spk\nname: 动态播报\ntrigger: [{type: manual}]\n"
             "action:\n"
             "  - {type: skill, name: decide}\n"
             "  - {type: speak, expr: \"result['speech']\"}\n")
    engine, log = make_engine(tmp_path, {"s.yaml": scene},
                              skills={"decide": decide})
    assert await engine.trigger("spk") is True
    assert _spoken == ["动态播报内容"]
    log.close()


def test_speak_text_and_expr_together_rejected(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action: [{type: speak, text: hi, expr: \"1+1\"}]\n")
    with pytest.raises(SceneError, match="恰好其一"):
        make_engine(tmp_path, {"b.yaml": bad})


def test_speak_expr_syntax_checked(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action: [{type: speak, expr: \"result[\"}]\n")
    with pytest.raises(SceneError, match="语法错误"):
        make_engine(tmp_path, {"b.yaml": bad})


async def test_speech_trigger_routes_by_intent(tmp_path):
    ran: list[str] = []

    async def rec(ctx):
        ran.append(ctx.get("text"))

    scene = ("id: sp\nname: 语音场景\ntrigger: [{type: speech, intent: message}]\n"
             "action: [{type: skill, name: rec}]\n")
    engine, log = make_engine(tmp_path, {"s.yaml": scene}, skills={"rec": rec})
    bus.clear_subscriptions()
    try:
        engine.wire_speech_triggers()
        await bus.publish({"type": "speech.final",
                           "payload": {"text": "给儿子发消息", "intent": "message"}})
        await bus.publish({"type": "speech.final",
                           "payload": {"text": "帮我念一下", "intent": "photo"}})
    finally:
        bus.clear_subscriptions()
    assert ran == ["给儿子发消息"]  # intent 不匹配的场景不被触发
    log.close()


async def test_speech_trigger_without_intent_matches_any(tmp_path):
    ran: list[str] = []

    async def rec(ctx):
        ran.append(ctx.get("intent"))

    scene = ("id: sp\nname: 全收\ntrigger: [{type: speech}]\n"
             "action: [{type: skill, name: rec}]\n")
    engine, log = make_engine(tmp_path, {"s.yaml": scene}, skills={"rec": rec})
    bus.clear_subscriptions()
    try:
        engine.wire_speech_triggers()
        await bus.publish({"type": "speech.final",
                           "payload": {"text": "x", "intent": "chat"}})
    finally:
        bus.clear_subscriptions()
    assert ran == ["chat"]
    log.close()


# ---------- actions：三个场景技能 ----------

async def test_family_message_send_flow(tmp_path, monkeypatch):
    from actions.messaging import family_message_send

    async def fake_chat_json(prompt, scene_id="", max_tokens=None):
        return {"recipient": "儿子", "body": "晚上过去吃饭"}

    monkeypatch.setattr("actions.messaging.chat_json", fake_chat_json)

    import httpx

    async def delivered_post(self, url, **kwargs):
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")
    monkeypatch.setattr(httpx.AsyncClient, "post", delivered_post)
    log = EventLog(tmp_path / "msg.db")
    set_default(log)
    try:
        result = await family_message_send({"text": "告诉我儿子晚上过去吃饭"})
        assert result["status"] == "sent"
        assert "儿子" in result["speech"]
        msgs = log.list_messages("to_family")
        assert msgs[0]["recipient"] == "儿子" and msgs[0]["text"] == "晚上过去吃饭"
    finally:
        set_default(None)
        log.close()


async def test_family_message_pending_receipt_is_not_called_sent(tmp_path, monkeypatch):
    from actions.messaging import family_message_send
    from skills.notify import NotificationResult

    async def fake_chat_json(prompt, scene_id="", max_tokens=None):
        return {"recipient": "儿子", "body": "晚上过去吃饭"}

    async def pending(*args, **kwargs):
        return NotificationResult(1, True, False, {"dingtalk": "failed"}, pending=True)

    monkeypatch.setattr("actions.messaging.chat_json", fake_chat_json)
    monkeypatch.setattr("actions.messaging.notify.send_with_receipt", pending)
    log = EventLog(tmp_path / "pending-msg.db")
    set_default(log)
    try:
        result = await family_message_send({"text": "告诉我儿子晚上过去吃饭"})
        assert result["status"] == "pending"
        assert "已经发给" not in result["speech"]
    finally:
        set_default(None)
        log.close()


async def test_real_family_message_scene_announces_pending(
        tmp_path, monkeypatch, _spoken):
    from actions import SKILLS
    from core import config
    from skills.notify import NotificationResult

    async def fake_chat_json(prompt, scene_id="", max_tokens=None):
        return {"recipient": "儿子", "body": "晚上过去吃饭"}

    async def pending(*args, **kwargs):
        return NotificationResult(1, True, False, {"dingtalk": "failed"}, pending=True)

    monkeypatch.setattr("actions.messaging.chat_json", fake_chat_json)
    monkeypatch.setattr("actions.messaging.notify.send_with_receipt", pending)
    log = EventLog(tmp_path / "pending-scene.db")
    engine = SceneEngine(config.ROOT / "scenes", StateStore(), log, skills=SKILLS)
    assert await engine.trigger(
        "msg_to_family", ctx={"text": "告诉我儿子晚上过去吃饭"}) is True
    assert any("自动重试" in text for text in _spoken)
    assert not any("已经发给" in text for text in _spoken)
    log.close()


async def test_family_reply_pending_receipt_is_not_called_replied(tmp_path, monkeypatch):
    from actions.messaging import family_message_receive
    from skills.notify import NotificationResult

    async def fake_listen(*args, **kwargs):
        return "我很好，不用担心"

    async def pending(*args, **kwargs):
        return NotificationResult(1, True, False, {"dingtalk": "failed"}, pending=True)

    monkeypatch.setattr("actions.messaging.stt.listen", fake_listen)
    monkeypatch.setattr("actions.messaging.notify.send_with_receipt", pending)
    log = EventLog(tmp_path / "pending-reply.db")
    set_default(log)
    try:
        result = await family_message_receive({"text": "吃饭了吗"})
        assert result["status"] == "pending"
        assert "已回复" not in result["speech"]
    finally:
        set_default(None)
        log.close()


async def test_family_message_send_no_recipient(tmp_path, monkeypatch):
    from actions.messaging import family_message_send

    async def fake_chat_json(prompt, scene_id="", max_tokens=None):
        return {"recipient": "", "body": "想孩子了"}

    monkeypatch.setattr("actions.messaging.chat_json", fake_chat_json)
    result = await family_message_send({"text": "想孩子了"})
    assert result["status"] == "no_recipient"


def _text_image():
    import cv2
    img = np.zeros((480, 640, 3), np.uint8)
    cv2.putText(img, "HELLO 123", (60, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 2.5, (255, 255, 255), 5)
    return img


async def test_photo_read_ocr_only_when_read_intent(monkeypatch):
    from actions import photo_ocr
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())
    vlm_calls: list[int] = []

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        vlm_calls.append(1)
        return {"answer": "不该被调用", "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    result = await photo_ocr.photo_read({"text": "帮我念一下上面写的什么"})
    assert result["source"] == "ocr"
    assert "HELLO" in result["speech"]
    assert vlm_calls == []  # 朗读意图绝不升云端（L2.5 档纪律）


async def test_photo_read_escalates_for_semantic_question(monkeypatch):
    from actions import photo_ocr
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())
    vlm_calls: list[int] = []

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        vlm_calls.append(1)
        return {"answer": "一天三次，饭后服用", "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    result = await photo_ocr.photo_read({"text": "这药一天吃几次"})
    assert result["source"] == "vlm"
    assert result["speech"] == "一天三次，饭后服用"
    assert vlm_calls == [1]


async def test_photo_read_uncertain_when_vlm_fails(monkeypatch):
    from actions import photo_ocr
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"uncertain": True, "error": "两次输出均不是合法 JSON"}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    result = await photo_ocr.photo_read({"text": "这药一天吃几次"})
    assert result["status"] == "uncertain" and "看不太清" in result["speech"]


async def test_family_query_fall_suspect_marks_urgent(tmp_path, monkeypatch):
    from actions import family_query
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"posture": "D", "on_floor": True, "activity": "平躺",
                "reply": "老人平躺在地面上", "confidence": 0.9, "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    log = EventLog(tmp_path / "q.db")
    set_default(log)
    try:
        result = await family_query.family_status_answer({})
        assert result["urgent"] is True
        msgs = log.list_messages("to_family")
        assert msgs[0]["urgent"] is True and "平躺" in msgs[0]["text"]
    finally:
        set_default(None)
        log.close()


async def test_family_query_low_confidence_not_urgent(tmp_path, monkeypatch):
    from actions import family_query
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"posture": "D", "on_floor": True, "reply": "看不太清",
                "confidence": 0.5, "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    log = EventLog(tmp_path / "q.db")
    set_default(log)
    try:
        result = await family_query.family_status_answer({})
        assert result["urgent"] is False  # 0.5 < 0.75 阈值，不升级紧急
        assert log.list_messages("to_family")[0]["urgent"] is False
    finally:
        set_default(None)
        log.close()


async def test_family_query_string_false_does_not_trigger_fall(monkeypatch):
    from actions import family_query
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"posture": "D", "on_floor": "false", "reply": "画面描述",
                "confidence": 0.99, "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    async def fake_send(*args, **kwargs):
        return 1

    monkeypatch.setattr("actions.family_query.notify.send", fake_send)
    result = await family_query.family_status_answer({})
    assert result["urgent"] is False


async def test_family_query_invalid_confidence_does_not_crash(monkeypatch):
    from actions import family_query
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"posture": "D", "on_floor": True, "reply": "画面描述",
                "confidence": "unknown", "uncertain": False}

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    async def fake_send(*args, **kwargs):
        return 1

    monkeypatch.setattr("actions.family_query.notify.send", fake_send)
    result = await family_query.family_status_answer({})
    assert result["urgent"] is False
    assert result["confidence"] == 0.0


async def test_photo_read_medication_structure(monkeypatch):
    from actions import photo_ocr
    monkeypatch.setattr("skills.vision.grab_frame", lambda: _text_image())

    async def fake_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {
            "answer": "奶奶，这是降压药，一天吃一次，每次吃一片，记得饭后吃。",
            "uncertain": False,
            "drug_name": "硝苯地平控释片",
            "dosage": "每次1片",
            "frequency": "一日1次",
            "usage_timing": "饭后",
            "precautions": "整片吞服，不可嚼碎",
        }

    monkeypatch.setattr("skills.vision.json_call", fake_json_call)
    result = await photo_ocr.photo_read({"text": "帮我看看这个药盒，这药怎么吃？"})
    assert result["source"] == "vlm"
    assert "降压药" in result["speech"]
    assert "medication_info" in result
    assert result["medication_info"]["drug_name"] == "硝苯地平控释片"
    assert result["medication_info"]["dosage"] == "每次1片"
    assert result["medication_info"]["frequency"] == "一日1次"


# ---------- 真实 scenes/ 目录 ----------

def test_real_scenes_all_load(tmp_path):
    from actions import SKILLS
    from core import config
    log = EventLog(tmp_path / "real.db")
    engine = SceneEngine(config.ROOT / "scenes", StateStore(), log, skills=SKILLS)
    assert {"demo_hello", "photo_ocr", "msg_to_family", "msg_to_elder",
            "family_query"} <= set(engine.scenes)
    log.close()
