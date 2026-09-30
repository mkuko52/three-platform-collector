"""Build a Windows x64 folder that needs no installed Python/Node; Edge login is still manual."""
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist/rpc/reverse_collector"
PACKAGE = ROOT / "双机分片"


def main():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("便携版仅支持 Windows x64；不能把本机包当作跨系统程序")
    if OUT.exists():
        raise SystemExit(f"已有便携包：{OUT}；请先移走旧包，不覆盖可能已有的采集数据")
    builder = shutil.which("pyinstaller")
    if not builder:
        raise SystemExit("打包电脑需要已安装 PyInstaller；不自动下载/安装依赖")
    work = ROOT / "js_reverse_cache/portable_build"
    hooks = work / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    # The installed patchright PyInstaller hook for playwright otherwise bundles an unrelated second driver.
    (hooks / "hook-playwright.sync_api.py").write_text("datas = []; binaries = []; hiddenimports = []\n", encoding="utf-8")
    command = [builder, "--noconfirm", "--clean", "--onedir", "--name", "reverse_collector",
               "--distpath", str(OUT.parent), "--workpath", str(work / "work"),
               "--specpath", str(work), "--additional-hooks-dir", str(hooks),
               "--collect-data", "playwright", "--collect-binaries", "playwright",
               *[part for module in ("torch", "tensorflow", "torchvision", "torchaudio", "numpy", "pandas",
                                     "scipy", "matplotlib", "IPython", "pytest", "jedi", "PIL", "cv2", "sklearn")
                 for part in ("--exclude-module", module)],
               "--hidden-import", "requests", "--hidden-import", "httpx",
               "--hidden-import", "websocket", str(ROOT / "start_all.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    for source, dest in ((ROOT / "collectors", OUT / "collectors"),
                         (ROOT / "archive/previous_workspace/双减舆情需求.md",
                          OUT / "archive/previous_workspace/双减舆情需求.md")):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, dest)
    shutil.copy2(PACKAGE / "异机分片使用说明.md", OUT / "异机分片使用说明.md")
    (OUT / "runtime").mkdir()
    from start_all import rate_overrides
    current = rate_overrides()
    minimum = {"douyin": 45, "kuaishou": 90, "xiaohongshu": 90}
    rates = {name: max(seconds, current.get(name, 0)) for name, seconds in minimum.items()}
    (OUT / "runtime/collector_rate_overrides.json").write_text(
        json.dumps(rates, ensure_ascii=False), encoding="utf-8")
    (OUT / "prepare_logins.bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\n'
        'set "EDGE=%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe"\r\n'
        'if not exist "%EDGE%" set "EDGE=%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe"\r\n'
        'if not exist "%EDGE%" (echo Edge not found; manually start Chrome/Edge with separate CDP profiles. & pause & exit /b 1)\r\n'
        'start "" "%EDGE%" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="%~dp0runtime\\douyin\\browser" https://www.douyin.com/\r\n'
        'start "" "%EDGE%" --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 --user-data-dir="%~dp0runtime\\xiaohongshu\\browser" https://www.xiaohongshu.com/\r\n'
        'start "" "%EDGE%" --remote-debugging-port=9224 --remote-debugging-address=127.0.0.1 --user-data-dir="%~dp0runtime\\kuaishou\\browser" https://www.kuaishou.com/\r\n'
        'echo Log in manually, confirm your approved proxy route, leave all three windows open, then run start_reverse.bat.\r\n'
        'pause\r\n', encoding="ascii")
    (OUT / "start_reverse.bat").write_text(
        '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
        'reverse_collector.exe --fail-fast --retries 0 --shard reverse %*\r\n'
        'set "RC=%ERRORLEVEL%"\r\necho Collector exit code: %RC%\r\n'
        'if not "%COLLECTOR_TEST_NO_PAUSE%"=="1" pause\r\nexit /b %RC%\r\n', encoding="ascii")
    speed_dir = OUT / "采集速度版本"
    speed_dir.mkdir()
    for flag in ("fast", "high-speed", "ultra-speed", "ultimate-speed"):
        (speed_dir / f"start_reverse_{flag.replace('-', '_')}.bat").write_text(
            f'@echo off\ncall "%~dp0..\\start_reverse.bat" --{flag} %*\nexit /b %ERRORLEVEL%\n',
            encoding="ascii")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([str(OUT / "reverse_collector.exe"), "--fail-fast", "--retries", "0",
                             "--shard", "reverse", "--check"], cwd=OUT, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode or "采集范围核验" not in result.stdout or result.stderr:
        raise SystemExit("便携包离线自检失败（未发网站请求）：" + result.stderr[-1000:])
    temporary_archive = Path(shutil.make_archive(str(PACKAGE / "reverse_collector_windows_x64_rpc.next"), "zip",
                                               root_dir=OUT.parent, base_dir=OUT.name))
    archive = PACKAGE / "reverse_collector_windows_x64_rpc.zip"
    temporary_archive.replace(archive)  # 完整写好再替换，打包中断不截断原发布包。
    print(f"便携包离线自检通过：{OUT}；压缩包：{archive}；未验证另一台电脑的浏览器/登录/代理和线上接口")


if __name__ == "__main__":
    main()
