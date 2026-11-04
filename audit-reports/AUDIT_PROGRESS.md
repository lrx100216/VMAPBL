# Audit Progress Log · VMAPBL Deep Audit
**Date**: 2026-11-04  
**Harness**: Google Antigravity / Gemini 3.8 Flash (High Thinking)  
**Workspace**: `/Users/liuruixuan/VMAPBL`  
**Git Branch**: `feat/p0-safety-loop` (HEAD: 9e9bb048693ab0612b2821b3f1b9a0788445e98a + 25 modified files)  

---

## Stages Status

| Stage | Name | Status | Details |
|---|---|---|---|
| **Stage 1** | **Preflight** | **COMPLETED** | Git repo checked, Python 3.13 venv validated, uncommitted diff (25 files) inspected, 380 unit tests collected. |
| **Stage 2** | **Planning** | **COMPLETED** | Module breakdown established, plan artifact generated, task checkpoint `3319ac3d57fd4986a44ac2c5319e86db` registered in Hermes memory. |
| **Stage 3** | **Delegation** | **COMPLETED** | 3 specialized Subagents spawned: `arch-auditor` (`d47bd1cf`), `security-auditor` (`28b479a1`), `code-logic-auditor` (`ee3b2d17`). |
| **Stage 4** | **Deep Inspection** | **IN PROGRESS** | Manual cross-module inspection underway across `core/`, `actions/`, `sensors/`, `server/`, `skills/`, `tools/`, `deploy/`. |
| **Stage 5** | **Verification** | **IN PROGRESS** | Pytest 380/380 tests passed in 35.98s recorded via `task_verification_record`. Direct Python execution of readiness probe failure confirmed. |
| **Stage 6** | **Independent Review** | **PENDING** | Subagent results reconciliation, false-positive elimination, reverse auditing. |
| **Stage 7** | **Reporting** | **PENDING** | Generate `AUDIT_REPORT.md`, update `FINDINGS.md`, `ARCHITECTURE.md`. |
| **Stage 8** | **Final Integrity Check** | **PENDING** | Verify exact line numbers, code references, and readiness gate. |

---

## Key Milestone Observations
1. **Dynamic Verification Baseline**: Full test suite passes: `380 passed in 35.98s` under Python 3.13.15 + pytest 9.1.1.
2. **Readiness Probe Lockup**: `core/health.py:48` enforces `configured_channels > 0`, causing `/readyz` to permanently fail (503) whenever external webhook URLs are not configured, triggering misleading launcher warnings ("感知降级：请检查摄像头/麦克风权限").
3. **Cross-Day Stale Data Exposure**: `server/dashboard.py:46` reads the most recent `out_of_bed` event without date filtering; `server/static/index.html` displays only `HH:MM`, so yesterday's wake-up time is shown today.
4. **VLA vs Classic Architectural Duality**: In VLA mode, `main.py:_wire_vla` does not wire state triggers (breaking Scene 3 door key) and skips `sensor.*` triggers (breaking Scene 6 get-up). Every `motion_start` triggers an expensive, high-latency cloud VLM call.
5. **State Durability Deficit**: `StateStore` (`core/state.py`), `_silence_until` (`core/vla_agent.py`), and `_pending` (`actions/wakeup.py`) are purely in-memory structures that vanish on process reboot.
