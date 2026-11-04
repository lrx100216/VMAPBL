# Repair Progress Tracker · VMAPBL Autonomous Remediation
**Date**: 2026-11-04  
**Protocol**: Antigravity Autonomous Repair Protocol (Gemini 3.8 Flash High)  
**Workspace**: `/Users/liuruixuan/VMAPBL`  

---

## 1. Summary Metrics
- **Total Issues Identified in Audit**: 20
- **Confirmed Real Issues**: 19
- **False Positives**: 1 (AUDIT-014: `bed` is strictly an ROI in Scene 6 wakeup, not a 4th medication slot)
- **Remediated & Verified Issues**: 19
- **In Progress**: 0
- **Blocked**: 0
- **Remaining Open Issues**: 0

---

## 2. Issue Repair Matrix

| Issue ID | Title | Priority | Target Modules | Status | Verification Evidence |
|---|---|---|---|---|---|
| **AUDIT-001** | `/readyz` 503 lockup on empty webhooks / dead outbox | **P0** | `core/health.py`, `core/log.py`, `server/app.py` | **FIXED** | `clear_dead_notifications()`, `POST /api/notifications/clear-dead`; `tests/test_notify_reliability.py` passed (12/12) |
| **AUDIT-002** | Dashboard wake-up cross-day leakage | **P0** | `server/dashboard.py` | **FIXED** | `_today_start_ts()` midnight timestamp boundary; `tests/test_dashboard_live.py` passed (6/6) |
| **AUDIT-003** | Core state persistence (medication, silence, wakeup) | **P0** | `core/log.py`, `core/state.py`, `core/vla_agent.py`, `actions/wakeup.py` | **FIXED** | `system_state` SQLite table, `restore_persisted()`, `_silence_until` persistence; `test_bus_state.py`, `test_vla_agent.py`, `test_m5_scenes.py` passed |
| **AUDIT-004** | API token enforcement & launcher `--require-production` | **P0** | `core/config.py`, `core/preflight.py`, `server/auth.py`, `launcher` | **FIXED** | Dual `BUTLER_TOKEN` / `BUTLER_API_TOKEN` support, `--require-production` verification; `tests/test_auth.py`, `test_app_bundle.py` passed |
| **AUDIT-005** | Missing `door_outside` ROI in `settings.yaml` | **P1** | `config/settings.yaml`, `actions/door.py` | **FIXED** | Added default `door_outside` polygon to `roi:`, missing ROI logging; `tests/test_m5_scenes.py` passed |
| **AUDIT-006** | Missing `models/pillbox.pt` detector weights / fallback | **P1** | `models/`, `actions/medication.py` | **FIXED** | Created `models/` with `README.md` workflow guide, graceful voice fallback auditing in `actions/medication.py`; `tests/test_m6_scenes.py` passed |
| **AUDIT-007** | Fall negation asymmetry ("不是没事" cancels alarm) | **P0** | `actions/fall.py`, `core/vla_agent.py` | **FIXED** | Negation prefix filtering (`没/不/非/未/别`) for `fine_keywords`; `tests/test_audit_hardening.py` passed |
| **AUDIT-008** | Apple Speech Synthesis command injection in TTS (`[[slnc]]`) | **P1** | `skills/tts.py` | **FIXED** | Regex tag stripping `re.sub(r"\[\[.*?\]\]", "", text)`; `tests/test_audit_hardening.py` passed |
| **AUDIT-009** | Webhook verification fail-closed when secrets unconfigured | **P0** | `server/webhook_auth.py` | **FIXED** | Fail-closed policy when `BUTLER_TOKEN` set or platform headers present without secret; `tests/test_auth.py` passed |
| **AUDIT-010** | VLA mode `state.occupancy` locked at False | **P1** | `sensors/sentinel.py`, `core/vla_agent.py` | **FIXED** | `sentinel` sets `occupancy=True` on motion, resets after 30s quiet; `UPDATE_STATE` accepts `occupancy`; `tests/test_vla_agent.py` passed |
| **AUDIT-011** | VLA mode unbounded token economics on `motion_start` | **P1** | `core/vla_agent.py` | **FIXED** | 60s cooldown throttle on mundane `motion_start`/`stop` without speech; `tests/test_vla_agent.py` passed |
| **AUDIT-012** | VLA mode wire-up bypasses SceneEngine triggers | **P2** | `main.py` | **FIXED** | Wired `engine.wire_state_triggers()` in `_wire_vla`; `tests/test_vla_ops.py` passed (3/3) |
| **AUDIT-013** | Perception gate 2 occupancy timeout on crumpled fall pose | **P0** | `sensors/motion.py`, `sensors/perception.py` | **FIXED** | `occupied_during` lookback across motion spike window `[quiet_since - spike_window_s, quiet_since]`; `tests/test_m4_perception.py` passed |
| **AUDIT-014** | Missing `bed` slot in `StateStore` causes `KeyError` | **P2** | `core/state.py` | **FALSE POSITIVE** | `bed` is strictly an ROI in Scene 6 (`wakeup`), not a medication slot. Reverted invalid `bed` slot to preserve 3-slot schema `(morning, noon, evening)`; tests passed |
| **AUDIT-015** | Synchronous `VACUUM INTO` in main event loop | **P1** | `main.py` | **FIXED** | Offloaded `log.prune_history` and `log.backup_to` via `await asyncio.to_thread(...)`; `tests/test_maintenance.py` passed |
| **AUDIT-016** | Local USB camera disconnect lacks reconnection logic | **P1** | `sensors/camera.py` | **FIXED** | Added 30 consecutive read failure threshold to trigger `_reconnect()` using `_open_capture()`; `tests/test_camera_stream.py` passed |
| **AUDIT-017** | EventBus `CancelledError` drops remaining handlers | **P2** | `core/bus.py` | **FIXED** | Wrapped handler await with `asyncio.shield` and caught `asyncio.CancelledError`; `tests/test_bus_state.py` passed |
| **AUDIT-018** | TTS FIFO lock priority inversion (urgent speech preempts chat) | **P1** | `skills/tts.py` | **FIXED** | `urgent=True` speech preempts/cancels ongoing non-urgent speech; `tests/test_audit_hardening.py` passed |
| **AUDIT-019** | Missing CORS & CSRF protection on mutation routes | **P2** | `server/app.py` | **FIXED** | Installed `CORSMiddleware`, added origin check on mutation requests rejecting external origins with 403; `tests/test_auth.py` passed |
| **AUDIT-020** | Webhook replay vulnerability (missing nonce cache) | **P1** | `server/webhook_auth.py` | **FIXED** | In-memory sliding TTL (300s) nonce/signature replay cache with `clear_nonce_cache()`; `tests/test_auth.py` passed |
