"""场景8 动作：拍照文字识别（L2.5 OCR 优先，语义问题才升 L3 VLM，规范第六节）。

阶梯纪律（3.1 节）：能低档不升档——
1. 老人问「念/读/什么字」类 → Apple Vision OCR 直接读，¥0、<50ms，不调云；
2. 老人问语义（"这药一天吃几次"）或 OCR 没读到 → 升级 vision.json_call，
   提示词硬约束：必须准确、不得编造画面中不存在的内容、看不清明确说看不清。
"""
from __future__ import annotations

from actions import skill
from core import config
from skills import ocr, vision

_VLM_SCHEMA = {"answer": "用一句中文回答老人的问题",
               "uncertain": "true/false：画面看不清或没有相关文字时为 true"}
_VLM_PROMPT = """你是独居老人的随身读字助手。观察这张照片，回答老人的问题。

硬性要求：
- 必须准确，只依据画面里真实存在的内容，绝不编造画面中不存在的内容；
- 看不清、没有文字或与问题无关时，uncertain 给 true，answer 说明看不清；
- 照片里可能有本地 OCR 先读出的文字（可能不全，仅供参考，以你看到的为准）。

本地 OCR 读到的内容：{ocr_text}

老人的问题：{question}"""


@skill("photo_read")
async def photo_read(ctx: dict) -> dict:
    """场景8 动作：抓帧 → OCR 优先 → 必要时升 VLM。返回 {status, speech}。"""
    question = (ctx.get("text") or "这上面写的什么").strip()
    frame = vision.grab_frame()
    if frame is None:
        return {"status": "uncertain",
                "speech": "奶奶，摄像头打不开，我看不到纸上的字。"}
    lines = ocr.recognize_text(frame)
    keywords = config.settings().get("photo_ocr", {}).get("read_keywords", [])
    read_intent = any(k in question for k in keywords)
    if read_intent and lines:
        text = "，".join(t for t, _c in lines)
        return {"status": "ok", "source": "ocr", "speech": f"上面写着：{text}"}
    # 语义问题，或想读但 OCR 没读到（手写字/光线差）→ 升 L3 云端
    ocr_hint = "；".join(t for t, _c in lines) if lines else "（无）"
    data = await vision.json_call(
        frame, _VLM_PROMPT.format(ocr_text=ocr_hint, question=question),
        _VLM_SCHEMA, scene_id="photo_ocr")
    if data.get("uncertain"):
        return {"status": "uncertain",
                "speech": "奶奶，这个我看不太清，能把纸举近一点、光线亮一点再问我吗？"}
    answer = str(data.get("answer") or "").strip()
    if not answer:
        return {"status": "uncertain", "speech": "奶奶，我没看到上面有字。"}
    return {"status": "ok", "source": "vlm", "speech": answer}
