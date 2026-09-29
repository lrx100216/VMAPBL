"""药盒检测器训练：yolo26n + 自标注数据，M2 上 device=mps（规范 M6：十几分钟能训完）。

用法（先采集标注，再训练）：
    python tools/collect_pillbox_frames.py     # 采集 50–100 张原图
    （人工标注成 YOLO 格式 txt，class id 0 = pillbox）
    python tools/train_pillbox.py              # 训练并部署权重

数据集布局（YOLO 惯例，本脚本会校验；images/val 为空时自动从 train 划出 1/5）：
    data/pillbox_ds/
    ├── images/train/*.jpg      # 训练图
    ├── images/val/*.jpg        # 验证图
    ├── labels/train/*.txt      # 同名标注（无 txt 的图按「无药盒」负样本处理）
    └── labels/val/*.txt

产出：runs/pillbox/train/weights/best.pt → 复制部署为 models/pillbox.pt
（场景4 的 PillboxDetector 默认从这里加载；models/ 不入库）。

训练指标（mAP 等）由 ultralytics 在训练结束时自己打印——本脚本不转述、不编造。
场景4 全链路先用 python main.py --trigger medication 实跑；逐场景回放统计
（tools/replay.py）目前只支持 door_key/get_up/wake_up，medication 的 20 次
回放回归计划在 M8 扩展——届时拿真实标注素材跑出准确率数字才算数（规范纪律：
数字必须实测，本脚本不代跑）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 允许从任意 cwd 运行

import yaml  # noqa: E402

DS = ROOT / "data" / "pillbox_ds"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def _images(sub: str) -> list[Path]:
    d = DS / "images" / sub
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMG_EXTS)


def _prepare_dataset() -> Path:
    """校验数据集布局，必要时自动划验证集；生成 ultralytics 数据集 yaml。"""
    for sub in ("images/train", "labels/train"):
        if not (DS / sub).is_dir():
            raise SystemExit(
                f"缺目录 {DS / sub}。先运行 tools/collect_pillbox_frames.py "
                "采集，再按本文件头部注释的布局标注。")
    train_imgs = _images("train")
    if len(train_imgs) < 50:
        print(f"⚠️ 训练图只有 {len(train_imgs)} 张（规范建议 50–100 张）："
              "可以继续，但检出率大概率不够，建议补采。")
    if not train_imgs:
        raise SystemExit(f"{DS / 'images/train'} 里没有图片")

    val_imgs = _images("val")
    if not val_imgs:
        # 自动划 1/5 做验证（按文件名顺序每隔 5 张取 1，确定性、不引入随机）
        (DS / "images/val").mkdir(parents=True, exist_ok=True)
        (DS / "labels/val").mkdir(parents=True, exist_ok=True)
        moved = 0
        for i, img in enumerate(train_imgs):
            if i % 5 != 0:  # 文件名顺序每隔 5 张取 1：确定性，不引入随机
                continue
            label = DS / "labels/train" / f"{img.stem}.txt"
            shutil.move(str(img), DS / "images/val" / img.name)
            if label.exists():
                shutil.move(str(label), DS / "labels/val" / label.name)
            moved += 1
        val_imgs = _images("val")
        print(f"未提供验证集：已从训练集划出 {moved} 张到 images/val")
    train_imgs = _images("train")  # 自动划分后重取：划走的图已不在 train 目录
    missing_labels = sum(
        1 for img in train_imgs + val_imgs
        if not (DS / "labels" / img.parent.name / f"{img.stem}.txt").exists())
    if missing_labels:
        print(f"提示：{missing_labels} 张图无同名标注 txt，将按「无药盒」"
              "负样本参与训练（YOLO 惯例；若只是忘标请补齐）")

    ds_yaml = DS / "pillbox.yaml"
    ds_yaml.write_text(yaml.safe_dump(
        {"path": str(DS), "train": "images/train", "val": "images/val",
         "names": {0: "pillbox"}},
        allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"数据集：train {len(train_imgs)} 张 / val {len(val_imgs)} 张，"
          f"描述文件 {ds_yaml}")
    return ds_yaml


def main() -> int:
    ap = argparse.ArgumentParser(description="药盒检测器训练（yolo26n, device=mps）")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--base", type=str, default="yolo26n.pt",
                    help="预训练底座（默认 yolo26n.pt，首次自动下载）")
    args = ap.parse_args()

    ds_yaml = _prepare_dataset()
    from ultralytics import YOLO  # 重依赖，校验通过后才导入

    print(f"开始训练：base={args.base} epochs={args.epochs} "
          f"imgsz={args.imgsz} device=mps（M2 上规范预估十几分钟）")
    model = YOLO(args.base)
    model.train(data=str(ds_yaml), epochs=args.epochs, imgsz=args.imgsz,
                device="mps", project="runs/pillbox", name="train",
                exist_ok=True)
    best = ROOT / "runs" / "pillbox" / "train" / "weights" / "best.pt"
    if not best.exists():
        raise SystemExit(f"训练结束但没找到 {best}，请检查上方 ultralytics 输出")
    dest = ROOT / "models" / "pillbox.pt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, dest)
    print(f"已部署：{best} → {dest}")
    print("下一步：python main.py --trigger medication 实测场景4 全链路；"
          "逐场景 20 次回放统计（含 medication）计划在 M8 扩展，"
          "届时用真实标注素材跑出准确率数字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
