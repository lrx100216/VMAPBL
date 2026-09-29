"""配置唯一入口：.env（密钥/模型名/视频源）+ config/settings.yaml（阈值/ROI）。

纪律：模型名只允许出现在 .env 与本文件中，其他模块一律从这里取。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录（VMAPBL/）
load_dotenv(ROOT / ".env")


@lru_cache
def settings() -> dict:
    """读取 config/settings.yaml（不存在则返回空 dict，调用方用 .get 带默认值）。"""
    path = ROOT / "config" / "settings.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def api_key() -> str:
    """DeepSeek API Key；未配置时给出明确指引。"""
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key or "在这里" in key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置：cp -n .env.example .env 后填入真实 Key")
    return key


def base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()


def model_name() -> str:
    """云端视觉模型名（唯一合法出处）。"""
    name = os.getenv("DEEPSEEK_MODEL", "").strip()
    if not name:
        raise RuntimeError("DEEPSEEK_MODEL 未配置，请检查 .env（可从 .env.example 复制）")
    return name


def video_source() -> "str | int":
    """视频源："0" → 摄像头索引 0；否则视为回放文件路径（离线回放模式，规范第八节）。"""
    raw = os.getenv("VIDEO_SOURCE", "0").strip()
    return int(raw) if raw.isdigit() else raw
