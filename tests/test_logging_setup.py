"""统一日志落盘与轮转（第三阶段）。"""
from __future__ import annotations

import logging

from core import logging_setup


def _reset(monkeypatch):
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def test_configure_logging_writes_file_and_is_idempotent(tmp_path, monkeypatch):
    _reset(monkeypatch)
    path = tmp_path / "logs" / "butler.log"
    try:
        first = logging_setup.configure_logging(path=path)
        second = logging_setup.configure_logging(path=path)
        assert first == second == path
        logging.getLogger("sensor.test").warning("摄像头离线")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "摄像头离线" in path.read_text(encoding="utf-8")
        assert len(logging.getLogger().handlers) == 2  # 文件 + stderr，不重复添加
    finally:
        _reset(monkeypatch)


def test_configure_logging_creates_parent_directory(tmp_path, monkeypatch):
    _reset(monkeypatch)
    path = tmp_path / "deep" / "nested" / "butler.log"
    try:
        logging_setup.configure_logging(path=path)
        assert path.parent.is_dir()
    finally:
        _reset(monkeypatch)


def test_configure_logging_survives_unwritable_file(tmp_path, monkeypatch):
    """日志文件不可写时保留 stderr，不能让守护进程起不来。"""
    _reset(monkeypatch)

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise OSError("read-only")

    monkeypatch.setattr(logging_setup.logging.handlers,
                        "RotatingFileHandler", _Boom)
    try:
        path = logging_setup.configure_logging(path=tmp_path / "x.log")
        assert path == tmp_path / "x.log"
        assert logging.getLogger().handlers  # 至少还有 stderr handler
    finally:
        _reset(monkeypatch)
