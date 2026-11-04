import pytest

from tools.healthcheck import evaluate


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), True, 200.0])
def test_future_or_invalid_heartbeat_is_not_healthy(timestamp):
    assert evaluate({"ts": timestamp, "ready": True}, now=100.0, stale_after_s=180) == ["heartbeat_stale"]
