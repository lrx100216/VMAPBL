# Repair Test & Verification Registry · VMAPBL
**Date**: 2026-11-04  
**Protocol**: Antigravity Autonomous Repair Protocol (Gemini 3.8 Flash High)  
**Workspace**: `/Users/liuruixuan/VMAPBL`  
**Test Command**: `PYTHONPATH=. ./.venv/bin/pytest -v`  
**Python Version**: 3.13.15  
**Pytest Version**: 9.1.1  

---

## 1. Executive Test Summary
- **Baseline Test Count**: 380 passed (0 failed)
- **Post-Repair Test Count**: **393 passed** (0 failed, 0 errors, 0 skips)
- **Net New Verified Tests**: +13 regression and security hardening tests
- **Execution Duration**: 32.02 seconds
- **Pass Rate**: **100.0%**

---

## 2. Test Suite Breakdown

| Test File | Total Tests | Passed | Failed | Key Verification Targets |
|---|---|---|---|---|
| `tests/test_app_bundle.py` | 5 | 5 | 0 | macOS launcher, relative paths, degraded notifications |
| `tests/test_audit_hardening.py` | 15 | 15 | 0 | Negation asymmetry, TTS tag stripping, TTS urgent preemption, medication daily reset |
| `tests/test_auth.py` | 13 | 13 | 0 | Token requirement, fail-closed webhooks, replay protection, CORS mutation rejection |
| `tests/test_background_scenes.py` | 3 | 3 | 0 | Asynchronous scene execution & task tracking |
| `tests/test_bus_state.py` | 7 | 7 | 0 | EventBus dispatch, CancelledError tolerance, state event payload |
| `tests/test_camera_stream.py` | 7 | 7 | 0 | Stream reconnection, USB camera 30-frame retry reconnection |
| `tests/test_dashboard_live.py` | 6 | 6 | 0 | Dashboard timeline, wake-up midnight timestamp filtering |
| `tests/test_dashboard_pages.py` | 4 | 4 | 0 | Static dashboard assets & debug console |
| `tests/test_demo_readiness.py` | 2 | 2 | 0 | Demo runbook verification |
| `tests/test_deploy_assets.py` | 7 | 7 | 0 | LaunchAgent plist generation & caffeinate arguments |
| `tests/test_family_router.py` | 14 | 14 | 0 | Message classification & query routing |
| `tests/test_health.py` | 5 | 5 | 0 | Health check probes & snapshot status |
| `tests/test_health_hardening.py` | 7 | 7 | 0 | Health probe staleness & camera heartbeat |
| `tests/test_logging_setup.py` | 3 | 3 | 0 | EventLog SQLite table creation & rotation |
| `tests/test_m3_voice_chain.py` | 31 | 31 | 0 | Voice STT/TTS pipeline, query formatting, half-duplex mutex |
| `tests/test_m4_perception.py` | 14 | 14 | 0 | Motion gate, still detector, Gate 2 crumpled fall occupancy hold |
| `tests/test_m5_scenes.py` | 36 | 36 | 0 | ROI polygon tests, door_outside key reminders, wake_up recheck persistence |
| `tests/test_m6_scenes.py` | 38 | 38 | 0 | Medication scene, pillbox detector fallback logging, daily accounting |
| `tests/test_m7_console.py` | 15 | 15 | 0 | REST endpoints, ROI calibration, WebSocket push |
| `tests/test_m8_regression.py` | 8 | 8 | 0 | Long-run regression & cost estimation |
| `tests/test_maintenance.py` | 4 | 4 | 0 | Background thread VACUUM INTO and log pruning |
| `tests/test_no_network.py` | 4 | 4 | 0 | Offline local fall detection & TTS execution |
| `tests/test_notify_delivery.py` | 13 | 13 | 0 | Webhook outbox retry queue & backoff |
| `tests/test_notify_reliability.py` | 12 | 12 | 0 | Clear dead notifications API, unblocking /readyz |
| `tests/test_notify_webhook.py` | 6 | 6 | 0 | Outgoing robot payload parsing (DingTalk/Feishu) |
| `tests/test_production_blockers.py` | 6 | 6 | 0 | Preflight blockers, frozen frame sentinel protection |
| `tests/test_production_preflight.py` | 6 | 6 | 0 | Production env/config validation |
| `tests/test_replay_mute.py` | 1 | 1 | 0 | Video replay mode |
| `tests/test_rules_drugs.py` | 3 | 3 | 0 | Drug rule evaluation |
| `tests/test_runtime_hardening.py` | 6 | 6 | 0 | Subprocess isolation & signal handling |
| `tests/test_safety_consistency.py` | 6 | 6 | 0 | Cross-module safety invariant enforcement |
| `tests/test_safety_slice1.py` | 19 | 19 | 0 | VLA action sandboxing, speaking tail restoration, state protection |
| `tests/test_scene_engine.py` | 31 | 31 | 0 | SceneEngine YAML DSL, triggers, conditions, choose branches |
| `tests/test_sentinel.py` | 8 | 8 | 0 | EdgeSentinel 5fps benchmarks, motion/quiet transitions |
| `tests/test_vla_agent.py` | 18 | 18 | 0 | VLA policy, motion throttling, sentinel occupancy tracking, prompt schemas |
| `tests/test_vla_ops.py` | 3 | 3 | 0 | VLA mode state trigger wiring, heartbeat loop |
| `tests/test_voice_loop.py` | 7 | 7 | 0 | Continuous voice listening loop & VAD |
| **TOTAL** | **393** | **393** | **0** | **100% Passing** |
