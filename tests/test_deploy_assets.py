"""launchd 自启/防休眠配置与独立看门狗（第三阶段）。"""
from __future__ import annotations

import plistlib
import sys
import time
from pathlib import Path

import pytest

from core.heartbeat import write_heartbeat
from tools import healthcheck, install_launchd

ROOT = Path(__file__).resolve().parent.parent


def test_guardian_plist_wraps_caffeinate_and_keepalive(tmp_path):
    plists = install_launchd.build_plists(ROOT, Path("/tmp/py"), tmp_path / "logs")
    assert set(plists) == {"com.local.butler.guardian.plist",
                           "com.local.butler.watchdog.plist"}
    guardian = plistlib.loads(plists["com.local.butler.guardian.plist"].encode())
    args = guardian["ProgramArguments"]
    assert args[:2] == ["/usr/bin/caffeinate", "-dimsu"]
    assert args[2] == str(Path("/tmp/py").resolve())
    assert args[3].endswith("main.py") and "--serve" in args
    assert guardian["KeepAlive"] == {"SuccessfulExit": False}
    assert guardian["ThrottleInterval"] == 15
    assert guardian["WorkingDirectory"] == str(ROOT)
    assert guardian["ProcessType"] == "Interactive"
    assert str(tmp_path / "logs") in guardian["StandardOutPath"]
    assert guardian["EnvironmentVariables"]["PYTHONPATH"] == str(ROOT)


def test_watchdog_plist_runs_every_minute(tmp_path):
    plists = install_launchd.build_plists(ROOT, Path("/tmp/py"), tmp_path / "logs")
    watchdog = plistlib.loads(plists["com.local.butler.watchdog.plist"].encode())
    assert watchdog["StartInterval"] == 60
    assert watchdog["ProgramArguments"][1].endswith("tools/healthcheck.py")
    assert "KeepAlive" not in watchdog


def test_installer_writes_plists_without_loading(tmp_path):
    plists = install_launchd.build_plists(ROOT, Path("/tmp/py"), tmp_path / "logs")
    written = install_launchd.install(plists, tmp_path / "LaunchAgents")
    assert len(written) == 2 and all(p.exists() for p in written)
    assert all(p.read_text(encoding="utf-8").startswith("<?xml") for p in written)


def test_render_rejects_unreplaced_placeholder():
    with pytest.raises(ValueError, match="占位符"):
        install_launchd.render_plist("<plist>__MISSING__</plist>", {"PYTHON": "/p"})


def test_healthcheck_evaluate_states():
    now = 1000.0
    assert healthcheck.evaluate(None, now=now, stale_after_s=180) == [
        "heartbeat_missing_or_corrupt"]
    fresh = {"ts": now - 5, "ready": True, "checks": {"camera": True, "db": True}}
    assert healthcheck.evaluate(fresh, now=now, stale_after_s=180) == []
    stale = {"ts": now - 500, "ready": True}
    assert healthcheck.evaluate(stale, now=now, stale_after_s=180) == [
        "heartbeat_stale"]
    not_ready = {"ts": now, "ready": False,
                 "checks": {"camera": False, "db": True}}
    assert healthcheck.evaluate(not_ready, now=now, stale_after_s=180) == [
        "not_ready:camera"]


def test_healthcheck_alerts_once_then_suppresses(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "heartbeat.json"
    state_path = tmp_path / "last_alert.json"
    write_heartbeat(heartbeat_path, {"ts": time.time() - 999, "ready": True})
    monkeypatch.setattr(healthcheck.config, "settings", lambda: {
        "ops": {"heartbeat_path": str(heartbeat_path),
                "stale_after_s": 180,
                "watchdog_state_path": str(state_path),
                "watchdog_repeat_s": 1800}})
    monkeypatch.setenv("WATCHDOG_WEBHOOK_URL", "https://watchdog.invalid/hook")
    sent = []

    def fake_post(url, text):
        sent.append((url, text))
        return True

    monkeypatch.setattr(healthcheck, "_post", fake_post)
    assert healthcheck.main([]) == 1
    assert len(sent) == 1 and "heartbeat_stale" in sent[0][1]
    assert healthcheck.main([]) == 1
    assert len(sent) == 1          # 重复告警窗口内不刷屏
    assert state_path.exists()


def test_healthcheck_ok_exits_zero(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "heartbeat.json"
    write_heartbeat(heartbeat_path, {"ts": time.time(), "ready": True,
                                     "checks": {"camera": True, "db": True}})
    monkeypatch.setattr(healthcheck.config, "settings", lambda: {
        "ops": {"heartbeat_path": str(heartbeat_path), "stale_after_s": 180}})
    monkeypatch.setenv("WATCHDOG_WEBHOOK_URL", "")
    assert healthcheck.main([]) == 0
