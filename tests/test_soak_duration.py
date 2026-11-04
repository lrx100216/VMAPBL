import sys

import pytest

from tools import soak_test


async def test_cycle_passes_deadline_to_perception(tmp_path, monkeypatch):
    captured = {}

    async def fake_perception(**kwargs):
        captured.update(kwargs)
        return {"frames": 3, "pose_calls": 1}

    monkeypatch.setattr("sensors.perception.run_perception", fake_perception)
    result = await soak_test._cycle(None, tmp_path, 1, 0.75)
    assert captured["max_duration_s"] == 0.75
    assert result == {"frames": 3, "pose_calls": 1}


def test_zero_duration_rejected_before_start(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["soak_test", "--minutes", "0"])
    with pytest.raises(SystemExit, match="运行时长必须大于零"):
        soak_test.main()
