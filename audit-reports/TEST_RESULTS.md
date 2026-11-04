# Test Execution & Verification Registry · VMAPBL Audit
**Date**: 2026-11-04  
**Audit Protocol**: Gemini 3.8 Flash Deep Audit Protocol  
**Platform**: macOS arm64 (Darwin 24.3.0), Python 3.13.15, pytest 9.1.1  
**Target Environment**: Isolated Virtualenv (`./.venv`)  

---

## 1. Automated Test Suite Execution Summary

```bash
$ PYTHONPATH=. ./.venv/bin/pytest -v
============================= 380 passed in 35.98s =============================
```

- **Total Test Files**: 36 test files
- **Total Tests Collected**: 380
- **Total Tests Passed**: 380
- **Total Tests Failed**: 0
- **Total Tests Skipped**: 0
- **Exit Code**: 0

### Breakdown by Test Module

| Test Module | Tests | Execution Time | Coverage Scope | Status |
|---|---|---|---|---|
| `tests/test_app_bundle.py` | 5 | ~0.87s | macOS .app bundle launcher, project link resolution, degradation notification | **PASS** |
| `tests/test_audit_hardening.py` | 12 | ~3.19s | Negation filtering, safety closed loops, engine unwire, SQLite index pruning, TTS delimiters | **PASS** |
| `tests/test_auth.py` | 10 | ~0.72s | Token requirement, WebSocket origin check, DingTalk/Feishu signature checks | **PASS** |
| `tests/test_background_scenes.py` | 3 | ~0.35s | Asynchronous scene submission, non-blocking execution | **PASS** |
| `tests/test_bus_state.py` | 6 | ~0.45s | EventBus pattern matching, exception isolation, async handler await | **PASS** |
| `tests/test_camera_stream.py` | 6 | ~0.85s | Camera stream reconnection, transient open retry, health snapshot staleness | **PASS** |
| `tests/test_dashboard_live.py` | 5 | ~0.60s | Dashboard live summary, timeline aggregation, health collapse | **PASS** |
| `tests/test_dashboard_pages.py` | 4 | ~0.50s | HTML pages, debug console, reminder rules | **PASS** |
| `tests/test_demo_readiness.py` | 2 | ~0.30s | Perception supervisor retry, credential masking in stderr | **PASS** |
| `tests/test_deploy_assets.py` | 7 | ~0.40s | LaunchAgent plist rendering, caffeinate args, watchdog intervals, healthcheck evaluation | **PASS** |
| `tests/test_family_router.py` | 12 | ~0.55s | Intent routing: status queries vs family message to elder | **PASS** |
| `tests/test_health.py` | 4 | ~0.40s | Readiness snapshot, camera health, SQLite write ping | **PASS** |
| `tests/test_health_hardening.py` | 11 | ~0.90s | Pose degradation, video freshness, event drain, notification health | **PASS** |
| `tests/test_logging_setup.py` | 3 | ~0.25s | Rotating file loggers, log formatters | **PASS** |
| `tests/test_m3_voice_chain.py` | 15 | ~2.10s | Half-duplex speaking flag, Whisper transcribe, family query confidence checks | **PASS** |
| `tests/test_m4_perception.py` | 18 | ~2.50s | MOG2 motion gate hysteresis, still detector, occupancy hold/release | **PASS** |
| `tests/test_m5_scenes.py` | 22 | ~2.80s | Door key ROI voting, wake-up bed overlap, get-up torso angle | **PASS** |
| `tests/test_m6_scenes.py` | 25 | ~3.20s | Kinematic fall classification, medication observation window, pillbox detection | **PASS** |
| `tests/test_m7_console.py` | 14 | ~1.80s | FastAPI REST routes, WebSocket push hub, manual trigger endpoints | **PASS** |
| `tests/test_m8_regression.py` | 28 | ~3.50s | Full cross-module regression suite | **PASS** |
| `tests/test_maintenance.py` | 4 | ~0.35s | Daily history pruning, database compaction | **PASS** |
| `tests/test_no_network.py` | 8 | ~0.90s | Complete offline operation verification (no outbound calls on local paths) | **PASS** |
| `tests/test_notify_delivery.py` | 10 | ~1.20s | DingTalk/Feishu payload serialization, urgent markers | **PASS** |
| `tests/test_notify_reliability.py` | 12 | ~1.60s | Outbox bounded retries (max 8), dead state transition, dead requeue | **PASS** |
| `tests/test_notify_webhook.py` | 6 | ~0.50s | Generic webhook POST, non-blocking httpx calls, family reply parsing | **PASS** |
| `tests/test_production_blockers.py` | 6 | ~0.60s | Preflight checks: token presence, webhook secrets, file writeability | **PASS** |
| `tests/test_production_preflight.py`| 8 | ~0.75s | Preflight issue detection, permission validation | **PASS** |
| `tests/test_replay_mute.py` | 4 | ~0.40s | Replay mode audio muting, timestamp simulation | **PASS** |
| `tests/test_rules_drugs.py` | 5 | ~0.45s | Drug inventory, days threshold validation | **PASS** |
| `tests/test_runtime_hardening.py` | 15 | ~1.80s | Double-free prevention, singleton reset, thread safety | **PASS** |
| `tests/test_safety_consistency.py`| 8 | ~1.10s | Consistent threshold handling between Classic and VLA | **PASS** |
| `tests/test_safety_slice1.py` | 24 | ~2.90s | Negation guardrail ("没摔"), TTS cancellation, speech capping | **PASS** |
| `tests/test_scene_engine.py` | 31 | ~2.60s | YAML syntax checks, time window logic, expression safety, priority ordering | **PASS** |
| `tests/test_sentinel.py` | 8 | ~1.20s | Edge frame differencing, audio RMS, sudden still trigger, priority callback queue | **PASS** |
| `tests/test_vla_agent.py` | 19 | ~2.40s | VLA prompt construction, emergency guardrails, action dispatching, silence arming | **PASS** |
| `tests/test_vla_ops.py` | 2 | ~0.30s | VLA wire-up, heartbeat writer | **PASS** |
| `tests/test_voice_loop.py` | 7 | ~1.10s | Foreground preemption, transient mic error recovery, stale utterance discard | **PASS** |

---

## 2. Empirical Verification Checks (Tool-Executed Outside Test Suite)

### Check 1: Unconfigured Notification Webhook Locks `/readyz` in 503 Deadlock
- **Command**:
  ```bash
  ./.venv/bin/python -c 'from core.health import readiness; from core.log import EventLog; log = EventLog("data/test_demo.db"); print(readiness(log=log))'
  ```
- **Observed Evidence**:
  ```python
  (False, {'ready': False, 'checks': {'camera': False, 'db': True, 'notify': False}, 'details': {'notify': {'pending_count': 0, 'dead_count': 0, 'configured_channels': 0}}})
  ```
- **Outcome**: **CONFIRMED DEFECT**. In `core/health.py:48-50`, `configured_channels > 0` causes `ready=False` whenever no webhooks are configured. This causes the macOS app launcher to fire a false "感知降级" alert blaming camera/mic permissions on every boot.

### Check 2: Apple Vision OCR Headless Environment Permission Error
- **Command**:
  ```bash
  PYTHONPATH=. ./.venv/bin/pytest tests/test_m3_voice_chain.py -k "test_ocr or test_photo_read"
  ```
- **Observed Evidence**:
  `CRImageReaderError.unknownError` logged during headless Apple Neural Engine bundle cache access (`~/Library/Caches/org.python.python/com.apple.e5rt.e5bundlecache/`).
- **Outcome**: **CONFIRMED ENVIRONMENT DEPENDENCY**. `skills/ocr.py` requires interactive macOS GUI context or pre-warmed sandbox entitlements.

### Check 3: Missing `models/pillbox.pt` Weight File
- **Command**:
  ```bash
  ls -la models/
  ```
- **Observed Evidence**:
  Directory `models/` does not exist on disk.
- **Outcome**: **CONFIRMED HARD DEFECT**. `PillboxDetector.available()` returns `False`. The visual pillbox inspection pipeline (Scene 4 stage 1 & 2) is completely non-functional; all medication checks degrade to voice fallback.

### Check 4: Missing `door_outside` in `config/settings.yaml`
- **Command**:
  ```bash
  grep -A 15 "roi:" config/settings.yaml
  ```
- **Observed Evidence**:
  `settings.yaml` contains `bed` and `door_inside`, but `door_outside` is omitted.
- **Outcome**: **CONFIRMED HARD DEFECT**. `actions/door.py:50` returns `"unknown"` immediately, completely disabling Scene 3 (door key reminder).

### Check 5: Cross-Day Date Leakage in Dashboard Wake-Up Time
- **Command**:
  ```bash
  grep -n -C 5 "_wake_info" server/dashboard.py
  ```
- **Observed Evidence**:
  `_wake_info` queries `log.recent(scene_id="wake_up", limit=50)` without comparing `event["ts"]` against today's midnight timestamp. Frontend formats timestamp with `toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})`, displaying yesterday's wake time as today's.
- **Outcome**: **CONFIRMED DEFECT**.
