# Repair Execution Log · VMAPBL Autonomous Remediation
**Date**: 2026-11-04  
**Protocol**: Antigravity Autonomous Repair Protocol (Gemini 3.8 Flash High)  
**Workspace**: `/Users/liuruixuan/VMAPBL`  

---

## Batch 1: P0 Critical Safety & Reliability Issues

### AUDIT-001: `/readyz` Permanent 503 Deadlock on Empty Webhooks / Dead Outbox
- **Root Cause**: `core/health.py` and `skills/notify.py` accumulated dead notifications in `notify_outbox` after 8 failed attempts, causing `/readyz` to permanently report unhealthy (`dead_count > 0`).
- **Fix**:
  1. Added `clear_dead_notifications()` in `core/log.py` to archive dead outbox entries.
  2. Added management API endpoint `POST /api/notifications/clear-dead` in `server/app.py`.
- **Verification**: `tests/test_notify_reliability.py` passed (12 passed in 1.10s).

### AUDIT-002: Dashboard Timeline & Wake-Up Time Cross-Day Data Leakage
- **Root Cause**: `_wake_info` in `server/dashboard.py` queried historical events without bounding timestamps to the current day.
- **Fix**:
  1. Added `_today_start_ts()` helper in `server/dashboard.py` computing local midnight timestamp.
  2. Filtered out events prior to `today_start`.
- **Verification**: `tests/test_dashboard_live.py::test_wake_info_suppresses_yesterday_event` passed.

### AUDIT-003: Core Operational States Lost on Restart (Medication, Silence, Wakeup)
- **Root Cause**: `medication_taken`, `_silence_until`, and `_pending["date"]` were stored exclusively in volatile process memory.
- **Fix**:
  1. Created `system_state` SQLite table in `core/log.py` with `set_state_val` and `get_state_val`.
  2. Added state restore and write-through persistence to `StateStore.mark_medication` and `StateStore.reset_daily_medication`.
  3. Added SQLite persistence for `_silence_until` in `core/vla_agent.py` and `_pending["date"]` in `actions/wakeup.py`.
- **Verification**: `tests/test_bus_state.py`, `tests/test_vla_agent.py`, `tests/test_m5_scenes.py` passed.

### AUDIT-004: API Token Enforcement & Production Preflight Check
- **Root Cause**: `core/config.py` and `core/preflight.py` only checked `BUTLER_TOKEN`, ignoring `BUTLER_API_TOKEN`.
- **Fix**:
  1. Supported both `BUTLER_TOKEN` and `BUTLER_API_TOKEN` in `core/config.py` and `core/preflight.py`.
  2. Preserved local development openness when unconfigured, while enforcing validation under `--require-production`.
- **Verification**: `tests/test_auth.py`, `tests/test_app_bundle.py` passed.

### AUDIT-007: Fall Action Negation Asymmetry Cancelling Real Fall Alarms
- **Root Cause**: `actions/fall.py` and `core/vla_agent.py` matched substrings of `fine_keywords` (e.g. `"没事"`) without checking for negation prefixes like `"不是没事"`, `"一点也不好"`.
- **Fix**:
  1. Added `_fine_in_reply` in `actions/fall.py` and `_fine_in_speech` in `core/vla_agent.py` with negation prefix lookahead (`没/不/非/未/别`).
- **Verification**: `tests/test_audit_hardening.py::test_fine_in_speech_negation` passed.

### AUDIT-009: Webhook Verification Fail-Closed When Secrets Unconfigured
- **Root Cause**: `verify_webhook` returned `(True, "no_secret_configured")` unconditionally when secrets were unset, allowing unauthenticated attackers on the LAN to forge incoming messages.
- **Fix**:
  1. Modified `server/webhook_auth.py` so that if `BUTLER_TOKEN` is configured or platform signature headers are supplied without matching secrets, requests are rejected with 401.
- **Verification**: `tests/test_auth.py::test_webhook_fails_closed_when_butler_token_set_and_secrets_unset` passed.

### AUDIT-013: Perception Gate 2 Occupancy Timeout on Crumpled Fall Pose
- **Root Cause**: When an elder collapses into a crumpled posture, YOLO pose detection drops below confidence threshold. If more than 3 seconds elapsed before quiet motion was confirmed, `occ.occupied(quiet_since)` returned False, silently dropping `sensor.sudden_still`.
- **Fix**:
  1. Added `occupied_during(start_ts, end_ts)` in `sensors/motion.py`.
  2. Updated Gate 2 in `sensors/perception.py` to inspect occupancy across the entire motion spike window `[quiet_since - spike_window_s, quiet_since]`.
- **Verification**: `tests/test_m4_perception.py::test_crumpled_fall_occupancy_hold` passed.

---

## Batch 2: P1 High-Priority Hardware, Safety & Security Issues

### AUDIT-005: Missing `door_outside` ROI in `settings.yaml`
- **Root Cause**: `config/settings.yaml` lacked `door_outside` polygon coordinates, causing `door_decision` to always return `"unknown"`.
- **Fix**:
  1. Added default `door_outside` polygon coordinates under `roi:` in `config/settings.yaml`.
  2. Added warning logs in `actions/door.py` when an ROI polygon is missing.
- **Verification**: `tests/test_m5_scenes.py` door tests passed.

### AUDIT-006: Missing `models/pillbox.pt` Detector Weights / Fallback
- **Root Cause**: Repository lacked `models/` directory, preventing visual pillbox inspection and lacking training documentation.
- **Fix**:
  1. Created `models/` directory with `README.md` documenting data collection and training via `tools/train_pillbox.py`.
  2. Added event logging for voice fallback in `actions/medication.py`.
- **Verification**: `tests/test_m6_scenes.py` passed (10/10).

### AUDIT-008: Apple Speech Synthesis Command Injection in TTS (`[[slnc]]`)
- **Root Cause**: External messages containing `[[slnc 60000]]` were passed directly to macOS `say`, freezing audio output and deafening microphone input.
- **Fix**:
  1. Added regex tag stripping in `skills/tts.py:speak()`: `text = re.sub(r"\[\[.*?\]\]", "", text or "")`.
- **Verification**: `tests/test_audit_hardening.py::test_tts_strips_apple_speech_command_injection` passed.

### AUDIT-010: VLA Mode `state.occupancy` Locked at False
- **Root Cause**: `EdgeSentinel` only tracked frame difference without updating `state.occupancy`.
- **Fix**:
  1. In `sensors/sentinel.py`, set `state.occupancy = True` upon detecting active motion, resetting to False after 30 seconds of quiet stillness.
- **Verification**: `tests/test_vla_agent.py::test_sentinel_updates_occupancy_on_motion` passed.

### AUDIT-011: VLA Mode Unbounded Token Economics on Mundane `motion_start`
- **Root Cause**: Every normal household movement triggered a full cloud VLM call via `decide_and_act`.
- **Fix**:
  1. Implemented a 60-second cooldown throttle for mundane `motion_start` and `motion_stop` events without speech in `VLAPolicy`.
  2. Preserved zero-latency dispatch for `sudden_still`, speech, and scheduled events.
- **Verification**: `tests/test_vla_agent.py::test_vla_motion_start_throttling` passed.

### AUDIT-015: Synchronous `VACUUM INTO` in Main Event Loop
- **Root Cause**: `main.py` executed `log.prune_history` and `log.backup_to` synchronously on the asyncio event loop thread during daily maintenance.
- **Fix**:
  1. Offloaded database operations via `await asyncio.to_thread(...)`.
- **Verification**: `tests/test_maintenance.py` passed.

### AUDIT-016: Local USB Camera Disconnect Lacks Reconnection Logic
- **Root Cause**: Camera thread only triggered `_reconnect()` for network RTSP streams; local device read failures entered an infinite 20Hz loop.
- **Fix**:
  1. Added a 30-consecutive-failure threshold for local camera reads to trigger `_reconnect()`.
  2. Updated `_reconnect()` to call `_open_capture()`, preserving device backend configurations.
- **Verification**: `tests/test_camera_stream.py::test_camera_local_reconnects_after_consecutive_failures` passed.

### AUDIT-018: TTS FIFO Lock Priority Inversion
- **Root Cause**: Non-urgent chat speech held `_lock`, blocking emergency inquiries ("奶奶您还好吗？") for up to 60 seconds.
- **Fix**:
  1. Added preemption in `skills/tts.py`: `speak(text, urgent=True)` cancels ongoing non-urgent speech tasks immediately.
  2. Added loop-aware `_get_lock()` to eliminate cross-test event loop binding issues.
- **Verification**: `tests/test_audit_hardening.py::test_tts_urgent_speech_preempts_non_urgent_speech` passed.

### AUDIT-020: Webhook Replay Vulnerability (Missing Nonce Cache)
- **Root Cause**: The 300-second timestamp window allowed intercepted requests to be replayed multiple times.
- **Fix**:
  1. Added in-memory sliding TTL (300s) nonce/signature cache `_seen_signatures` in `server/webhook_auth.py`.
  2. Replayed signatures are rejected immediately.
- **Verification**: `tests/test_auth.py::test_webhook_replay_protection_rejects_duplicate` passed.

---

## Batch 3: P2 Architecture & Edge Case Issues

### AUDIT-012: VLA Mode Wire-Up Bypasses SceneEngine Triggers
- **Root Cause**: `_wire_vla` omitted `engine.wire_state_triggers()`, preventing Scene 3 (`door_key`) from reacting to `state.occupancy`.
- **Fix**:
  1. Added `engine.wire_state_triggers()` in `main.py:_wire_vla()`.
- **Verification**: `tests/test_vla_ops.py::test_wire_vla_connects_state_triggers` passed.

### AUDIT-014: Missing `bed` Slot in `StateStore` (Evaluated: False Positive)
- **Investigation**: `bed` is strictly an ROI in Scene 6 (`wakeup`), not a medication slot. Medication slots are strictly `morning`, `noon`, `evening`.
- **Action**: Confirmed False Positive; maintained canonical 3-slot schema and added thread safety and persistence.

### AUDIT-017: EventBus `CancelledError` Drops Remaining Handlers
- **Root Cause**: `publish()` caught `Exception`, which does not catch `asyncio.CancelledError`, aborting handler dispatch if one handler was cancelled.
- **Fix**:
  1. Caught `asyncio.CancelledError` in `core/bus.py:publish()`, logged notice, and continued executing subsequent handlers.
- **Verification**: `tests/test_bus_state.py::test_bus_cancelled_handler_does_not_drop_subsequent_subscribers` passed.

### AUDIT-019: Missing CORS & CSRF Protection on Mutation Routes
- **Root Cause**: Missing CORSMiddleware and Origin validation allowed malicious websites on the LAN to POST commands to `/api/trigger` or `/api/msg_to_elder`.
- **Fix**:
  1. Added `CORSMiddleware` in `server/app.py` restricted to local/whitelisted origins.
  2. Added HTTP middleware checking `Origin` on mutating requests (POST/DELETE/PUT), rejecting unauthorized external origins with 403 Forbidden.
- **Verification**: `tests/test_auth.py::test_cross_origin_mutation_rejected` passed.
