"""三个项目的前台总开关：python start_all.py；Ctrl+C 统一停止。"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from email.utils import parsedate_to_datetime

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
if getattr(sys, "frozen", False):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
PROJECTS = {"douyin": "抖音", "kuaishou": "快手", "xiaohongshu": "小红书"}
RESUME_KEYS = {"1": "douyin", "2": "kuaishou", "3": "xiaohongshu"}
ENV = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def project_python():
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable))
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv if venv.is_file() else Path(sys.executable))


RATE_DEFAULTS = {"douyin": 30, "kuaishou": 60, "xiaohongshu": 60}
# 随项目附带的保守档位；提速只跳过这一初始档位，不跳过故障后的自动降频。
NORMAL_SEED_RATES = {"douyin": 45, "kuaishou": 90, "xiaohongshu": 90}
HIGH_RATES = {"douyin": 20, "kuaishou": 40, "xiaohongshu": 60}
ULTRA_RATES = {"douyin": 10, "kuaishou": 20, "xiaohongshu": 10}
ULTIMATE_RATES = {"douyin": 10, "kuaishou": 10, "xiaohongshu": 10}


def rate_overrides(path=None):
    path = path or ROOT / "runtime/collector_rate_overrides.json"
    if not path.exists():
        return {}
    values = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(values, dict) or any(name not in RATE_DEFAULTS or type(value) not in (int, float)
            or not math.isfinite(value) or not RATE_DEFAULTS[name] <= value <= 3600
            for name, value in values.items())):
        raise RuntimeError("采集间隔记录损坏；已拒绝启动，不猜测安全间隔")
    return values


def selected_rates(fast=False, path=None, high=False, ultra=False, ultimate=False):
    recorded = rate_overrides(path)
    if fast or high or ultra or ultimate:
        recorded = {name: value for name, value in recorded.items() if value > NORMAL_SEED_RATES[name]}
    defaults = ULTIMATE_RATES if ultimate else ULTRA_RATES if ultra else HIGH_RATES if high else RATE_DEFAULTS
    return {name: max(default, recorded.get(name, 0)) for name, default in defaults.items()}


def lower_rate_if_needed(name, reason, path=None, fast=False, high=False, ultra=False, ultimate=False):
    # 登录、文件占用、坏数据和已知单篇权限问题不是频控；降频不能修复这些问题。
    if re.search(r"登录|验证码|CSV 被占用|断点损坏|单篇|权限或已被删除", reason):
        return None
    if not re.search(r"操作太快|频率受限|访问频繁|访问被限制|HTTP (?:429|461|403|503)|"
                     r"接口拒绝|请求失败|原生请求失败|响应异常|status_code=", reason):
        return None
    path = path or ROOT / "runtime/collector_rate_overrides.json"
    settings = rate_overrides(path)
    previous = selected_rates(fast, path, high, ultra, ultimate)[name]
    # 高于初始保守档位：即使记录文件缺失，下次提速也不会跳过本次故障降频。
    current = min(3600, math.ceil(max(previous, NORMAL_SEED_RATES[name]) * 1.5))
    if current == previous:
        return None
    settings[name] = current
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return previous, current


def validate_scope():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from collectors import douyin, kuaishou, xiaohongshu
    required = ROOT / "archive/previous_workspace/双减舆情需求.md"
    expected = xiaohongshu.load_requirement_keywords(required)
    if len(expected) != 134:
        raise RuntimeError("需求文档关键词数已变化；拒绝按错误词表全量采集")
    queries = douyin.search_queries(expected)
    for name, module in (("抖音", douyin), ("快手", kuaishou), ("小红书", xiaohongshu)):
        if (list(module.DEFAULT_KEYWORDS) != expected or module.search_queries(expected) != queries or len(queries) != 938
                or str(module.START) != "2021-07-24" or str(module.END) != "2026-07-24"):
            raise RuntimeError(f"{name}关键词/查询顺序或含首尾日期与需求文档不一致；已拒绝启动")
    print("采集范围核验：三站均为需求文档134基础词+年份组合=938查询；"
          "贴文/评论均按发布时间2021-07-24～2026-07-24含首尾筛选。", flush=True)


def arguments(name, dy_port, xhs_port, test_requests=None, rates=None, ks_port=9224):
    args = {"douyin": ["--browser-rpc", str(dy_port)], "kuaishou": ["--rpc-port", str(ks_port)],
            "xiaohongshu": ["--transport", "rpc", "--rpc-port", str(xhs_port)]}[name]
    if test_requests is not None:
        args += ["--max-requests" if name == "kuaishou" else "--test-requests", str(test_requests)]
    if rates and name in rates:
        args += [{"douyin": "--interval", "kuaishou": "--delay", "xiaohongshu": "--min-delay"}[name],
                 str(rates[name])]
    return args


def run_worker(name, dy_port, xhs_port, test_requests=None, shard="full", check=False, fast=False, high=False,
               ultra=False, ultimate=False, ks_port=9224):
    # Windows 的 CTRL_BREAK 默认会直接终止；转为 KeyboardInterrupt 才会执行采集器的 finally。
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "collectors"))
    if check and getattr(sys, "frozen", False) and name == "douyin":
        from playwright.sync_api import sync_playwright
        with sync_playwright():  # 只起本地驱动，不启动浏览器或访问站点。
            pass
    rates = (selected_rates(fast, high=high, ultra=ultra, ultimate=ultimate) if fast or high or ultra or ultimate
             else rate_overrides())  # 常规档无记录时沿用采集器原有默认参数。
    sys.argv = [str(ROOT / "collectors" / (name + ".py")), *arguments(name, dy_port, xhs_port, test_requests,
                                                                     rates, ks_port),
                *(["--shard", shard] if shard != "full" else []), *(["--check"] if check else [])]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except KeyboardInterrupt:
        return 130
    return 0


def workspace_lock():
    """OS 文件锁随进程退出释放；不根据旧 PID 猜测、也不删除正在使用的锁。"""
    # 总开关锁与三个脚本自己的平台锁互补，防止重复写入同一份数据。
    path = ROOT / "logs/collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise RuntimeError("已有总开关正在运行；请在原窗口 Ctrl+C 停止，勿重复采集") from None


def check_project(name):
    """离线检查入口/导入和必要文件，不启动浏览器，不验证登录有效性。"""
    command = ([project_python(), "--worker", name, "--check"] if getattr(sys, "frozen", False) else
               [project_python(), str(ROOT / "collectors" / (name + ".py")), "--check"])
    check = subprocess.run(command, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=30)
    if check.returncode:
        raise RuntimeError(f"{PROJECTS[name]} 入口/依赖检查失败（{command[0]}）：\n{check.stderr.strip()}")
    print(f"[{PROJECTS[name]}] 离线检查通过，Python: {command[0]}", flush=True)


def stop_processes(processes, grace=60):
    running = [p for p in processes if p.poll() is None]
    for process in running:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        except OSError:
            pass  # 发送信号前可能刚好退出。
    deadline = time.monotonic() + grace
    for process in running:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            print(f"PID {process.pid} 未及时退出，强制停止该采集进程；下次从已提交断点续采。", flush=True)
            if os.name == "nt":
                # venv 的 python.exe 可能只是转发器；仅 kill 它会留下真正的采集进程。
                result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                        capture_output=True, timeout=10)
                if result.returncode and process.poll() is None:
                    raise RuntimeError(f"无法停止本次任务 PID={process.pid}，请人工检查")
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)


def console_width(text):
    # ponytail: 按Unicode东亚宽度换行；复杂组合Emoji宽度因终端而异，卡片不画右边框以免错位。
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def console_wrap(value, width):
    """仅清理终端控制字符；原始日志和CSV不受影响，正文不截断。"""
    text = "—" if value is None or (isinstance(value, str) and not value.strip()) else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = "".join(c for c in text if c == "\n" or not unicodedata.category(c).startswith("C"))
    result = []
    for paragraph in text.split("\n"):
        line, used = "", 0
        for char in paragraph:
            size = console_width(char)
            if line and used + size > width:
                result.append(line)
                line, used = "", 0
            line += char
            used += size
        result.append(line)
    return result


def render_console_line(name, text, width=None):
    """单行结构化日志转终端卡片；由父进程整块打印，三平台不会交叉穿插卡片行。"""
    width = max(24, width if width is not None else min(110, shutil.get_terminal_size((96, 24)).columns - 1))
    platform = PROJECTS[name]
    parts = text.split(" | ", 3)
    if len(parts) != 4 or parts[2] not in ("INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"):
        prefix = f"[{platform}] "
        return "\n".join((prefix if index == 0 else " " * console_width(prefix)) + line
                         for index, line in enumerate(console_wrap(text, width - console_width(prefix))))
    clock, level, message = parts[0][11:19], parts[2], parts[3]
    if len(clock) != 8 or clock[2::3] != "::" or not clock.replace(":", "").isdigit():
        clock = "--:--:--"
    kind, separator, payload = message.partition(" | ")
    data = None
    if separator and kind in ("贴文已保存", "评论候选已保存", "评论页已提交"):
        try:
            data = json.loads(payload)
        except ValueError:
            pass
        if not isinstance(data, dict):
            level, message = "WARNING", "记录格式异常，原始内容已保留在日志中"
            data = None
    if data is not None and kind != "评论页已提交":
        heading = f"{platform} · {kind} · {clock}"
        header = console_wrap(heading, width - 3)
        first = "┌─ " + header[0]
        if console_width(first) < width:
            first += " "
        lines = ["", first + "─" * max(0, width - console_width(first)), *["│  " + line for line in header[1:]]]

        def field(label, value):
            prefix = "│  " + label + " " * (8 - console_width(label)) + " : "
            continuation = "│" + " " * (console_width(prefix) - 1)
            for index, line in enumerate(console_wrap(value, width - console_width(prefix))):
                lines.append((prefix if index == 0 else continuation) + line)

        if kind == "评论候选已保存":
            for label, key in (("所属贴文", "内容ID"), ("评论ID", "评论ID"), ("评论者", "评论者昵称"),
                               ("评论时间", "评论时间"), ("点赞数", "评论点赞数")):
                field(label, data.get(key))
            eligible = data.get("可参与高赞排序")
            field("参与排序", "是（候选，非最终TOP20）" if eligible is True else
                  "否（日期或点赞信息不符合筛选条件）" if eligible is False else None)
            field("评论内容", data.get("评论内容"))
        else:
            for label, key in (("内容ID", "内容ID"), ("标题", "标题"), ("作者", "作者"),
                               ("发布时间", "发布时间"), ("搜索词", "搜索词")):
                field(label, data.get(key))
            field("互动", "   |   ".join(label + " " + (str(data[key]) if data.get(key) not in (None, "") else "—")
                                         for label, key in (("赞", "点赞数"), ("评", "评论数"), ("藏", "收藏数"), ("转", "分享数"))))
            if data.get("内容") != data.get("标题"):
                field("正文", data.get("内容"))
        field("采集时间", data.get("采集时间"))
        lines.append("└" + "─" * (width - 1))
        return "\n".join(lines)
    tag = {"WARNING": "警告", "ERROR": "错误", "CRITICAL": "错误", "DEBUG": "调试"}.get(level, "信息")
    if data is not None:
        tag = "进度"
        status = "已完成" if data.get("已完成") is True else "待续采"
        excluded = f"日期排除 {data['日期排除']} 条  |  " if "日期排除" in data else ""
        message = (f"内容ID {data.get('内容ID', '—')}\n"
                   f"累计 {data.get('累计页数', '—')} 页  |  返回 {data.get('返回条数', '—')} 条  |  "
                   f"新增 {data.get('新增候选', '—')} 条  |  {excluded}{status}")
    elif message.startswith("高赞评论CSV已同步："):
        tag = "CSV"
        path, separator, detail = message.partition("，共")
        if separator:
            filename = path.split("：", 1)[1].replace("\\", "/").rsplit("/", 1)[-1]
            message = f"{filename}  |  共{detail}"  # 完整路径仍保留在原始日志。
    prefix = f"{clock}  {platform}{' ' * (6 - console_width(platform))}  {tag}{' ' * (4 - console_width(tag))}  "
    lines = console_wrap(message, max(4, width - console_width(prefix)))
    if console_width(prefix) >= width - 4:
        return "\n".join(console_wrap(f"{clock} [{platform}] {tag}", width) + console_wrap(message, width))
    return "\n".join((prefix if index == 0 else " " * console_width(prefix)) + line for index, line in enumerate(lines))


def desktop_notice(title, message=None):
    """原生Windows提示；弹窗独立于采集进程，等用户确认，不阻塞其它平台。"""
    if os.name != "nt":
        if message and sys.stdout.isatty():
            print("\a", end="", flush=True)
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
        if message:
            # 冻结EXE不是Python解释器，不能用 -c；独立提示入口不启动采集。
            command = [sys.executable, *([] if getattr(sys, "frozen", False) else [__file__]),
                       "--show-notice", title, message]
            subprocess.Popen(command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, AttributeError):
        print("[警报] 系统提示不可用，请查看终端警报和状态文件。", flush=True)


def alert_summary(text):
    text = " ".join(console_wrap(text, 300))[:300]
    if re.search(r"https?://|(?:cookie|authorization|\w*token|sign(?:ature)?|__NS_\w+)[\"']?\s*[:=]", text, re.I):
        return "错误含网址或认证字段，摘要不复制；请在本机查看对应平台日志。"
    return text


def alert_log_line(text):
    parts = text.split(" | ", 3)
    if len(parts) == 4 and parts[2] in ("ERROR", "CRITICAL", "WARNING"):
        return ("WARNING" if parts[2] == "WARNING" else "ERROR"), parts[3]
    if text.startswith("采集未完成：") or re.match(r"^[\w.]+(?:Error|Exception):", text):
        return "ERROR", text
    return "", ""


LOGIN_ERROR = (r"NEED_LOGIN|web_need_login|verify_check|manual_verification|验证码|人机验证|人工验证|安全验证|访问验证|"
               r"challenge/login|需要[^；\n]*登录|请[^；\n]*登录|登录或权限异常|HTTP[ =]+401\b|code=-10[014]\b")
HARD_STOP = (r"HTTP[ =]+403\b|损坏|不一致|不匹配|CSV.*占用|PermissionError|已有.*任务|缺少.*令牌|签名.*失败")


def needs_manual_login(reason):
    return (len(reason) <= 8000 and bool(re.search(LOGIN_ERROR, reason, re.I))
            and not re.search(HARD_STOP, reason, re.I))


def manual_pause_kind(name, reason):
    if needs_manual_login(reason):
        return "登录/验证"
    text = reason.removeprefix("采集未完成：")
    if len(text) <= 8000 and text.startswith(("CSV 被占用，未能更新：",
                                            "CSV 被占用或写入权限不足（系统错误码")):
        return "解除CSV占用"
    if name == "xiaohongshu" and len(text) <= 8000 and text.startswith("浏览器访问被限制（code="):
        return "检查访问限制"
    return None


def read_resume_keys():
    """非阻塞读取1/2/3；Windows无需回车，其他交互终端输入数字后回车。None表示输入不可用。"""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return None
        if os.name == "nt":
            import msvcrt
            keys = []
            # 批量去重；每轮有界读取，避免按住键影响其它平台日志和停止信号。
            for _ in range(64):
                if not msvcrt.kbhit():
                    break
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):  # 丢弃功能键扫描码，不能把F键/方向键当数字确认。
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                else:
                    keys.append(key)
        else:
            import select
            if not select.select([sys.stdin], [], [], 0)[0]:
                return []
            data = os.read(sys.stdin.fileno(), 64)
            if not data:
                return None
            keys = data.decode("ascii", errors="ignore")
        return list(dict.fromkeys(RESUME_KEYS[key] for key in keys if key in RESUME_KEYS))
    except (OSError, ValueError):
        return None


def retry_wait(name, code, reason, delay, comments_paused=False):
    """只对已知可冷却复查的只读失败续采；未知故障/人工验证/数据错误不盲重启。"""
    if code not in (1, 2) or len(reason) > 8000:
        return None
    if (needs_manual_login(reason) or re.search(HARD_STOP, reason, re.I)
            or re.search(r"timeout_no_retry|结果(?:可能)?不确定|结果未知", reason)):
        return None
    if name == "kuaishou" and reason.startswith("采集未完成："):
        reason = reason.removeprefix("采集未完成：")
    if name == "kuaishou" and code == 1 and (
            reason.startswith("快手请求频率受限") or re.search(r"接口拒绝 result=2\b|\bHTTP[ =]+429\b", reason)):
        # 未明result=2不等于已确认频控，但也保守等待，不用每5分钟再撞接口。
        return retry_after_wait(reason, max(900, delay * 3))  # 默认15/30/60分钟；服务端要求更久则遵守。
    # ponytail: 三个已有采集器的受控错误文本允许名单；新增错误类别时同步测试，不对任意exit=1重试。
    if code == 2:
        eligible = name == "kuaishou" and comments_paused
    else:
        eligible = bool(re.search(r"抖音评论响应异常|抖音评论 cursor 未推进|评论空页或游标循环|评论页全部重复|"
            r"连续 3 个关键词未能加载搜索页|\bHTTP[ =]+(?:408|429|461|5\d\d)\b|"
            r"\b(?:ReadTimeout|ConnectTimeout|TimeoutError|ConnectionError|ConnectError|ReadError|RemoteProtocolError)\b|"
            r"浏览器业务请求加载失败", reason)) or (
            name == "kuaishou" and bool(re.search(r"接口拒绝 result=2\b", reason)))
    if not eligible:
        return None
    return retry_after_wait(reason, delay)


def retry_after_wait(reason, delay=0):
    """自动重试和人工确认共用服务端冷却约束；无效头不授权重发。"""
    hint = re.search(r"Retry-After=(?:'([^']*)'|\"([^\"]*)\"|([^\s;]+))", reason, re.I)
    if hint:
        value = next(v for v in hint.groups() if v is not None).strip()
        if value not in ("", "未提供"):
            try:
                if value.isdecimal():
                    seconds = float(value)
                else:
                    deadline = parsedate_to_datetime(value)
                    if deadline.tzinfo is None:
                        return None
                    seconds = deadline.timestamp() - time.time()
                if not math.isfinite(seconds):
                    return None
                delay = max(delay, seconds)  # 不为缩短等待而忽略服务端Retry-After。
            except (ValueError, TypeError, OverflowError):
                return None
    return delay


def emit_alert(message, path):
    block = "\n" + "!" * 64 + "\n【采集警报 / 需要检查】\n" + message + "\n" + "!" * 64
    print(block, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(block + "\n")
    except OSError as exc:
        print(f"[警报] 无法保存警报文件 {path}：{type(exc).__name__}；请保留终端信息。", flush=True)
    desktop_notice("采集警报 — 请检查", message + f"\n\n警报记录：{path}")


def forward_logs(readers, pending, final=False, on_line=None):
    """读取日志新增部分到终端；子进程仍直接写文件，不会被终端管道堵住。"""
    for name, reader in readers.items():
        data = pending.get(name, b"") + reader.read(-1 if final else 65536)
        lines = data.split(b"\n")
        pending[name] = lines.pop()
        if final and pending[name]:
            lines.append(pending.pop(name))
        for line in lines:
            # 完整行才解码，避免文件写入恰好切开一个中文字符。
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if on_line:
                on_line(name, text)
            print(render_console_line(name, text), flush=True)


def supervise(jobs, logdir, stop, max_retries=3, retry_delay=300, resume_poll=None, cooldown_path=None,
              test_requests=None, fail_fast=False, fast=False, high=False, ultra=False, ultimate=False):
    if not 0 <= max_retries <= 10 or not math.isfinite(retry_delay) or retry_delay < 0:
        raise ValueError("无效自动续采配置")
    processes, results, readers, pending = {}, {}, {}, {}
    states = {name: "待启动" for name in jobs}
    alerts, errors, warnings = {}, {}, {}
    attempts = dict.fromkeys(jobs, 0)
    manual_resumes = dict.fromkeys(jobs, 0)
    manual_wait = set()
    manual_not_before, manual_kinds = {}, {}
    retry_at, log_starts, last_codes = {}, {}, {}
    initial_wait = set()
    fail_fast_abort = False
    cooldowns = {}
    if cooldown_path is not None:
        cooldown_path = Path(cooldown_path)
        if cooldown_path.exists():
            cooldowns = json.loads(cooldown_path.read_text(encoding="utf-8"))
            if (not isinstance(cooldowns, dict) or any(n not in PROJECTS or type(v) not in (int, float)
                    or not math.isfinite(v) or v < 0 for n, v in cooldowns.items())):
                raise RuntimeError("冷却记录损坏；为避免提前请求已停止，请检查runtime/collector_cooldowns.json")

    def remember_cooldown(name, delay):
        if cooldown_path is None or delay <= 0:
            return
        cooldowns[name] = max(cooldowns.get(name, 0), time.time() + delay)
        cooldown_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cooldown_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cooldowns, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cooldown_path)

    paused_comments = set()

    def publish():
        for name in manual_wait:
            remaining = max(0, math.ceil(manual_not_before[name] - time.monotonic()))
            cooldown = f"冷却剩余{remaining}秒；" if remaining else ""
            key = next(k for k, n in RESUME_KEYS.items() if n == name)
            states[name] = f"等待人工{manual_kinds[name]}；{cooldown}完成后回本窗口按{key}确认续采"
        for name, deadline in retry_at.items():
            remaining = max(0, math.ceil(deadline - time.monotonic()))
            states[name] = (f"沿用上轮冷却，剩余{remaining}秒；到期按原断点首次启动" if name in initial_wait else
                            f"冷却等待，剩余{remaining}秒；待第{attempts[name] + 1}/{max_retries}次自动续采")
        status = " | ".join(f"{PROJECTS[n]}：{states[n]}" for n in jobs)
        text = (f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n总开关PID：{os.getpid()}\n运行目录：{logdir}\n"
                + "\n".join(f"{PROJECTS[n]}：{states[n]}；日志 {logdir / (n + '.log')}" for n in jobs)
                + "\n\n最近警报（事件发生时的记录；当前动作以上方状态为准）：\n" + ("\n\n".join(alerts.values()) or "暂无")
                + ("\n\n按对应提示完成登录/验证、关闭占用CSV或检查访问限制后，在原总开关窗口按：1=抖音，2=快手，3=小红书。"
                   "Windows无需回车；其他终端需回车。只恢复正在等待人工确认的平台，Ctrl+C取消。\n" if manual_wait else "")
                + "\n\n运行时每30秒更新；时间长期不更新可能是总开关关闭，不能只凭此文件判定仍在运行。\n")
        for path in (logdir / "状态.txt", ROOT / "logs/最新状态.txt"):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(path)
            except OSError as exc:
                print(f"[警报] 状态文件更新失败 {path}：{type(exc).__name__}；以终端和平台日志为准。", flush=True)
        print("[平台状态] " + status, flush=True)
        if alerts:
            print("!!! 持续警报：\n" + "\n".join(alerts.values()) + "\n!!! 详见 logs/最新状态.txt；"
                  + ("严格模式故障即全停，须人工处理后重新启动。" if fail_fast else "其它平台可继续运行。"), flush=True)
        desktop_notice(("采集警报 | " if alerts else "采集状态 | ") + status)

    def warn(name, reason):
        message = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{PROJECTS[name]}] {states[name]}\n"
                   f"原因摘要：{alert_summary(reason)}\n日志：{logdir / (name + '.log')}")
        alerts[name] = message
        publish()
        instruction = ("严格模式停止全部平台，人工处理后重新启动原入口；已提交数据及原断点保留。"
                       if fail_fast else "其它平台不受此提示影响；已提交数据及原断点保留。自动续采/人工确认方式见状态。")
        emit_alert(message + "\n\n" + instruction + "\n普通警报弹窗的确定按钮不会恢复采集。"
                   "\n弹窗记录事件发生时的情况；当前动作请看 logs/最新状态.txt。", logdir / "警报.txt")
        # emit_alert的弹窗标题不覆盖持续显示各平台状态的控制台标题。
        desktop_notice("采集警报 | " + " | ".join(f"{PROJECTS[n]}：{states[n]}" for n in jobs))

    def observe(name, text):
        nonlocal fail_fast_abort
        level, message = alert_log_line(text)
        if level:
            # 原文仅在内存中用于区分验证/临时异常；输出和状态文件仍经过摘要脱敏。
            (warnings if level == "WARNING" else errors)[name] = message
        if (name == "kuaishou" and level == "WARNING"
                and message.startswith("连续3篇评论分页停滞：本轮暂停评论请求")):
            paused_comments.add(name)
            if (name not in results and name not in retry_at and not stop.is_set()
                    and processes[name].returncode is None and states[name] != "评论暂停，贴文仍在采集"):
                if fail_fast:
                    fail_fast_abort = True
                    results[name] = 1
                    states[name] = "连续评论分页停滞，故障触发全停；断点保留"
                    warn(name, message + "；严格模式立即停止所有平台，不继续搜索")
                    stop.set()
                else:
                    states[name] = "评论暂停，贴文仍在采集"
                    warn(name, message)

    def start(name, manual=False):
        nonlocal fail_fast_abort
        if stop.is_set():
            return
        if name in readers:
            forward_logs({name: readers[name]}, pending, final=True)
            readers.pop(name).close()
        pending.pop(name, None)
        errors.pop(name, None)
        warnings.pop(name, None)
        paused_comments.discard(name)
        try:
            # 同一轮的重启追加日志；新reader从本次起点读取，不重打上次的数据卡片。
            resuming = attempts[name] or manual_resumes[name]
            with (logdir / (name + ".log")).open("a" if resuming else "w", encoding="utf-8") as logfile:
                log_starts[name] = logfile.tell()
                if resuming:
                    label = f"人工确认续采 {manual_resumes[name]}" if manual else f"自动续采 {attempts[name]}/{max_retries}"
                    logfile.write(f"\n【{label}】沿用原断点，不重扫完成任务\n")
                    logfile.flush()
                process = subprocess.Popen(jobs[name], cwd=ROOT, env=ENV,
                    stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), start_new_session=os.name != "nt")
        except OSError as exc:
            results[name] = last_codes[name] = 1
            states[name] = "启动失败，需人工处理"
            if not stop.is_set():
                warn(name, f"{type(exc).__name__}: {exc}")
                if fail_fast:
                    fail_fast_abort = True
                    stop.set()
            return
        processes[name] = process
        readers[name] = (logdir / (name + ".log")).open("rb")
        readers[name].seek(log_starts[name])
        states[name] = (f"采集中（PID {process.pid}；已自动续采{attempts[name]}/{max_retries}次；"
                        f"人工确认{manual_resumes[name]}次）")
        if attempts[name] or manual_resumes[name]:
            alerts[name] = f"[{PROJECTS[name]}] 已按原断点重新启动；是否恢复仍待本次结果，历史故障见警报.txt"
        print(f"[{PROJECTS[name]}] 进程已启动 PID={process.pid}，日志：{logdir / (name + '.log')}", flush=True)

    try:
        for name in jobs:
            if stop.is_set():
                break
            remaining = cooldowns.get(name, 0) - time.time()
            if remaining > 0:
                initial_wait.add(name)
                retry_at[name] = time.monotonic() + remaining
            else:
                start(name)
        publish()
        last_status = time.monotonic()
        while len(results) < len(jobs) and not stop.wait(0.2):
            # 在识别新失败之前消耗按键；提前按键不会被存成未来登录失败的自动授权。
            if resume_poll is not None:
                requested = resume_poll()
                if requested is None:
                    resume_poll = None
                    for name in list(manual_wait):
                        manual_wait.remove(name)
                        manual_not_before.pop(name)
                        manual_kinds.pop(name)
                        results[name] = last_codes[name]
                        states[name] = "人工处理等待已结束：输入通道不可用，请处理后重新运行"
                    publish()
                else:
                    for name in dict.fromkeys(requested):
                        if stop.is_set():
                            break
                        if name not in manual_wait:
                            print(f"[人工确认] {PROJECTS.get(name, '未知平台')}当前未等待人工确认，未重复启动。", flush=True)
                            continue
                        remaining = math.ceil(manual_not_before[name] - time.monotonic())
                        if remaining > 0:
                            print(f"[人工确认] {PROJECTS[name]}仍须遵守冷却要求，至少再等{remaining}秒后重新按键确认。", flush=True)
                            continue
                        manual_wait.remove(name)
                        manual_not_before.pop(name)
                        manual_kinds.pop(name)
                        manual_resumes[name] += 1
                        print(f"[人工确认] {PROJECTS[name]}：收到处理完成确认，只恢复该平台；仍由采集器检查实际条件。", flush=True)
                        start(name, manual=True)
                        publish()
            for name, deadline in list(retry_at.items()):
                if time.monotonic() >= deadline and not stop.is_set():
                    retry_at.pop(name)
                    if name in initial_wait:
                        initial_wait.remove(name)
                    else:
                        attempts[name] += 1
                    start(name)
                    publish()
            if stop.is_set():
                break
            # 先查退出码，不等积压的数据卡片打印完才报警。
            for name, process in processes.items():
                if name in results or name in retry_at or name in manual_wait or process.poll() is None:
                    continue
                code = last_codes[name] = process.returncode
                states[name] = ("正常结束（退出码0）" if code == 0 else
                                "结束但未全部完成（退出码2）" if code == 2 else f"失败退出（退出码{code}）")
                if code == 2 and test_requests is not None and not stop.is_set():
                    with (logdir / (name + ".log")).open("rb") as file:
                        file.seek(log_starts[name])
                        if "有界测试达到请求上限".encode("utf-8") in file.read():
                            results[name] = 2
                            states[name] = "有界测试达到请求上限；未完成、原断点保留"
                            alerts.pop(name, None)
                            publish()
                            continue
                if code != 0 and not stop.is_set():
                    # 只取本次尝试的末尾，旧日志中的登录/限流错误不能影响新一轮判定。
                    try:
                        with (logdir / (name + ".log")).open("rb") as file:
                            size = file.seek(0, os.SEEK_END)
                            file.seek(max(log_starts[name], size - 65536))
                            for text in file.read().decode("utf-8", errors="replace").splitlines():
                                observe(name, text)
                    except OSError:
                        pass
                    reason = (errors.get(name) or warnings.get(name) or
                              ("存在待补任务或参数错误，请查看日志与覆盖报告" if code == 2 else "进程异常退出，详情见平台日志"))
                    delay = retry_wait(name, code, reason, retry_delay * 2 ** attempts[name], name in paused_comments)
                    kind = manual_pause_kind(name, reason) if code == 1 else None
                    manual_delay = retry_after_wait(reason, 1800 if kind == "检查访问限制" else 0) if kind else None
                    if delay is not None:
                        remember_cooldown(name, delay)
                    if manual_delay is not None:
                        remember_cooldown(name, manual_delay)
                    if fail_fast:
                        results[name] = code
                        states[name] = ("尚有未完成任务，严格模式触发全停" if code == 2 else
                                        f"失败退出（退出码{code}），严格模式触发全停")
                        if code != 2:
                            try:
                                slower = lower_rate_if_needed(name, reason, fast=fast, high=high, ultra=ultra,
                                                              ultimate=ultimate)
                                if slower:
                                    reason += f"；下次启动该平台最低间隔已从{slower[0]}秒降频至{slower[1]}秒；本轮不重试"
                            except (OSError, ValueError, RuntimeError):
                                reason += "；间隔配置保存失败，须人工检查，禁止自动重启"
                        fail_fast_abort = True
                        warn(name, reason + "；停止全部平台，原断点保留")
                        stop.set()
                        break
                    if manual_delay is not None and resume_poll is not None:
                        manual_wait.add(name)
                        manual_kinds[name] = kind
                        manual_not_before[name] = time.monotonic() + manual_delay
                        key = next(k for k, n in RESUME_KEYS.items() if n == name)
                        states[name] = f"等待人工{kind}；完成后回本窗口按{key}确认续采"
                        if kind == "解除CSV占用":
                            instruction = "请关闭Excel/WPS中占用的CSV并检查目录写权限；不强制关闭应用、不丢弃数据库候选"
                        elif kind == "检查访问限制":
                            instruction = "至少冷却30分钟，并在原浏览器确认限制解除、回到正常搜索页；不保证重新登录能解决"
                        else:
                            instruction = "请在采集使用的专用浏览器完成登录或验证，不换账号/profile；三站均直接复用浏览器登录态"
                        reason += f"；{instruction}。等待期间采集器不发送业务请求；按键不保证问题已解决"
                        if manual_delay:
                            reason += f"；至少等待{math.ceil(manual_delay)}秒，未满时确认键不启动采集"
                    elif delay is not None and attempts[name] < max_retries:
                        retry_at[name] = time.monotonic() + delay
                        reason += f"；本次退出码{code}，等待{math.ceil(delay)}秒后按原断点自动续采"
                    else:
                        results[name] = code
                        states[name] += ("；自动续采次数用尽/已关闭" if delay is not None else "；需人工处理，不自动重试")
                    # publish会刷新倒计时；先刷新状态，弹窗才能准确显示重试计划。
                    if name in retry_at:
                        states[name] = f"冷却等待{math.ceil(delay)}秒，待第{attempts[name] + 1}/{max_retries}次自动续采"
                    warn(name, reason)
                else:
                    results[name] = code
                    alerts.pop(name, None)
                    publish()
            forward_logs(readers, pending, on_line=observe)
            if time.monotonic() - last_status >= 30:
                publish()
                last_status = time.monotonic()
    finally:
        if stop.is_set():
            print("停止全部采集，等待落盘（最多60秒）；正常退出保留浏览器，超时清理本次任务进程树。", flush=True)
        try:
            stop_processes(processes.values())
        finally:
            try:
                retry_at.clear()  # Ctrl+C/总开关异常取消所有待重启任务，不在清理阶段重新拉起。
                manual_wait.clear()
                manual_not_before.clear()
                manual_kinds.clear()
                initial_wait.clear()
                # 先固定终态，避免最后补读日志把手动停止/已退出的平台误报为仍在采集。
                for name in jobs:
                    if name not in results:
                        if name in processes and processes[name].poll() is None:
                            states[name] = "停止未确认，请人工检查进程"
                        elif fail_fast_abort:
                            states[name] = "其它平台故障导致全停/未启动；原断点保留"
                        else:
                            states[name] = "手动停止/未启动，自动续采已取消" if stop.is_set() else "总开关异常结束，请检查"
                forward_logs(readers, pending, final=True)
            finally:
                for reader in readers.values():
                    reader.close()
                publish()
    outcomes = {n: results.get(n, last_codes.get(n, processes[n].returncode if n in processes else "未启动")) for n in jobs}
    print("退出汇总：" + ", ".join(f"{PROJECTS[n]}={code}" for n, code in outcomes.items()), flush=True)
    return 1 if fail_fast_abort else 130 if stop.is_set() else 1 if any(v not in (0, 2) for v in results.values()) else 2 if 2 in results.values() else 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # 旧终端不支持的表情不应中断采集总开关。
    parser = argparse.ArgumentParser(description="并行增量采集；可恢复异常冷却后有限续采，Ctrl+C取消等待和全部任务")
    parser.add_argument("--only", nargs="+", choices=PROJECTS, help="仅启动所选平台，默认全部")
    parser.add_argument("--check", action="store_true", help="仅离线检查，不联网采集/不启动浏览器")
    parser.add_argument("--test-alert", action="store_true", help="仅测试本机警报，不启动采集、不修改采集状态")
    parser.add_argument("--retries", type=int, default=3, help="每平台本轮最多自动续采次数，0关闭，默认3，上限10")
    parser.add_argument("--test-requests", type=int, help="有界采集测试：单平台请求上限（1–10），须 --only 且 --retries 0")
    parser.add_argument("--fail-fast", action="store_true", help="任一平台失败或未完成即终止全部平台，不自动重试；需 --retries 0")
    speed = parser.add_mutually_exclusive_group()
    speed.add_argument("--fast", action="store_true", help="提速档：按采集器默认间隔运行，但不跳过已自动降频的最低间隔")
    speed.add_argument("--high-speed", action="store_true", help="高速档：抖音20–35秒、快手40秒、小红书60–75秒；保留已自动降频的最低间隔")
    speed.add_argument("--ultra-speed", action="store_true", help="超高速档：抖音/小红书10–25秒、快手20秒；保留已自动降频的最低间隔")
    speed.add_argument("--ultimate-speed", action="store_true", help="究极档：抖音/小红书10–25秒、快手10秒；保留自动降频和定期休息")
    parser.add_argument("--shard", choices=("full", "forward", "reverse"), default="full",
                        help="938个查询前469顺序/后469倒序；两机各用独立状态目录")
    parser.add_argument("--retry-delay", type=int, default=300, help="首次冷却秒数，后续倍增；默认300，至少30秒；遵守Retry-After")
    parser.add_argument("--dy-port", type=int, default=9222)
    parser.add_argument("--xhs-port", type=int, default=9223)
    parser.add_argument("--ks-port", type=int, default=9224)
    parser.add_argument("--worker", choices=PROJECTS, help=argparse.SUPPRESS)
    parser.add_argument("--show-notice", nargs=2, metavar=("TITLE", "MESSAGE"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.show_notice:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, args.show_notice[1], args.show_notice[0], 0x50030)
            return 0
        except (OSError, AttributeError):
            print("系统提示不可用，请查看终端警报和状态文件。", file=sys.stderr)
            return 1  # 不递归调用emit_alert，不进入采集/锁/状态文件逻辑。
    if args.test_alert:
        emit_alert(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 警报自测：这不是采集故障。\n"
                   "未联网、未启动或停止采集、未修改CSV和断点。\nWindows应弹出确认框并播放系统提示音（取决于系统声音设置）。",
                   ROOT / "logs/diagnostics/报警自测.txt")
        return 0
    if not 0 <= args.retries <= 10 or not 30 <= args.retry_delay <= 86400:
        parser.error("retries须为0–10，retry-delay须为30–86400秒")
    ports = (args.dy_port, args.xhs_port, args.ks_port)
    if not all(1 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        parser.error("三平台 CDP 端口必须在1–65535之间且互不相同")
    if args.test_requests is not None and not 1 <= args.test_requests <= 10:
        parser.error("test-requests 须为1–10")
    if args.fail_fast and (args.retries != 0 or args.test_requests is not None):
        parser.error("全量严格模式须 --retries 0，不可与有界采集测试同时使用")
    if args.shard != "full" and not args.worker and (not args.fail_fast or args.test_requests is not None):
        parser.error("分片必须使用 --fail-fast --retries 0，不允许测试额度冒充全量")
    if args.worker:
        return run_worker(args.worker, args.dy_port, args.xhs_port, args.test_requests, args.shard, args.check,
                          args.fast, args.high_speed, args.ultra_speed, args.ultimate_speed, args.ks_port)
    names = list(dict.fromkeys(args.only or PROJECTS))
    if args.test_requests is not None and (len(names) != 1 or args.retries != 0):
        parser.error("有界采集测试须 --only 唯一平台且 --retries 0")
    try:
        for name in names:
            check_project(name)
        if args.fail_fast:
            validate_scope()
        rates = selected_rates(args.fast, high=args.high_speed, ultra=args.ultra_speed,
                               ultimate=args.ultimate_speed)  # 配置损坏时拒绝启动，不回退到更快默认间隔。
        if args.check:
            print("三站均使用本机浏览器RPC；未校验登录有效性或线上接口，遇到验证仍停止。")
            return 0
        with workspace_lock():
            logdir = ROOT / "logs/runs" / (time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}")
            logdir.mkdir(parents=True)
            latest = ROOT / "logs/最新日志.txt"
            temporary = latest.with_suffix(".tmp")
            temporary.write_text(logdir.relative_to(ROOT).as_posix() + "\n", encoding="utf-8")
            temporary.replace(latest)  # 仅记录最近启动目录，不代表采集成功或仍在运行。
            stop = threading.Event()
            for event in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])):
                signal.signal(event, lambda *_: stop.set())
            worker_command = ([project_python()] if getattr(sys, "frozen", False) else
                              [project_python(), "-u", str(Path(__file__).resolve())])
            jobs = {n: [*worker_command, "--worker", n, "--dy-port", str(args.dy_port), "--xhs-port", str(args.xhs_port),
                         "--ks-port", str(args.ks_port),
                         *(["--ultimate-speed"] if args.ultimate_speed else ["--ultra-speed"] if args.ultra_speed else
                           ["--high-speed"] if args.high_speed else ["--fast"] if args.fast else []),
                         *(["--test-requests", str(args.test_requests)] if args.test_requests is not None else []),
                         *(["--shard", args.shard] if args.shard != "full" else [])] for n in names}
            print(f"本机浏览器RPC端口：抖音{args.dy_port}、小红书{args.xhs_port}、快手{args.ks_port}；各自独立登录档案。", flush=True)
            print("启动增量续采：只处理未完成部分，沿用原游标；不重扫已完成任务，不自动补旧详情。Ctrl+C 停止。", flush=True)
            if args.fail_fast:
                print(f"严格分片模式（{args.shard}）：三站使用需求文档词表与固定日期；"
                      "任一平台失败/未完成，全站停止并报警，不自动重试。", flush=True)
            print(f"可冷却复查的异常：每平台最多自动续采{args.retries}次，首次等{args.retry_delay}秒，之后倍增；登录/验证等需人工处理。", flush=True)
            interactive = sys.stdin is not None and sys.stdin.isatty()
            print(f"{'究极' if args.ultimate_speed else '超高速' if args.ultra_speed else '高速' if args.high_speed else '提速' if args.fast else '常规'}档位：抖音{rates['douyin']}–{rates['douyin']+15}秒、快手{rates['kuaishou']}秒、"
                  f"小红书{rates['xiaohongshu']}–{rates['xiaohongshu']+15}秒；"
                  "定期休息，跨重启保留已记录的冷却截止时间。短时测试不保证长期稳定。", flush=True)
            print("严格模式故障即全停；数字键不恢复采集，人工处理后重新运行原入口。" if args.fail_fast else
                  "人工处理后按1=抖音、2=快手、3=小红书确认续采；只恢复等待中的平台。" if interactive else
                  "当前没有交互输入；登录/验证失败时仍会退出，需人工处理后重新运行。", flush=True)
            return supervise(jobs, logdir, stop, args.retries, args.retry_delay,
                             resume_poll=read_resume_keys if interactive and not args.fail_fast else None,
                             cooldown_path=ROOT / "runtime/collector_cooldowns.json",
                             test_requests=args.test_requests, fail_fast=args.fail_fast,
                             fast=args.fast, high=args.high_speed, ultra=args.ultra_speed,
                             ultimate=args.ultimate_speed)
    except Exception as exc:
        message = f"{time.strftime('%Y-%m-%d %H:%M:%S')} 总开关启动/运行失败：{type(exc).__name__}: {alert_summary(str(exc))}"
        print(message, file=sys.stderr)
        emit_alert(message + "\n本次命令已停止；如已有另一总开关在运行，不要重复启动。",
                   ROOT / "logs/启动失败.txt")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
