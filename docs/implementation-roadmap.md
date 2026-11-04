# Implementation Roadmap & Engineering Schedule · VMAPBL
**Project**: VMAPBL Functional Parity with Reference AI Butler  
**Date**: 2026-11-04  
**Author**: Lead Software Architect  

---

## 1. Prioritized Work Packages

```text
[Sprint 1: Core HA Client & Configuration] (P0)
   ├── skills/ha.py (HomeAssistantClient: REST API & Service Calling)
   ├── config/settings.yaml (homeassistant configuration schema)
   └── tests/test_ha_integration.py (Mocked HA API client unit tests)

[Sprint 2: Smart Night Lighting on Get-Up] (P1)
   ├── actions/getup.py (Integrate HA light.turn_on with fallback to console_blink)
   └── tests/test_ha_integration.py (Verify get-up night lighting flow)

[Sprint 3: Appliance Voice Intent Control] (P1)
   ├── actions/appliance.py (Natural language appliance intent parsing & execution)
   ├── scenes/appliance_control.yaml (Appliance control scene definition)
   └── tests/test_appliance_control.py (Voice command intent tests)

[Sprint 4: Structured Medication OCR Reading] (P1)
   ├── actions/photo_ocr.py (Specialized medication leaflet & packaging prompt)
   └── tests/test_photo_ocr.py (Medicine dosage explanation tests)

[Sprint 5: DingTalk Stream Mode Adapter] (P2)
   ├── tools/dingtalk_stream_gateway.py (Outbound WebSocket stream client)
   └── docs/ha-integration-plan-2026-11-01.md (Documentation update)

[Sprint 6: Full Regression & Multi-Level Verification] (P0)
   ├── Comprehensive test execution (393 -> 405+ tests)
   ├── docs/verification-report.md (Evidence recording)
   └── hermes-memory verification records
```

---

## 2. Detailed Task Breakdown

### Task 1: Home Assistant Integration Module (`skills/ha.py`)
- **Objective**: Provide reliable, non-blocking asynchronous communication with Home Assistant REST API.
- **Components**:
  - `HomeAssistantClient`:
    - `call_service(domain, service, service_data, target)`
    - `get_state(entity_id)`
    - `set_state(entity_id, state, attributes)`
    - `is_available()`
  - Configuration loading from `config/settings.yaml` under `homeassistant:` block, supporting `HA_TOKEN` and `HA_URL` environment variables.
  - Zero-exception policy: network timeouts or connection refusals are logged with warnings and return graceful failure dictionaries (`{"ok": False, "error": ...}`) instead of crashing calling scenes.

### Task 2: Smart Night Lighting in `actions/getup.py`
- **Objective**: When elder gets up at night, actuate a real smart night light in Home Assistant.
- **Components**:
  - Check `config.settings().get("homeassistant", {}).get("entities", {}).get("night_light")`.
  - If configured and HA is reachable, invoke `light.turn_on` with soft brightness (e.g. 25-30%).
  - Graceful fallback: maintain `console_blink` if HA is unconfigured or offline.

### Task 3: Appliance Voice Intent Control (`actions/appliance.py`)
- **Objective**: Translate spoken commands ("开灯", "把空调开到26度", "太热了", "把客厅电扇关了") into HA service calls with natural elder feedback.
- **Components**:
  - Register skill `appliance_control(ctx: dict) -> dict`.
  - Intent classification logic (rules + fast LLM extraction for domain, entity, service, and parameters).
  - Voice responses tuned for elderly care ("奶奶，已经把空调开到26度了").

### Task 4: Medication Leaflet OCR Guidance (`actions/photo_ocr.py`)
- **Objective**: Specialize Scene 8 for medicine packaging and instruction leaflets.
- **Components**:
  - Distinguish generic paper from medicine packaging by checking for keywords (`"药"`, `"怎么吃"`, `"一天吃几次"`, `"用量"`).
  - Use structured extraction schema: `drug_name`, `dosage`, `frequency`, `timing`, `precautions`, `speech_summary`.
  - Generate conversational, reassuring speech output for TTS.

### Task 5: DingTalk Stream Mode Adapter (`tools/dingtalk_stream_gateway.py`)
- **Objective**: Enable zero-public-IP inbound message delivery from DingTalk in residential networks.
- **Components**:
  - Provide a standalone background gateway script bridging DingTalk Stream connection to local Butler endpoint `/api/webhook/family_reply`.

---

## 3. Success & Acceptance Criteria
1. `skills/ha.py` passes all unit tests with 100% test coverage under simulated HA server environments.
2. Scene 6 (`get_up`) successfully invokes Home Assistant lighting service when enabled and falls back to console blink when disabled.
3. Appliance voice control accurately handles light, climate, switch, and fan control requests.
4. Medication OCR correctly parses drug name, dosage, and frequency into conversational Chinese speech.
5. Zero regressions across the existing 393 unit tests.
