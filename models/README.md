# VMAPBL 模型权重目录

本目录用于存放系统运行所需的自训及微调模型权重文件。

## 1. 药盒检测模型 (`models/pillbox.pt`)
- **作用**：用于场景4（服药提醒）第一阶段视觉判定：识别桌面/托盘上是否存在药盒目标。
- **获取方式**：
  1. 使用采图标注工具采集 50~100 张家庭药盒真实摆放图像：
     ```bash
     python tools/collect_pillbox_frames.py
     ```
  2. 使用 Ultralytics YOLO 训练脚本产出权重：
     ```bash
     python tools/train_pillbox.py
     ```
  3. 将训练产出的 `best.pt` 复制为 `models/pillbox.pt`：
     ```bash
     cp runs/detect/train/weights/best.pt models/pillbox.pt
     ```
- **优雅降级**：
  当本目录不存在 `pillbox.pt` 时，系统自动在 `actions/medication.py` 记录降级审计事件，并无缝切入纯语音两轮问询确认（“奶奶，药吃了吗？”），确保老人看护不断档。
