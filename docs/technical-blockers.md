# Technical Blockers & Root Cause Analysis · VMAPBL
**Date**: 2026-11-04  
**Orchestrator**: Lead Architect / Orchestrator  
**Project**: VMAPBL (Apple Silicon Elder-Care Butler) vs. Reference ("硬核DIY：不到400元，我给85岁外婆造了个AI管家")  

---

## 1. Overview of Key Bottlenecks

Through architectural and functional comparison between VMAPBL and the reference project, 5 key technical bottlenecks were identified that prevent full feature parity:

| Blocker ID | Domain | Severity | Bottleneck Description | Feasibility Category | Resolution Strategy |
|---|---|---|---|---|---|
| **BLOCKER-001** | Architecture / Smart Home | **High** | Absence of Home Assistant REST/WebSocket client bridge | **A (Directly Implementable)** | Implement `skills/ha.py` & configure `homeassistant:` in `settings.yaml` |
| **BLOCKER-002** | Scenarios / Automation | **Medium** | Night get-up uses ANSI terminal blink instead of real light control | **A (Directly Implementable)** | Upgrade `actions/getup.py` with HA light/switch service linkage & fallback |
| **BLOCKER-003** | Interaction / Smart Home | **Medium** | Voice loop lacks home appliance intent parsing & control | **A (Directly Implementable)** | Add `actions/appliance.py` & `scenes/appliance_control.yaml` for lights & AC |
| **BLOCKER-004** | Assisted Features / Vision | **Medium** | Photo OCR reading lacks specialized medication dosage structuring | **A (Directly Implementable)** | Enhance `actions/photo_ocr.py` with specialized medication leaflet prompt & schema |
| **BLOCKER-005** | Communication / Network | **Low** | DingTalk callback requires inbound HTTP webhook (no Stream mode) | **B (Implementable via Adapter)** | Provide `tools/dingtalk_stream_gateway.py` Stream mode WebSocket bridge |

---

## 2. In-Depth Root Cause Analysis (RCA) & Solution Design

---

### BLOCKER-001: Missing Native Home Assistant Bidirectional Integration Bridge

#### 1. Target Behavior
In the reference project, Home Assistant acts as the central hub ("Body"):
- It registers and communicates with smart home devices (lights, switches, smart plugs, AC via infrared/Zigbee).
- It receives AI events from the AI agent and synchronizes device states.
- It displays dashboard entities and status cards for family members.

#### 2. Current Implementation in VMAPBL
- `docs/ha-integration-plan-2026-11-01.md` outlined a conceptual integration plan.
- However, zero code exists in `skills/` or `actions/` to communicate with Home Assistant.
- `skills/notify.py` allows sending generic webhook POSTs, but cannot query entity states or invoke Home Assistant services (e.g. `light.turn_on`, `climate.set_temperature`).

#### 3. Functional Gap
VMAPBL cannot inspect home sensors (e.g. door contact sensor, bed occupancy sensor) nor can it trigger physical smart home actuators.

#### 4. Root Cause
Architectural omission: VMAPBL was initially scoped as an autonomous vision-and-voice agent running on a Mac, without a dedicated Home Assistant client adapter.

#### 5. Feasibility Category
**Category A**: Can be directly implemented in the current codebase without external library additions, using standard `httpx.AsyncClient` communicating with Home Assistant REST API (`/api/services/{domain}/{service}` and `/api/states/{entity_id}`).

#### 6. Solution Architecture
- Create `skills/ha.py`:
  - `HomeAssistantClient`:
    - `call_service(domain: str, service: str, target: dict, service_data: dict) -> dict`
    - `get_state(entity_id: str) -> dict | None`
    - `set_state(entity_id: str, state: str, attributes: dict) -> dict`
    - `is_available() -> bool`
  - Safe error handling: when HA is unreachable or unconfigured, log warning and return safe fallback without throwing unhandled exceptions.
- Add configuration section in `config/settings.yaml`:
  ```yaml
  homeassistant:
    enabled: false
    base_url: "http://192.168.0.105:8123"
    token: ""  # Can also be set via HA_TOKEN env variable
    timeout_s: 5.0
    entities:
      night_light: "light.night_light"
      bed_sensor: "binary_sensor.bed_occupancy"
      door_sensor: "binary_sensor.door_contact"
      ac_climate: "climate.air_conditioner"
  ```

#### 7. Verification Plan
- Mock HA REST API server using `httpx.ASGITransport` / TestClient in unit tests.
- Verify `call_service`, `get_state`, and `set_state`.
- Verify graceful degradation when HA server is offline.

---

### BLOCKER-002: Night Get-Up Automation Uses Console Blink Stub

#### 1. Target Behavior
In the reference project, when the 85-year-old elder gets out of bed at night (e.g. between 23:00 and 06:00), the system automatically illuminates a soft night light (`light.night_light`) to prevent stumbling in the dark, and turns it off after returning to bed.

#### 2. Current Implementation in VMAPBL
In `actions/getup.py`:
```python
@skill("console_blink")
async def console_blink(ctx: dict) -> str:
    """控制台闪烁：ANSI 反色整行打印 6 次（本机无继电器时的物理反馈替代）。"""
    line = "🔔 老人已起身，请注意"
    for _ in range(6):
        print(f"\033[7m{line}\033[0m", flush=True)
    return "blinked"
```
The codebase explicitly commented that console blink was a temporary placeholder for lack of relays.

#### 3. Functional Gap
No actual lighting hardware is actuated; the elder cannot benefit from automated night illumination.

#### 4. Root Cause
Lack of actuator integration with smart home lighting devices.

#### 5. Feasibility Category
**Category A**: Can be resolved by integrating `skills/ha.py` into `actions/getup.py`.

#### 6. Solution
- Update `actions/getup.py`:
  - When `getup_decide` classifies `getup`, check if night light is configured.
  - Call `ha.call_service("light", "turn_on", entity_id=night_light, brightness_pct=30)`.
  - If HA is not configured or disabled, retain `console_blink` as a local fallback.

#### 7. Verification Plan
- Unit test in `tests/test_m5_scenes.py` verifying that `get_up` triggers HA night light activation when enabled, and falls back to console blink when disabled.

---

### BLOCKER-003: Voice Loop Lacks Home Appliance Control Intent

#### 1. Target Behavior
Reference project allows natural voice control for home appliances ("帮我开灯", "关灯", "太热了/开空调", "把空调设为26度").

#### 2. Current Implementation in VMAPBL
- `skills/voice_loop.py` handles speech intent classification: currently routes to `msg_to_family` (messaging), `photo` (reading), or general talk.
- No appliance control action exists.

#### 3. Functional Gap
Elder cannot use voice commands to control lights, fans, or air conditioners.

#### 4. Root Cause
Omission of appliance control domain in intent router and scene actions.

#### 5. Feasibility Category
**Category A**: Can be implemented directly by adding `actions/appliance.py`, registering skill `appliance_control`, and adding `scenes/appliance_control.yaml`.

#### 6. Solution
- Create `actions/appliance.py`:
  - Recognizes keywords or invokes LLM to extract: `domain` (`light`/`climate`/`switch`/`fan`), `action` (`turn_on`/`turn_off`/`set_temperature`), and parameters.
  - Dispatches calls via `skills/ha.py`.
  - Responds with reassuring voice feedback ("奶奶，空调已经为您开到26度了" / "灯已经为您打开了").

#### 7. Verification Plan
- Unit test verifying intent extraction and service dispatch across common colloquial commands ("开灯", "关灯", "太热了", "把空调开到26度").

---

### BLOCKER-004: Medication OCR Reading Lacks Structured Dosage Guidance

#### 1. Target Behavior
When an elder shows a medicine box or leaflet to the camera, the reference project extracts the key instructions and explains them in plain, conversational language: "奶奶，这个药叫降压片，每天早饭后吃一次，一次吃一片，用温开水送服。".

#### 2. Current Implementation in VMAPBL
- `actions/photo_ocr.py` has a generic prompt that reads any text verbatim.
- It does not distinguish medication leaflets from generic documents, and does not structure drug dosage, timing, and cautions into elder-friendly summaries.

#### 3. Functional Gap
Reading raw technical leaflet text verbatim overwhelms an elderly person with dense small-print terminology and warnings.

#### 4. Root Cause
Prompt and schema in `actions/photo_ocr.py` were designed for generic text reading rather than specialized medication guidance.

#### 5. Feasibility Category
**Category A**: Can be directly implemented by enhancing `actions/photo_ocr.py` with intent detection: if the query mentions "药", "怎么吃", or "说明书", invoke a specialized medical dosage summarization prompt.

#### 6. Solution
- In `actions/photo_ocr.py`:
  - Detect medication intent (`medication_intent = any(k in question for k in ("药", "怎么吃", "吃几片", "用量", "说明书"))`).
  - Use structured schema: `drug_name`, `usage`, `dosage`, `frequency`, `cautions`, `speech_summary`.
  - Format output into natural spoken Chinese for `tts.speak`.

#### 7. Verification Plan
- Add unit test verifying that medicine packaging OCR text produces clear, structured spoken instructions.

---

### BLOCKER-005: Inbound Webhook Dependency for DingTalk (Missing Stream Mode)

#### 1. Target Behavior
Reference project `dingtalk-ha-gateway` supports DingTalk Stream Mode: establishing an outbound WebSocket connection to DingTalk cloud, which receives bot messages inside private residential LANs without public IPs, port forwarding, or domain names.

#### 2. Current Implementation in VMAPBL
- `server/app.py` exposes `POST /api/webhook/family_reply` which expects inbound HTTP POST callbacks from DingTalk/Feishu.
- In private residential networks without public IP or port forwarding, external DingTalk webhooks cannot reach `http://192.168.0.105:8321/api/webhook/family_reply` directly unless through reverse proxy (e.g. FRP/Cloudflare Tunnel) or Stream Mode.

#### 3. Functional Gap
Setting up public ingress requires extra networking skills from the user.

#### 4. Root Cause
Inbound HTTP webhook architecture vs. outbound persistent WebSocket stream architecture.

#### 5. Feasibility Category
**Category B**: Can be resolved by providing a lightweight DingTalk Stream adapter (`tools/dingtalk_stream_gateway.py`) that uses the official `dingtalk-stream` client to receive messages over outbound WebSocket and forward them locally to `http://127.0.0.1:8321/api/webhook/family_reply`.

#### 6. Solution
- Provide `tools/dingtalk_stream_gateway.py` as an optional background daemon.
- When enabled with `DINGTALK_CLIENT_ID` and `DINGTALK_CLIENT_SECRET`, it runs an outbound stream connection and delivers incoming events locally.

---
