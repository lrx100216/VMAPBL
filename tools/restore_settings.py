#!/usr/bin/env python3
"""Restore the previous validated settings file, retaining the backup."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import restore_settings_backup

if __name__ == "__main__":
    try:
        restore_settings_backup()
    except Exception as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
    print("已恢复上一次配置；备份仍保留。请重新检查 ROI 和通知配置。")
