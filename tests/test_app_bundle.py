"""macOS 双击启动应用包的静态验收。"""
from pathlib import Path
import os
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "VMAPBL Butler.app"
LAUNCHER = APP / "Contents" / "MacOS" / "launcher"
MAIN_EXE = APP / "Contents" / "MacOS" / "VMAPBLButler"


def test_app_bundle_has_native_metadata():
    info = APP / "Contents" / "Info.plist"
    assert info.exists()
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundlePackageType"] == "APPL"
    assert data["CFBundleExecutable"] == "VMAPBLButler"
    assert data["CFBundleName"] == "VMAPBL Butler"
    assert data["CFBundleIdentifier"] == "local.vmapbl.butler"


def test_app_bundle_main_executable_is_native():
    """主可执行文件必须是 Mach-O，macOS 才会把本地网络权限归到应用包本身。"""
    assert MAIN_EXE.exists()
    assert os.access(MAIN_EXE, os.X_OK)
    result = subprocess.run(["file", "-b", str(MAIN_EXE)],
                            capture_output=True, text=True)
    assert "Mach-O" in result.stdout, result.stdout


def test_native_launcher_check_mode():
    result = subprocess.run([str(MAIN_EXE), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_app_bundle_has_no_external_symlinks():
    root = APP.resolve()
    for path in APP.rglob("*"):
        if path.is_symlink():
            target = path.resolve()
            assert target == root or root in target.parents, \
                f"包内符号链接指向包外会让 codesign 校验失败：{path} -> {target}"


def test_app_bundle_declares_local_network_usage():
    """macOS 15+ 访问局域网 RTSP 摄像头需要本地网络用途说明，否则系统会拒绝连接。"""
    info = APP / "Contents" / "Info.plist"
    data = plistlib.loads(info.read_bytes())
    assert data.get("NSLocalNetworkUsageDescription")


def test_launcher_is_executable_and_valid_zsh():
    assert LAUNCHER.exists()
    assert os.access(LAUNCHER, os.X_OK)
    result = subprocess.run(["zsh", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_launcher_resolves_project_link_when_app_is_moved():
    # 仓库内的 .app 不提交绝对路径链接；安装到 /Applications 时创建该链接
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'PROJECT_LINK="$(dirname "$0")/../Resources/project"' in text
    assert 'APP_ROOT="$(cd "$PROJECT_LINK" && pwd)"' in text


def test_launcher_supports_project_path_file_without_symlink():
    """安装包内不能放指向包外的符号链接（会让 codesign 校验失败），改用路径文件。"""
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'Resources/project.path' in text
    assert 'APP_ROOT="$(<"$PROJECT_PATH_FILE")"' in text


def test_launcher_warns_when_service_is_degraded():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert '/readyz' in text
    assert 'display notification' in text
    assert '感知降级' in text


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
