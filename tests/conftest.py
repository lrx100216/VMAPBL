"""pytest 全局夹具：测试期间把真实 `say` 播报换成记录器。

绝不允许测试运行时电脑突然开口说话——所有走到 skills/tts.py 的播报
都会被这里拦截成记录列表（需要断言播报内容的测试通过 _spoken 夹具拿记录）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _block_real_microphone(monkeypatch):
    """Tests may inject a fake microphone; they must not open the Mac's input."""
    from skills import stt

    async def blocked(self):
        raise RuntimeError("测试不访问真实麦克风")

    monkeypatch.setattr(stt.Microphone, "__aenter__", blocked)


@pytest.fixture(autouse=True)
def _isolate_settings_and_real_endpoints(tmp_path, monkeypatch):
    """Each test owns its settings; live webhook credentials never leak into tests."""
    import yaml
    from core import config
    original = config._SETTINGS_PATH
    data = yaml.safe_load(original.read_text(encoding="utf-8")) if original.exists() else {}
    data = data or {}
    for name in ("dingtalk_webhook", "feishu_webhook", "webhook_url"):
        (data.setdefault("notify", {}))[name] = ""
    (data.setdefault("ops", {}))["watchdog_webhook_url"] = ""
    (data.setdefault("homeassistant", {}))["token"] = ""
    data["homeassistant"]["enabled"] = False
    path = tmp_path / "isolated-settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "_SETTINGS_PATH", path)
    for key in ("DINGTALK_WEBHOOK", "FEISHU_WEBHOOK", "NOTIFY_WEBHOOK_URL",
                "DINGTALK_SIGN_SECRET", "FEISHU_SIGN_SECRET", "HA_TOKEN",
                "WATCHDOG_WEBHOOK_URL", "DINGTALK_DIRECT_ENABLED", "DINGTALK_USER_ID",
                "DINGTALK_ROBOT_CODE"):
        monkeypatch.delenv(key, raising=False)
    cached_settings = config.settings
    cached_settings.cache_clear()
    yield
    cached_settings.cache_clear()


@pytest.fixture(autouse=True)
def _spoken(monkeypatch):
    """自动生效：替换 tts 的进程执行器为记录器，尾静默压到 1ms。"""
    from skills import tts
    spoken: list[str] = []

    async def fake_runner(text: str) -> None:
        spoken.append(text)

    monkeypatch.setattr(tts, "_runner", fake_runner)
    monkeypatch.setattr(tts, "_tts_settings",
                        lambda: {"tail_silence_ms": 1, "voice": "Tingting"})
    return spoken


@pytest.fixture(autouse=True)
def _block_cloud_network(monkeypatch):
    """默认禁止真实模型调用；单测可用自己的 monkeypatch 显式覆盖。"""
    from skills import llm, vision

    def blocked(*args, **kwargs):
        raise RuntimeError("测试禁网：请用 monkeypatch 替换模型调用边界")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(vision, "_CLIENT", None)
    monkeypatch.setattr(vision, "_chat_sync", blocked)
    # llm 在 import 时从 vision 复制了 _chat_sync 引用，必须分别拦截。
    monkeypatch.setattr(llm, "_chat_sync", blocked)
