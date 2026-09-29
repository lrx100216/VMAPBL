"""技能层：唯一出口集合（接口契约第四节）。

- tts.speak   唯一发声出口（半双工互斥）
- stt.listen  唯一收声出口
- vision.json_call  唯一视觉出口（云端 DeepSeek / 本地 mlx-vlm）
- ocr.recognize_text  L2.5 档：Apple Vision OCR
- llm.chat_json  纯文本 JSON 调用（意图分类/信息抽取共用）
- intent.classify  语音指令意图分类
- notify.send   唯一对外通知出口（M3 落 SQLite，M7 接 WebSocket）
"""
