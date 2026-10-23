"""传感器层：音视频采集与信号处理（接口契约外的实现细节层）。"""
from sensors.sentinel import EdgeSentinel, VLAObservation, assemble_observation, run_sentinel

__all__ = ["EdgeSentinel", "VLAObservation", "assemble_observation", "run_sentinel"]

