# Remaining Issues Tracker · VMAPBL
**Date**: 2026-11-04  
**Protocol**: Antigravity Autonomous Repair Protocol (Gemini 3.8 Flash High)  
**Workspace**: `/Users/liuruixuan/VMAPBL`  

---

## 1. Status Overview
- **Confirmed Issues from Audit**: 20
- **Issues Remediated & Verified**: 19
- **Issues Categorized as False Positive**: 1 (AUDIT-014)
- **Unresolved Open Issues**: 0
- **Environment-Blocked Issues**: 0

---

## 2. Issues Assessment & Resolution Summary

### False Positives
- **AUDIT-014**: *StateStore Missing `bed` Medication Slot*
  - **Audit Claim**: Missing a 4th `bed` slot in `StateStore.medication_taken` raises `KeyError` when recording bedtime drugs.
  - **Investigation Finding**: In VMAPBL domain models (`config/settings.yaml`, `skills/medication_text.py`, `actions/medication.py`, `StateStore`), `bed` is strictly an ROI polygon used in Scene 6 (`wakeup`) to classify elder `in_bed` vs `out_of_bed`. The medication schedule contract throughout the codebase strictly defines 3 daily slots: `morning`, `noon`, and `evening` (`slot_labels: {morning: 早上, noon: 中午, evening: 晚上}`). Adding `"bed"` as a medication slot broke existing contract tests (`test_state_reset_daily_medication` and `test_api_state_reflects_store`).
  - **Resolution**: Reverted invalid 4th slot; preserved canonical 3-slot schema while adding thread-safe locking and SQLite persistence.

### Resolved Issues (19/19)
All other 19 issues (AUDIT-001 through AUDIT-013, AUDIT-015 through AUDIT-020) have been fully remediated, verified via targeted unit tests, and regression tested across the full suite.

---

## 3. Residual Risks & Recommended Future Improvements
1. **Apple Vision OCR Offline Environment**: `skills/ocr.py` requires macOS graphical login / TCC entitlements to compile ANE models. In headless CI/sandbox environments, it gracefully falls back to VLM vision inspection (`skills/vision.py`).
2. **Local Camera Disconnect Alerting**: While local camera reconnection is now implemented (30-frame retry trigger), USB bus power resets may occasionally require up to 5 seconds to re-enumerate on macOS hardware.
3. **Speech Preemption Race**: Urgent speech preemption cancels ongoing TTS immediately. In high-frequency alternating dialogue, rapid speech interruption may clip preceding messages; an emergency chime / sound cue is recommended for future UX polish.
