"""macOS 双击启动应用包的静态验收。"""
from pathlib import Path
import os
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "VMAPBL Butler.app"
LAUNCHER = APP / "Contents" / "MacOS" / "launcher"


def test_app_bundle_has_native_metadata():
    info = APP / "Contents" / "Info.plist"
    assert info.exists()
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundlePackageType"] == "APPL"
    assert data["CFBundleExecutable"] == "launcher"
    assert data["CFBundleName"] == "VMAPBL Butler"
    assert data["CFBundleIdentifier"] == "local.vmapbl.butler"


def test_launcher_is_executable_and_valid_zsh():
    assert LAUNCHER.exists()
    assert os.access(LAUNCHER, os.X_OK)
    result = subprocess.run(["zsh", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_launcher_uses_project_relative_paths_and_opens_console():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "/Users/liuruixuan" not in text
    assert 'APP_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"' in text
    assert '.venv/bin/python' in text
    assert 'main.py" --serve' in text
    assert 'http://127.0.0.1:8321/healthz' in text
    assert 'CONSOLE_URL="http://127.0.0.1:8321/"' in text
    assert 'open "$CONSOLE_URL"' in text
    assert "kill -0" in text  # 服务异常退出时显示失败而不是假成功
