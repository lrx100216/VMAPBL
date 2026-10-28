"""配置唯一入口：.env（密钥/模型名/视频源）+ config/settings.yaml（阈值/ROI）。

纪律：模型名只允许出现在 .env 与本文件中，其他模块一律从这里取。
"""
from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录（VMAPBL/）
_SETTINGS_PATH = ROOT / "config" / "settings.yaml"
_yaml_lock = threading.Lock()
load_dotenv(ROOT / ".env")


@lru_cache
def settings() -> dict:
    """读取 config/settings.yaml（不存在则返回空 dict，调用方用 .get 带默认值）。"""
    path = _SETTINGS_PATH
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_settings() -> dict:
    """清空配置缓存并重新加载 config/settings.yaml（供在线 ROI 标定热更新）。"""
    settings.cache_clear()
    return settings()


def save_roi(name: str, points: list[list[int] | tuple[int, int]],
             settings_path: Path | None = None) -> list[list[int]]:
    """将多边形顶点写入 settings.yaml 的 roi.<name>（保留注释）并立即热刷新缓存。"""
    from ruamel.yaml import YAML

    target = settings_path or _SETTINGS_PATH
    ryaml = YAML()
    ryaml.preserve_quotes = True
    clean_pts = [[int(round(x)), int(round(y))] for x, y in points]
    with _yaml_lock:
        if target.exists():
            with open(target, encoding="utf-8") as f:
                data = ryaml.load(f) or {}
        else:
            data = {}
        if data.get("roi") is None:
            data["roi"] = {}
        data["roi"][name] = clean_pts
        with open(target, "w", encoding="utf-8") as f:
            ryaml.dump(data, f)
        reload_settings()
    return clean_pts


def delete_roi(name: str, settings_path: Path | None = None) -> bool:
    """从 settings.yaml 删除指定 ROI 多边形并热刷新缓存。"""
    from ruamel.yaml import YAML

    target = settings_path or _SETTINGS_PATH
    ryaml = YAML()
    ryaml.preserve_quotes = True
    with _yaml_lock:
        if not target.exists():
            return False
        with open(target, encoding="utf-8") as f:
            data = ryaml.load(f) or {}
        rois = data.get("roi")
        if not isinstance(rois, dict) or name not in rois:
            return False
        del rois[name]
        with open(target, "w", encoding="utf-8") as f:
            ryaml.dump(data, f)
        reload_settings()
    return True


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


def butler_token() -> str:
    """家人端访问口令；为空时保持本机调试模式（生产部署必须设置）。"""
    return os.getenv("BUTLER_TOKEN", "").strip()


def dingtalk_sign_secret() -> str:
    return os.getenv("DINGTALK_SIGN_SECRET", "").strip()


def feishu_sign_secret() -> str:
    return os.getenv("FEISHU_SIGN_SECRET", "").strip()
