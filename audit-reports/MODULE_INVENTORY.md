# Module Inventory · VMAPBL Codebase
**Date**: 2026-11-04  
**Audit Protocol**: Gemini 3.8 Flash Deep Audit Protocol  
**Repository**: `/Users/liuruixuan/VMAPBL`  
**Total Source Code Scale**: ~9,647 lines (Python + YAML + Plist) + ~4,800 lines of tests (380 tests)

---

## Subsystem 1: Core Infrastructure (`core/`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `core.bus` | `core/bus.py` | 77 | In-process asynchronous event bus with pattern matching, sequential await | High | **Verified** | Sequential handler dispatch can cause head-of-line blocking if async handlers delay. |
| `core.state` | `core/state.py` | 65 | Global in-memory state store for occupancy, motion, medication, and TTS speaking | High | **Verified** | **AUDIT-003**: 100% in-memory; state wiped on process crash or restart. |
| `core.config` | `core/config.py` | 138 | Configuration loader for `config/settings.yaml`, ROI polygon updater | High | **Reviewed** | Writes ROI directly back to YAML; has threading lock but YAML overwrites comments. |
| `core.scene_engine` | `core/scene_engine.py` | 611 | YAML DSL parser, static validation, cooldown enforcement, priority arbiter | Critical | **Verified** | Robust load-time DSL validation; single-mode re-entrancy blocking verified. |
| `core.health` | `core/health.py` | 59 | System health & readiness evaluation for `/readyz`, heartbeat, and watchdog | Critical | **Verified** | **AUDIT-001**: `configured_channels > 0` causes permanent 503 if no webhook is set. |
| `core.log` | `core/log.py` | 372 | SQLite database wrapper (events, api_calls, messages, outbox, medications) | High | **Verified** | Outbox bounded exponential backoff (max 8 attempts), WAL mode enabled. |
| `core.vla_agent` | `core/vla_agent.py` | 810 | Vision-Language-Action agent policy, action dispatcher, fallback heuristics | Critical | **Verified** | In-memory `_silence_until`; emergency escalation normalization; fallback catches generic `still`. |
| `core.heartbeat` | `core/heartbeat.py` | 25 | Heartbeat file writer/reader for external watchdog | Medium | **Reviewed** | Simple atomic write using JSON replace. |
| `core.preflight` | `core/preflight.py` | 83 | Pre-flight production checks (tokens, weights, file permissions) | High | **Reviewed** | Only enforced when `--require-production` is explicitly passed. |
| `core.logging_setup` | `core/logging_setup.py` | 55 | Rotating file logger setup (10MB x 5) | Low | **Reviewed** | Operational logging intact. |

---

## Subsystem 2: Sensors & Hardware Ingestion (`sensors/`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `sensors.camera` | `sensors/camera.py` | 362 | AVFoundation/RTSP camera stream, ring buffer, reconnection supervisor | High | **Verified** | Automatic retry on transient disconnects; credentials stripped from RTSP logging. |
| `sensors.perception` | `sensors/perception.py` | 300 | MOG2 background subtraction motion gate (30fps) + Pose estimator (0.5s throttle) | Critical | **Verified** | Double-gate hysteresis (0.15/0.05) prevents flapping; pose warmup eliminates first-frame freeze. |
| `sensors.sentinel` | `sensors/sentinel.py` | 817 | 160x120 edge frame-differencing sentinel (5fps) for VLA mode | High | **Verified** | Bounded priority queue; drops non-urgent events under load; fires on every `motion_start`. |
| `sensors.pose` | `sensors/pose.py` | 177 | CoreML / PyTorch YOLO26n-pose wrapper, keypoint extraction, ANE acceleration | High | **Verified** | Graceful fallback from `.mlpackage` to `.pt`. |
| `sensors.detect` | `sensors/detect.py` | 69 | YOLO object detector for pillbox (`models/pillbox.pt`) | High | **Verified** | **AUDIT-005**: `models/pillbox.pt` is missing from disk; detector permanently unavailable. |
| `sensors.geometry` | `sensors/geometry.py` | 176 | Geometric utilities: point-in-polygon, torso inclination, fall kinematic classifier | High | **Verified** | Torso angle and hip drop rate algorithms verified by unit tests. |
| `sensors.motion` | `sensors/motion.py` | 176 | MOG2 OpenCV wrapper with morphological opening and warmup frame suppression | Medium | **Verified** | Morphological filtering removes camera noise effectively. |
| `sensors.microphone` | `sensors/microphone.py` | 89 | PyAudio 48kHz microphone stream, ring buffer, 16kHz resampling | High | **Verified** | Audio capture handles underrun gracefully. |
| `sensors.vad` | `sensors/vad.py` | 65 | Silero VAD / WebRTC VAD wrapper for speech boundary detection | High | **Reviewed** | Silence padding and max utterance truncation (15s). |

---

## Subsystem 3: Skills & External Capabilities (`skills/`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `skills.notify` | `skills/notify.py` | 442 | Persistent outbox notification sender (DingTalk, Feishu, Webhook) with receipts | Critical | **Verified** | Bounded retry worker; DingTalk/Feishu platform business receipt validation. |
| `skills.tts` | `skills/tts.py` | 73 | macOS `say` subprocess wrapper, half-duplex mutual exclusion, tail silence | Critical | **Verified** | Process kill on timeout/cancel; `--` argument delimiter prevents command injection. |
| `skills.stt` | `skills/stt.py` | 147 | `faster-whisper` speech-to-text, lazy singleton, executor isolation | High | **Verified** | Half-duplex background abort when foreground safety inquiry triggers. |
| `skills.vision` | `skills/vision.py` | 200 | Cloud VLM / OpenAI compatible vision API caller, token cost accounting | High | **Verified** | Tracks prompt/completion tokens and logs cost into `api_calls` table. |
| `skills.ocr` | `skills/ocr.py` | 58 | Apple Vision OCR (`pyobjc-framework-Vision`) for fast on-device text reading | Medium | **Verified** | Headless sandbox execution fails due to macOS Apple Neural Engine bundle cache permissions. |
| `skills.intent` | `skills/intent.py` | 60 | Keyword & LLM-based intent routing for user voice commands | Medium | **Reviewed** | Fast local keyword match before LLM escalation. |
| `skills.family_router` | `skills/family_router.py` | 53 | Routes incoming family messages: status query vs message to elder | Medium | **Reviewed** | Keyword whitelist with length threshold (<= 24 chars). |
| `skills.voice_loop` | `skills/voice_loop.py` | 87 | Background continuous voice listening supervisor | Medium | **Verified** | Backoff retry on transient audio device disconnects. |
| `skills.llm` | `skills/llm.py` | 44 | General LLM text completion wrapper | Low | **Reviewed** | Fallback text completion for non-vision tasks. |
| `skills.medication_text` | `skills/medication_text.py` | 35 | Medication confirmation text parser & slot classifier | Low | **Reviewed** | Filters refusal phrases and matches meal slots. |

---

## Subsystem 4: Actions & Scene Implementations (`actions/`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `actions.fall` | `actions/fall.py` | 125 | Scene 7: Kinematic fall verification, voice inquiry, negation guard, silence arming | Critical | **Verified** | Negation guard prevents false alerts on "没摔"; 30-min silence arms only on delivery. |
| `actions.medication` | `actions/medication.py` | 214 | Scene 4: Medication reminder, pillbox tracking, wrist proximity, voice fallback | High | **Verified** | Visual path completely disabled due to missing `models/pillbox.pt`; fallback works. |
| `actions.door` | `actions/door.py` | 70 | Scene 3: Door key reminder, inside vs outside ROI vote, VLM fallback | High | **Verified** | **AUDIT-005**: `door_outside` missing in `settings.yaml`, causes silent early return "unknown". |
| `actions.wakeup` | `actions/wakeup.py` | 128 | Scene 5: Morning wake-up check (08:30/09:00), bed ROI overlap, escalation chain | High | **Verified** | In-memory `_pending` dict resets on process reboot. |
| `actions.getup` | `actions/getup.py` | 46 | Scene 6: Getting up posture detection, torso inclination angle, rollover filter | Medium | **Verified** | Discriminates between rollover and upright sitting. |
| `actions.family_query` | `actions/family_query.py` | 63 | Scene 2: Remote family status inquiry, VLM frame analysis, fall suspicion | High | **Verified** | Strict bool check on `uncertain` and `on_floor`; float cast protection. |
| `actions.messaging` | `actions/messaging.py` | 82 | Scene 1 & 1b: Bidirectional family messaging, refusal word filter | High | **Verified** | Outbox receipts integrated; speech feedback updates on delivery status. |
| `actions.photo_ocr` | `actions/photo_ocr.py` | 53 | Scene 8: Photo reading, OCR first then VLM escalation | Medium | **Verified** | Output fed directly to TTS without semantic safety guardrail. |
| `actions.demo` | `actions/demo.py` | 10 | Demo scene action | Low | **Reviewed** | Simple greeting action. |

---

## Subsystem 5: Server & Web Console (`server/`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `server.app` | `server/app.py` | 460 | FastAPI REST server, WebSocket endpoint, static files, webhook handler | Critical | **Verified** | **AUDIT-004**: No token required when `BUTLER_TOKEN` is unset; input text capped at 200 chars. |
| `server.auth` | `server/auth.py` | 45 | Bearer token verification and WebSocket Origin filtering | High | **Verified** | Unconfigured token allows unauthenticated access to all endpoints. |
| `server.webhook_auth` | `server/webhook_auth.py` | 79 | DingTalk / Feishu callback signature verification, timestamp anti-replay | High | **Verified** | Unconfigured secret permits unsigned webhook callbacks. |
| `server.dashboard` | `server/dashboard.py` | 214 | Real-time dashboard state summary, timeline aggregator, health aggregator | High | **Verified** | **AUDIT-002**: `_wake_info` ignores event date, leaking yesterday's wake-up time. |
| `server.push` | `server/push.py` | 88 | WebSocket push hub with client tracking and closed connection cleanup | Medium | **Verified** | Dead websocket connection cleanup handles client drops cleanly. |
| `server.rules` | `server/rules.py` | 28 | Medication schedule and reminder rules data holder | Low | **Reviewed** | Medication inventory tracking. |

---

## Subsystem 6: Deployment & Standalone Runner (`deploy/`, `tools/`, `VMAPBL Butler.app`)

| Module | File Path | Lines | Role & Capabilities | Priority | Audit Status | Key Findings / Risks |
|---|---|---|---|---|---|---|
| `launcher` | `VMAPBL Butler.app/Contents/MacOS/launcher` | 67 | macOS App bundle launcher shell script | High | **Verified** | Launches `main.py --serve` without `--require-production`; pops up false degraded alert. |
| `guardian.plist` | `deploy/com.local.butler.guardian.plist.template` | 42 | LaunchAgent template wrapping `caffeinate -dimsu` and `main.py --serve` | High | **Verified** | Omits `--require-production`; relies on Interactive GUI session. |
| `watchdog.plist` | `deploy/com.local.butler.watchdog.plist.template` | 35 | LaunchAgent template running `tools/healthcheck.py` every 60 seconds | High | **Verified** | Alerts independently to secondary webhook channel. |
| `tools.healthcheck` | `tools/healthcheck.py` | 106 | Independent watchdog script checking `data/heartbeat.json` | High | **Verified** | Standalone stdlib urllib; rate-limited alert repeats (1800s). |
| `tools.install_launchd` | `tools/install_launchd.py` | 88 | Template renderer and LaunchAgent installer | Medium | **Verified** | Enforces placeholder elimination before file write. |
| `tools.permission_check` | `tools/permission_check.py` | 323 | Interactive hardware and macOS TCC permission validator | Medium | **Reviewed** | Validates camera, mic, and write permissions interactively. |
