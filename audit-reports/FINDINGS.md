# Comprehensive Findings Register · VMAPBL Audit
**Date**: 2026-11-04  
**Audit Protocol**: Gemini 3.8 Flash Deep Audit Protocol  
**Repository**: `/Users/liuruixuan/VMAPBL`  

---

### Issue ID: AUDIT-001
- **Title**: `/readyz` Permanent 503 Deadlock on Unconfigured Webhooks or Dead Outbox Messages
- **Severity**: Critical
- **Category**: Availability & Reliability
- **File Path**: `core/health.py`, `server/app.py`
- **Line Number**: `core/health.py:48-50`, `server/app.py:100-110`
- **Evidence**: 
  Direct execution of `readiness(log=log)` with empty settings yields:
  `{'ready': False, 'checks': {'camera': False, 'db': True, 'notify': False}, 'details': {'notify': {'pending_count': 0, 'dead_count': 0, 'configured_channels': 0}}}`.
- **Trigger Condition**: 
  1. Booting VMAPBL without external DingTalk/Feishu webhook configured (`configured_channels == 0`).
  2. Any emergency notification exhausting 8 retry attempts, permanently transitioning to `dead`.
- **Root Cause**: 
  `core/health.py:48-50` enforces `checks["notify"] = (configured_channels > 0 and health["pending_count"] == 0 and health["dead_count"] == 0)`.
- **Impact**: 
  System `/readyz` returns 503 permanently. The macOS double-click launcher pops up a misleading alert ("感知降级：请检查摄像头/麦克风权限"), and the independent watchdog fires continuous repeated alerts.
- **Verification Status**: Confirmed (reproduced via unit test & python execution)
- **Recommended Fix**: 
  Separate external notification health from service readiness. If no webhook channels are configured, mark `notify` as `disabled/local_only` (True for local ready). Treat `dead` notifications as warnings rather than service-level outages.
- **Priority**: P0
- **Estimated Effort**: 2 hours
- **Regression Risk**: Low
- **Acceptance Criteria**: `/readyz` returns 200 when camera and database are healthy, even without external webhooks configured.

---

### Issue ID: AUDIT-002
- **Title**: Dashboard Timeline & Wake-Up Time Cross-Day Data Leakage
- **Severity**: High
- **Category**: Business Logic & Data Integrity
- **File Path**: `server/dashboard.py`, `server/static/index.html`
- **Line Number**: `server/dashboard.py:46-55`, `server/static/index.html:299-303, 580`
- **Evidence**: 
  `_wake_info` iterates `log.recent(scene_id="wake_up", limit=50)` without filtering by today's date. The frontend formats the timestamp with `toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})`.
- **Trigger Condition**: 
  Any morning before 08:30 or before the elder gets out of bed.
- **Root Cause**: 
  The query retrieves the last 50 historical events across all days and accepts the first `out_of_bed` match regardless of age.
- **Impact**: 
  Yesterday's (or last week's) wake-up time is rendered on today's dashboard as "已起床 08:30", falsely assuring family members that an unresponsive or fallen elder is already awake and active.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  In `_wake_info`, calculate today's midnight timestamp (`datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()`) and discard events where `event["ts"] < today_midnight`.
- **Priority**: P0
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Dashboard displays "未确认" for wake time on a new day until an `out_of_bed` event from today is recorded.

---

### Issue ID: AUDIT-003
- **Title**: Core Operational States are Purely In-Memory and Lost on Restart
- **Severity**: Critical
- **Category**: Software Architecture & Durability
- **File Path**: `core/state.py`, `core/vla_agent.py`, `actions/wakeup.py`, `core/scene_engine.py`
- **Line Number**: `core/state.py:18-27`, `core/vla_agent.py:99`, `actions/wakeup.py:25-30`, `core/scene_engine.py:223`
- **Evidence**: 
  `StateStore.medication_taken`, `_silence_until`, `_pending["date"]`, and `_time_fired` are plain Python dictionary/float attributes with zero write-back to SQLite.
- **Trigger Condition**: 
  Any process crash, unhandled exception, macOS restart, or launchd automatic reload.
- **Root Cause**: 
  Architectural absence of a state persistence and recovery layer for volatile domain entities.
- **Impact**: 
  - Daily medication records are wiped, resulting in duplicate voice reminders and false alarms to family.
  - The 30-minute post-fall silence window is wiped, causing the system to immediately re-alert and harass an injured elder awaiting rescue.
  - Morning schedule triggers double-fire upon restart.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Store `medication_taken`, `_silence_until`, and `_time_fired` in a new SQLite table `system_state(key TEXT PRIMARY KEY, val TEXT, updated_ts REAL)` and rehydrate `StateStore` on startup.
- **Priority**: P0
- **Estimated Effort**: 4 hours
- **Regression Risk**: Medium
- **Acceptance Criteria**: Killing and restarting the Butler process retains `medication_taken` status and active silence timestamps.

---

### Issue ID: AUDIT-004
- **Title**: Default Unauthenticated API Access & Production Preflight Check Bypass
- **Severity**: Critical
- **Category**: Authentication & Authorization
- **File Path**: `server/auth.py`, `core/config.py`, `main.py`, `VMAPBL Butler.app/Contents/MacOS/launcher`, `deploy/com.local.butler.guardian.plist.template`
- **Line Number**: `server/auth.py:26-28, 33-35`, `core/config.py:128-130`, `main.py:423-431`, `launcher:44`, `guardian.plist.template:15`
- **Evidence**: 
  `config.butler_token()` only checks `BUTLER_TOKEN`. When empty, `api_authorized` and `ws_authorized` return `True` unconditionally. Furthermore, neither `launcher` nor `guardian.plist` passes `--require-production`.
- **Trigger Condition**: 
  Running VMAPBL via double-clicking `VMAPBL Butler.app` or via LaunchAgent without configuring `BUTLER_TOKEN`.
- **Root Cause**: 
  Fail-open design in authentication middleware and omission of `--require-production` in daemon launchers.
- **Impact**: 
  Any process on the local network or local host can invoke `/api/trigger/{scene_id}`, manipulate medication records via `/api/drugs`, disable rules via `/api/rules`, or stream real-time audio/visual events via `/ws`.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  1. In `core/config.py`, check both `BUTLER_TOKEN` and `BUTLER_API_TOKEN`.
  2. In `server/auth.py`, fail closed: reject unauthenticated requests unless `--insecure-dev` is explicitly passed.
  3. Add `--require-production` to `launcher` and `guardian.plist.template`.
- **Priority**: P0
- **Estimated Effort**: 2 hours
- **Regression Risk**: Medium
- **Acceptance Criteria**: Launching without token prevents API modifications and warns the operator.

---

### Issue ID: AUDIT-005
- **Title**: Missing `door_outside` ROI in `settings.yaml` Renders Scene 3 Permanently Inoperable
- **Severity**: Critical
- **Category**: Configuration & Functional Integrity
- **File Path**: `config/settings.yaml`, `actions/door.py`
- **Line Number**: `config/settings.yaml:165-190`, `actions/door.py:49-51`
- **Evidence**: 
  `config/settings.yaml` contains `roi.bed` and `roi.door_inside`, but `roi.door_outside` is absent. `actions/door.py:50` evaluates `if not inside or not outside: return "unknown"`.
- **Trigger Condition**: 
  Elder walking out the door or arriving home.
- **Root Cause**: 
  Incomplete default ROI calibration entries in `config/settings.yaml`.
- **Impact**: 
  Scene 3 (Door Key Reminder) always evaluates to `"unknown"` and never broadcasts any reminder ("奶奶，出门别忘记拿钥匙"). The core key reminder feature is 100% dead.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Provide default polygon coordinates for `door_outside` in `config/settings.yaml` and update `tools/calibrate_roi.py` to prompt for both inside and outside door zones.
- **Priority**: P1
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Triggering `door_key` with pose history inside or outside generates `"out"` or `"in"` decisions and speaks the corresponding reminder.

---

### Issue ID: AUDIT-006
- **Title**: Missing `models/pillbox.pt` Weight File Completely Disables Visual Medication Inspection
- **Severity**: High
- **Category**: Deployment & Functional Completeness
- **File Path**: `sensors/detect.py`, `actions/medication.py`
- **Line Number**: `sensors/detect.py:41-43`, `actions/medication.py:163-165`
- **Evidence**: 
  Directory `models/` is missing from the repository. `PillboxDetector.available()` checks `path.exists()` and returns `False`.
- **Trigger Condition**: 
  Medication schedule execution (`medication_watch`).
- **Root Cause**: 
  Trained model artifact `models/pillbox.pt` was never committed or bundled with the workspace.
- **Impact**: 
  Scene 4's primary two-stage visual inspection pipeline (Stage 1: pillbox present, Stage 2: wrist near box) is completely dead. The system permanently falls back to pure voice inquiry.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Supply pre-trained weights or include a synthetic mock detector for test/demo environments, or document the exact training workflow in the setup guide.
- **Priority**: P1
- **Estimated Effort**: 4 hours
- **Regression Risk**: Low
- **Acceptance Criteria**: `PillboxDetector.available()` returns `True` when weights are in place, allowing visual stage 1 & 2 execution.

---

### Issue ID: AUDIT-007
- **Title**: Fall Action Negation Asymmetry Cancels Real Fall Alarms on Rejection Replies
- **Severity**: Critical
- **Category**: Safety & Edge Case Handling
- **File Path**: `actions/fall.py`
- **Line Number**: `actions/fall.py:71-88, 109-115`
- **Evidence**: 
  `_distress_in_reply` checks for negation prefixes on `distress_keywords`, but `fine_keywords` (`["没事", "没摔", "还好", "自己起来"]`) are never checked for negations.
- **Trigger Condition**: 
  Elder falls, system asks "奶奶，您还好吗？", and elder replies: "不是没事" or "一点也不好" or "我可没好".
- **Root Cause**: 
  Line 112 matches `"没事"` as a substring of `"不是没事"`. Because `"不是没事"` contains no distress keyword, `_distress_in_reply` returns `False`, causing the check to evaluate to `True` (treated as fine).
- **Impact**: 
  The system speaks "好的，那您慢一点，注意安全" and cancels the alarm (`return "cancelled"`). An injured elder who explicitly states they are NOT fine has their emergency alert dropped.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Implement symmetric negation detection on `fine_keywords`: if a fine keyword is preceded by a negation prefix (`没/不/非/别`), it must not match.
- **Priority**: P0
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Unit test verifying "不是没事" and "一点也不好" triggers emergency escalation rather than cancellation.

---

### Issue ID: AUDIT-008
- **Title**: Apple Speech Synthesis Command Injection in TTS Enables Audio Denial of Service
- **Severity**: High
- **Category**: Security & Input Validation
- **File Path**: `skills/tts.py`, `actions/messaging.py`, `actions/photo_ocr.py`
- **Line Number**: `skills/tts.py:30-36, 56`, `actions/messaging.py:69`
- **Evidence**: 
  macOS `say` interprets commands enclosed in `[[ ... ]]`, such as `[[slnc 60000]]` (pause for 60s) or `[[volm 0.0]]` (mute). External messages from family and text read by OCR are formatted directly into `tts.speak`.
- **Trigger Condition**: 
  Receiving a family message or photographing a paper containing `[[slnc 60000]]`.
- **Root Cause**: 
  `skills/tts.py` invokes `say -- text` without stripping embedded speech synthesis command tags.
- **Impact**: 
  The `say` subprocess hangs silently for 60 seconds. During this window, `state.speaking` remains `True`, causing all microphone frames to be dropped (half-duplex enforcement) and completely deafening the device. Emergency alarms are blocked behind the TTS lock.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  In `skills/tts.py:speak()`, sanitize text using regex `re.sub(r"\[\[.*?\]\]", "", text)`.
- **Priority**: P1
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Spoken text containing `[[slnc 10000]]` has the tag stripped and speaks without pausing.

---

### Issue ID: AUDIT-009
- **Title**: Webhook Verification Bypass When Signing Secrets are Unconfigured
- **Severity**: Critical
- **Category**: Security & Webhook Authentication
- **File Path**: `server/webhook_auth.py`, `server/app.py`
- **Line Number**: `server/webhook_auth.py:50-79`, `server/app.py:80-84, 222-233`
- **Evidence**: 
  In `server/webhook_auth.py:79`, if `dingtalk_secret` and `feishu_secret` are empty, `verify_webhook` returns `(True, "no_secret_configured")`.
- **Trigger Condition**: 
  Sending an HTTP POST to `/api/webhook/family_reply` on a default installation.
- **Root Cause**: 
  Fail-open design in webhook authentication: unconfigured secrets are treated as valid rather than rejected.
- **Impact**: 
  Any unauthorized party on the local network can post forged messages or queries, triggering arbitrary speech to the elder or triggering camera frame queries.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Change default in `verify_webhook` to return `(False, "no_secret_configured")` when secrets are absent.
- **Priority**: P0
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Unsigned requests to `/api/webhook/family_reply` return HTTP 401 when secrets are unconfigured.

---

### Issue ID: AUDIT-010
- **Title**: VLA Mode State Divergence: `state.occupancy` Permanently Locked at `False`
- **Severity**: High
- **Category**: Architectural Divergence & Logic Flaw
- **File Path**: `sensors/sentinel.py`, `core/vla_agent.py`
- **Line Number**: `sensors/sentinel.py:470-473, 508-509`, `core/vla_agent.py:606-612`
- **Evidence**: 
  In VLA mode, `EdgeSentinel` only checks frame differencing and never updates `state.occupancy`. Furthermore, `core/vla_agent.py` restricts `UPDATE_STATE` exclusively to `is_still`.
- **Trigger Condition**: 
  Running in `--mode vla`.
- **Root Cause**: 
  Omission of occupancy tracking in `EdgeSentinel` combined with rigid state update restrictions in `vla_agent`.
- **Impact**: 
  `state.occupancy` remains `False` forever. The Web dashboard displays "画面无人" even when the elder is moving in front of the camera, and scenes relying on `occupancy == true` fail to trigger.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Allow `UPDATE_STATE` in `VLAPolicy` to update `occupancy` based on visual analysis, or add lightweight person detection to `EdgeSentinel`.
- **Priority**: P1
- **Estimated Effort**: 2 hours
- **Regression Risk**: Low
- **Acceptance Criteria**: Dashboard reflects active occupancy when moving in front of camera in VLA mode.

---

### Issue ID: AUDIT-011
- **Title**: VLA Mode Unbounded Token Economics & Latency on Mundane `motion_start`
- **Severity**: High
- **Category**: Performance & Financial Viability
- **File Path**: `sensors/sentinel.py`, `core/vla_agent.py`, `main.py`
- **Line Number**: `sensors/sentinel.py:772-784`, `core/vla_agent.py:496-506`, `main.py:314-319`
- **Evidence**: 
  `run_sentinel` emits `motion_start` on every transition above threshold, and `main.py` passes all events directly to `decide_and_act()`, triggering a full cloud VLM API call.
- **Trigger Condition**: 
  Normal daily movement in front of the camera in VLA mode.
- **Root Cause**: 
  Lack of event filtering, rate-limiting, and local tiered triage in the VLA event loop.
- **Impact**: 
  High cloud API token costs, API rate limit exhaustion (HTTP 429), and 1.5–3.5s latency delays on every motion start.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Throttle non-urgent events (`motion_start`, `motion_stop`) with a cooldown window (e.g. at most once every 60s), reserving immediate VLM calls for `sudden_still` and distress audio.
- **Priority**: P1
- **Estimated Effort**: 3 hours
- **Regression Risk**: Medium
- **Acceptance Criteria**: Continuous movement triggers at most one cloud VLM call within the throttle window.

---

### Issue ID: AUDIT-012
- **Title**: VLA Mode Wire-Up Bypasses SceneEngine State and Sensor Triggers
- **Severity**: High
- **Category**: Architecture & Feature Coverage
- **File Path**: `main.py`
- **Line Number**: `main.py:133-142`
- **Evidence**: 
  `_wire_vla(engine)` only calls `wire_time_triggers()` and `wire_speech_triggers()`. It explicitly omits `wire_state_triggers()` and passes `skip_prefixes=("sensor.",)` to `wire_event_triggers()`.
- **Trigger Condition**: 
  Running with `--mode vla`.
- **Root Cause**: 
  Incomplete integration between the VLA agent loop and the classic SceneEngine DSL.
- **Impact**: 
  Scene 3 (door key), Scene 6 (get-up), and Scene 7 (classic fall) are completely disabled and never arbitrated in VLA mode.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Harmonize event routing so that VLA either completely replaces SceneEngine with full feature parity or coordinates with SceneEngine triggers cleanly.
- **Priority**: P2
- **Estimated Effort**: 4 hours
- **Regression Risk**: High
- **Acceptance Criteria**: All 8 user scenes execute correctly in both Classic and VLA modes.

---

### Issue ID: AUDIT-013
- **Title**: Perception Gate 2 Occupancy Timeout (3s) Suppresses `sudden_still` on Real Falls
- **Severity**: Critical
- **Category**: Safety & Perception
- **File Path**: `sensors/perception.py`, `sensors/motion.py`
- **Line Number**: `sensors/perception.py:261-270`, `sensors/motion.py:148-155`
- **Evidence**: 
  `sensor.sudden_still` event requires `occupancy_at_quiet = quiet_since is not None and occ.occupied(quiet_since)`. `OccupancyTracker.hold_s` is 3.0s.
- **Trigger Condition**: 
  Elder falls, collapses onto the floor in an obscured or crumpled pose where YOLO pose confidence drops below 0.5.
- **Root Cause**: 
  If more than 3 seconds elapse between the last recognized upright pose and the moment quiet stillness is confirmed, `occupied()` returns `False`.
- **Impact**: 
  The `sensor.sudden_still` event is silently suppressed. Scene 7 is never triggered, causing a catastrophic silent miss of a real fall emergency.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Extend occupancy hold time upon high-motion spikes, or look back at occupancy across the entire spike window rather than solely at `quiet_since`.
- **Priority**: P0
- **Estimated Effort**: 2 hours
- **Regression Risk**: Medium
- **Acceptance Criteria**: Fall simulation with obscured post-fall pose triggers `sensor.sudden_still`.

---

### Issue ID: AUDIT-014
- **Title**: Missing `bed` Slot in `core/state.py` Causes Unhandled `KeyError`
- **Severity**: Medium
- **Category**: Data Model & Error Handling
- **File Path**: `core/state.py`
- **Line Number**: `core/state.py:22-23, 44-46`
- **Evidence**: 
  `self.medication_taken` is initialized with keys `("morning", "noon", "evening")`. `mark_medication(slot)` raises `KeyError(f"未知药顿次 {slot}")` if `slot == "bed"`.
- **Trigger Condition**: 
  Attempting to mark or record a bedtime medication slot.
- **Root Cause**: 
  Inconsistency between 4-slot medication schedules (morning, noon, evening, bed) and `StateStore` 3-slot schema.
- **Impact**: 
  Unhandled `KeyError` crashes the caller.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Add `"bed": False` to `StateStore.medication_taken` and update `slot_for_time()`.
- **Priority**: P2
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: `mark_medication("bed", True)` succeeds without exception.

---

### Issue ID: AUDIT-015
- **Title**: Synchronous `VACUUM INTO` in Main Event Loop Freezes System
- **Severity**: High
- **Category**: Performance & Concurrency
- **File Path**: `main.py`, `core/log.py`
- **Line Number**: `main.py:116`, `core/log.py:351`
- **Evidence**: 
  `_heartbeat_loop` runs on the main asyncio thread and calls `log.backup_to(...)` directly, which executes synchronous `VACUUM INTO` under lock.
- **Trigger Condition**: 
  Daily maintenance rollover (midnight).
- **Root Cause**: 
  Blocking I/O executed on the asyncio event loop thread.
- **Impact**: 
  Freezes the entire event loop for 1 to 5 seconds depending on database size, dropping video/audio frames and triggering false watchdog timeouts.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Offload database maintenance to an executor thread: `await asyncio.to_thread(log.backup_to, ...)`.
- **Priority**: P1
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Database backup executes in background worker thread without delaying event loop tasks.

---

### Issue ID: AUDIT-016
- **Title**: Local USB Camera Disconnect Lacks Reconnection Logic, Causing Infinite Loop
- **Severity**: High
- **Category**: Hardware Resilience
- **File Path**: `sensors/camera.py`
- **Line Number**: `sensors/camera.py:221-230`
- **Evidence**: 
  When `ok, frame = self._cap.read()` fails, `if self.source_is_stream: self._reconnect(); continue; time.sleep(0.05); continue`. Local cameras (`source_is_stream == False`) never call `_reconnect()`.
- **Trigger Condition**: 
  USB camera unplugged, bus glitch, or macOS camera sleep.
- **Root Cause**: 
  Reconnection logic was only wired for network RTSP streams, omitting local camera devices.
- **Impact**: 
  Thread loops at 20Hz forever reading failed frames; camera never recovers until the whole application is killed and restarted.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Add retry counter: if local camera read fails for 30 consecutive frames, call `_reconnect()` to re-open the device handle.
- **Priority**: P1
- **Estimated Effort**: 2 hours
- **Regression Risk**: Low
- **Acceptance Criteria**: Re-plugging a disconnected USB camera automatically recovers frame capture within 5 seconds.

---

### Issue ID: AUDIT-017
- **Title**: EventBus Unhandled `CancelledError` Drops Remaining Event Handlers
- **Severity**: Medium
- **Category**: Concurrency & Error Propagation
- **File Path**: `core/bus.py`
- **Line Number**: `core/bus.py:48-55`
- **Evidence**: 
  `publish()` catches `Exception`, which does not catch `asyncio.CancelledError`.
- **Trigger Condition**: 
  Cancellation of a publishing task while awaiting one of multiple handlers.
- **Root Cause**: 
  In Python 3.8+, `CancelledError` derives from `BaseException`.
- **Impact**: 
  Subsequent subscribers registered for the event never receive notification.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Shield handler dispatch or iterate with isolation to guarantee event delivery.
- **Priority**: P2
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Cancelling a caller task does not prevent remaining bus subscribers from receiving the event.

---

### Issue ID: AUDIT-018
- **Title**: TTS FIFO Lock Priority Inversion Blocks Emergency Inquiries
- **Severity**: High
- **Category**: Concurrency & Life Safety
- **File Path**: `skills/tts.py`
- **Line Number**: `skills/tts.py:20, 60-74`
- **Evidence**: 
  `_lock = asyncio.Lock()` provides strict FIFO ordering. A non-urgent chat response blocks emergency inquiries.
- **Trigger Condition**: 
  Elder falls while the assistant is speaking a general message or chat response.
- **Root Cause**: 
  Single unprioritized FIFO lock governing all speech output.
- **Impact**: 
  The critical secondary fall verification question ("奶奶您还好吗？") is blocked waiting behind non-urgent speech for up to 60 seconds.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Introduce an interruptible prioritized speech queue: urgent speech cancels ongoing non-urgent speech and jumps to the head of the queue.
- **Priority**: P1
- **Estimated Effort**: 3 hours
- **Regression Risk**: Medium
- **Acceptance Criteria**: Emergency speech interrupts non-urgent speech immediately.

---

### Issue ID: AUDIT-019
- **Title**: Missing CORS & CSRF Protection on State-Changing REST Endpoints
- **Severity**: Medium
- **Category**: Web Security
- **File Path**: `server/app.py`
- **Line Number**: `server/app.py:73-84`
- **Evidence**: 
  FastAPI application does not install `CORSMiddleware`. Endpoints do not validate `Origin` or `Referer` headers.
- **Trigger Condition**: 
  User visits a malicious webpage in a web browser on the same local network while VMAPBL is running.
- **Root Cause**: 
  Absence of CORS configuration and CSRF tokens for mutating REST endpoints.
- **Impact**: 
  A web page can trigger cross-origin POST requests to `/api/trigger/{scene_id}` or mutate rules.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Install `CORSMiddleware` restricted to `127.0.0.1` and configured origins.
- **Priority**: P2
- **Estimated Effort**: 1 hour
- **Regression Risk**: Low
- **Acceptance Criteria**: Cross-origin requests from unauthorized external origins are rejected with 403 Forbidden.

---

### Issue ID: AUDIT-020
- **Title**: Webhook Replay Vulnerability Due to Lack of Nonce Invalidation
- **Severity**: High
- **Category**: Security & Webhook Handling
- **File Path**: `server/webhook_auth.py`
- **Line Number**: `server/webhook_auth.py:16-75`
- **Evidence**: 
  Timestamp skew window is 300 seconds (`MAX_SKEW_S = 300`). Nonces are never cached or invalidated.
- **Trigger Condition**: 
  Re-sending an intercepted webhook payload within 5 minutes.
- **Root Cause**: 
  Absence of a processed-request nonce/signature cache.
- **Impact**: 
  Attackers can replay intercepted messages repeatedly, causing repeated loud TTS announcements and duplicate actions.
- **Verification Status**: Confirmed
- **Recommended Fix**: 
  Maintain an in-memory TTL set of seen nonces/signatures for 300 seconds. Reject duplicates with 409 Conflict.
- **Priority**: P1
- **Estimated Effort**: 2 hours
- **Regression Risk**: Low
- **Acceptance Criteria**: Replaying an identical webhook request within 5 minutes is rejected.
