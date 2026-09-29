"""L2.5 档：Apple Vision OCR（规范技术阶梯：读字 <50ms、¥0，优先于云端）。

API 已实证（pyobjc-framework-Vision 12.2.2，2026-09-22）：
- 路径：cv2.imencode JPEG → NSData → CIImage → VNImageRequestHandler(CIImage)；
- VNRecognizeTextRequest：setRecognitionLevel_(VNRequestTextRecognitionLevelAccurate)、
  setRecognitionLanguages_(["zh-Hans", "en-US"])、setUsesLanguageCorrection_；
- handler.performRequests_error_([request], None) → (True, None)；
- request.results() → VNRecognizedTextObservation → topCandidates_(1)[0]
  → .string() / .confidence()。
实证：cv2.putText 画的 "HELLO 123" 正确识别（置信度 0.50）。
"""
from __future__ import annotations

import cv2
import numpy as np

from core import config


def recognize_text(frame_bgr: np.ndarray) -> list[tuple[str, float]]:
    """识别一帧 BGR 图里的全部文字，返回 [(文字, 置信度)]（按置信度降序）。

    同步调用（Vision 实测 <50ms，可直接在事件循环里用）；任何异常返回 []。
    """
    try:
        cfg = config.settings().get("ocr", {})
        ok, jpg = cv2.imencode(".jpg", frame_bgr)
        if not ok:
            return []
        import Vision
        from Cocoa import NSData
        from Quartz import CIImage

        data = NSData.dataWithBytes_length_(jpg.tobytes(), jpg.size)
        ci_image = CIImage.imageWithData_(data)
        handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(
            ci_image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(list(cfg.get(
            "languages", ["zh-Hans", "en-US"])))
        request.setUsesLanguageCorrection_(True)
        success, err = handler.performRequests_error_([request], None)
        if not success:
            print(f"[ocr] Vision 请求失败: {err}")
            return []
        min_conf = float(cfg.get("min_confidence", 0.3))
        lines: list[tuple[str, float]] = []
        for obs in request.results() or []:
            cand = obs.topCandidates_(1)[0]
            text, conf = cand.string(), float(cand.confidence())
            if text.strip() and conf >= min_conf:
                lines.append((text.strip(), conf))
        lines.sort(key=lambda x: -x[1])
        return lines
    except Exception as e:
        print(f"[ocr] 识别异常（{type(e).__name__}: {e}）")
        return []
