"""P0-4：依赖可复现、生产预检与 RTSP 凭据脱敏。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sensors.camera import Camera

ROOT = Path(__file__).resolve().parent.parent


def test_dependency_requirements_and_lock_are_reproducible():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "opencv-python>=4.10,<5" in req
    assert "torchaudio==2.9.1" in req
    assert "httpx>=0.28,<1" in req
    assert "av>=11,<16" in req

    lock_path = ROOT / "requirements-lock.txt"
    assert lock_path.exists(), "必须提交可复现安装的 requirements-lock.txt"
    lock = lock_path.read_text(encoding="utf-8")
    for package in ("opencv-python", "torch", "torchaudio", "torchvision", "av", "httpx"):
        assert any(line.startswith(f"{package}==") for line in lock.splitlines()), package
    assert not any(line.startswith("opencv-python==5") for line in lock.splitlines())


def test_single_sided_door_does_not_require_outside_roi(tmp_path):
    from core.preflight import production_issues
    poly = [[0, 0], [5, 0], [5, 5]]
    cfg = {"roi": {"bed": poly, "door_inside": poly}, "door": {"mode": "proximity"}}
    issues = production_issues(settings=cfg, env={}, root=tmp_path)
    assert not any("door_outside" in issue for issue in issues)
    assert not any("door_inside" in issue for issue in issues)
    cfg["roi"].pop("door_inside")
    issues = production_issues(settings=cfg, env={}, root=tmp_path)
    assert any("door_inside" in issue for issue in issues)


def test_production_preflight_collects_all_blockers(tmp_path):
    from core.preflight import production_issues

    env_file = tmp_path / ".env"
    env_file.write_text("placeholder", encoding="utf-8")
    env_file.chmod(0o644)
    settings = {
        "server": {"host": "0.0.0.0"},
        "global": {"db_path": "data/butler.db"},
        "ops": {"log_path": "logs/butler.log"},
        "pose": {"weights": "models/missing.pt", "prefer_coreml": False},
    }
    issues = production_issues(settings=settings, env={}, root=tmp_path)
    text = "\n".join(issues)
    assert "BUTLER_TOKEN" in text
    assert "通知" in text
    assert "姿态模型" in text
    assert ".env" in text and "0600" in text


def test_production_preflight_accepts_complete_config(tmp_path):
    from core.preflight import production_issues

    env_file = tmp_path / ".env"
    env_file.write_text("placeholder", encoding="utf-8")
    env_file.chmod(0o600)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "pose.pt").write_bytes(b"model")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    settings = {
        "server": {"host": "127.0.0.1"},
        "global": {"db_path": "data/butler.db"},
        "ops": {"log_path": "logs/butler.log"},
        "pose": {"weights": "models/pose.pt", "prefer_coreml": False},
        "notify": {"dingtalk_webhook": "https://example.invalid/hook"},
        "roi": {name: [[0, 0], [1, 0], [1, 1]] for name in ("bed", "door_inside", "door_outside")},
    }
    env = {
        "BUTLER_TOKEN": "token",
        "DINGTALK_SIGN_SECRET": "secret",
    }
    assert production_issues(settings=settings, env=env, root=tmp_path) == []


def test_production_preflight_rejects_missing_roi(tmp_path):
    from core.preflight import production_issues
    issues = production_issues(settings={"roi": {}}, env={}, root=tmp_path)
    assert all(any(name in issue for issue in issues)
               for name in ("bed", "door_inside", "door_outside"))
    issues = production_issues(settings={"roi": {"bed": [1, 2, 3]}}, env={}, root=tmp_path)
    assert any("bed" in issue for issue in issues)
    issues = production_issues(settings={"roi": {"bed": [[0, 0], [1, 1], [2, 2]]}}, env={}, root=tmp_path)
    assert any("bed" in issue for issue in issues)


def test_production_preflight_checks_channel_specific_signatures(tmp_path):
    from core.preflight import production_issues

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "pose.pt").write_bytes(b"model")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    (tmp_path / ".env").chmod(0o600)
    settings = {
        "server": {"host": "127.0.0.1"},
        "global": {"db_path": "data/butler.db"},
        "ops": {"log_path": "logs/butler.log"},
        "pose": {"weights": "models/pose.pt", "prefer_coreml": False},
        "notify": {
            "dingtalk_webhook": "https://example.invalid/ding",
            "feishu_webhook": "https://example.invalid/feishu",
        },
    }
    issues = production_issues(settings=settings,
                               env={"BUTLER_TOKEN": "token"}, root=tmp_path)
    text = "\n".join(issues)
    assert "DINGTALK_SIGN_SECRET" in text
    assert "FEISHU_SIGN_SECRET" in text


def test_rtsp_open_error_never_contains_credentials(monkeypatch):
    class ClosedCapture:
        def isOpened(self):
            return False

    monkeypatch.setattr("sensors.camera.cv2.VideoCapture",
                        lambda *args, **kwargs: ClosedCapture())
    source = "rtsp://alice:secret@192.168.1.20:554/live?token=private"
    with pytest.raises(RuntimeError) as exc:
        Camera(source)._open_stream_capture()
    message = str(exc.value)
    assert "alice" not in message
    assert "secret" not in message
    assert "private" not in message
    assert "192.168.1.20" in message


async def test_main_require_production_fails_before_startup(monkeypatch):
    import main

    monkeypatch.setattr("core.logging_setup.configure_logging", lambda: None)
    monkeypatch.setattr("core.preflight.production_issues",
                        lambda **kwargs: ["测试：生产配置不完整"])
    monkeypatch.setattr("sys.argv", ["main.py", "--require-production", "--list"])
    assert await main.amain() == 2
