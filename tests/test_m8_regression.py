"""M8 回归与成本核算单测：replay 扩展（fall/pillbox 场景）、稳压工具、成本报表。

全部离线：replay 用假 _RUN_ONCE / 假 Camera / 假检测器测聚合与投票逻辑，
不碰真实模型与摄像头；成本报表读临时 SQLite；稳压只测工具函数不跑长时感知。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from core import bus
from core.log import EventLog

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


# ---------- replay 扩展：fall 进白名单 ----------

def test_replay_accepts_fall_scene(tmp_path, monkeypatch):
    """M8：fall 场景进入回放白名单（M6 评审缺口：只支持 door_key/get_up/wake_up）。"""
    from tools import replay

    async def fake_run(scene_id, scene_yaml, video, tmp, idx):
        return {"predicted": "fall" if idx % 2 else "no_fall", "detail": {}}
    monkeypatch.setattr(replay, "_RUN_ONCE", fake_run)
    monkeypatch.setattr(replay, "PoseEstimator", lambda: object())

    videos = [tmp_path / f"fall_real_{i:02d}.mp4" for i in range(2)]
    for v in videos:
        v.write_bytes(b"x")
    stats = replay.run_replay("fall", videos, "fall", repeat=2)
    assert stats["total"] == 4 and stats["correct"] == 2
    assert stats["scene"] == "fall"


def test_replay_whitelist_has_fall_and_pillbox():
    from tools import replay
    assert "fall" in replay._DECIDE_SKILL
    assert replay._DECIDE_SKILL["fall"] == "fall_decide"


# ---------- pillbox 检测器回放 ----------

def test_pillbox_pass_vote_requires_full_window():
    """5 帧滑窗 ≥3：窗口不满不放行；3/5 与 5/5 放行；2/5 不放行。"""
    from tools.replay import _pillbox_pass_vote
    assert _pillbox_pass_vote([True, True, True]) is False            # 不满 5 帧
    assert _pillbox_pass_vote([True, True, True, True, True]) is True
    assert _pillbox_pass_vote([True, True, True, False, False]) is True
    assert _pillbox_pass_vote([True, True, False, False, False]) is False
    assert _pillbox_pass_vote([True, True, True, True, True, False,
                               False, False, False, False]) is True   # 前半程命中即过


class _FakeCam:
    """假摄像头：喂 N 帧后置 eof。detect 每次采样调用（间隔由 ts 差决定）。"""

    def __init__(self, frames: list[tuple[float, np.ndarray]], fps=10.0):
        self._frames = frames
        self._i = 0
        self.eof = False

    def start(self):
        pass

    def latest(self):
        if self._i < len(self._frames):
            ts, f = self._frames[self._i]
            self._i += 1
            return ts, f
        self.eof = True
        return None

    def stop(self):
        pass


def _fake_detector(hits: list[bool]):
    """剧本式检测器：按命中序列逐采样返回有无药盒。"""
    class D:
        calls = 0

        @staticmethod
        def available():
            return True

        @staticmethod
        def detect(frame):
            D.calls += 1
            hit = hits[min(D.calls - 1, len(hits) - 1)]
            return [(np.array([10.0, 10.0, 50.0, 50.0]), 0.9)] if hit else []
    return D


def test_run_pillbox_replay_present(tmp_path, monkeypatch):
    """采样 1s 间隔 + 5 帧滑窗 ≥3 → 判 present（真实 Camera/Detector 打桩）。"""
    from sensors import camera as cam_mod
    from sensors import detect as det_mod
    from tools import replay

    frames = [(i, np.zeros((100, 100, 3), np.uint8)) for i in range(6)]
    monkeypatch.setattr(cam_mod, "Camera", lambda *a, **kw: _FakeCam(frames))
    monkeypatch.setattr(det_mod, "PillboxDetector", _fake_detector(
        [True, True, True, False, False]))

    video = tmp_path / "pillbox_real_01.mp4"
    video.write_bytes(b"x")
    stats = replay.run_pillbox_replay([video], "present", repeat=1)
    assert stats["total"] == 1 and stats["correct"] == 1
    assert stats["runs"][0]["predicted"] == "present"
    assert stats["runs"][0]["samples"] == 6


def test_run_pillbox_replay_absent(tmp_path, monkeypatch):
    """只喝水没有药盒的负样本：全程无检出 → absent（宁可漏报不可误报）。"""
    from sensors import camera as cam_mod
    from sensors import detect as det_mod
    from tools import replay

    frames = [(i, np.zeros((100, 100, 3), np.uint8)) for i in range(6)]
    monkeypatch.setattr(cam_mod, "Camera", lambda *a, **kw: _FakeCam(frames))
    monkeypatch.setattr(det_mod, "PillboxDetector", _fake_detector(
        [False, False, False, False, False]))

    video = tmp_path / "pillbox_drink_only_01.mp4"
    video.write_bytes(b"x")
    stats = replay.run_pillbox_replay([video], "absent", repeat=1)
    assert stats["runs"][0]["predicted"] == "absent" and stats["correct"] == 1


def test_pillbox_replay_requires_weights(tmp_path, monkeypatch):
    """权重未训练（available()=False）→ 拒绝跑并提示先训练（诚实不编造）。"""
    from sensors import detect as det_mod
    from tools import replay

    class NoWeights:
        @staticmethod
        def available():
            return False

    monkeypatch.setattr(det_mod, "PillboxDetector", NoWeights)
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    with pytest.raises(SystemExit, match="train_pillbox"):
        replay.run_pillbox_replay([video], "present")


# ---------- 稳压工具 ----------

def test_soak_rss_kb_positive():
    from tools import soak_test
    assert soak_test._rss_kb() > 0   # 高水位采样（resource，ps 兜底）可用且为正


# ---------- 成本报表 ----------

def test_cost_report_reads_db(tmp_path):
    from tools import cost_report

    db = tmp_path / "t.db"
    log = EventLog(db)
    log.log_api_call("family_query", "cloud", "deepseek-flash",
                     prompt_tokens=100, completion_tokens=50, cost_cny=0.0008)
    log.log_api_call("family_query", "cloud", "deepseek-flash",
                     prompt_tokens=200, completion_tokens=80, cost_cny=0.0008)
    log.log_api_call("photo_ocr", "cloud", "deepseek-flash",
                     prompt_tokens=30, completion_tokens=10, cost_cny=0.0004)
    log.log_event("door_key", "trigger_fire", {"src": "test"})
    log.log_event("door_key", "error", {"阶段": "action"})
    log.save_message("to_family", "hello", sender="老人")
    log.close()

    d = cost_report.report(db)
    assert d["total"]["calls"] == 3
    assert d["total"]["cost_cny"] == pytest.approx(0.0020)
    assert d["total"]["prompt_tokens"] == 330
    by_scene = {s["scene_id"]: s for s in d["per_scene"]}
    assert by_scene["family_query"]["calls"] == 2
    assert d["per_model"][0]["model"] == "deepseek-flash"
    ev = d["per_scene_events"]["door_key"]
    assert ev["trigger_fire"] == 1 and ev["error"] == 1
    assert d["messages"]["to_family"] == 1
    assert len(d["recent"]) == 3
