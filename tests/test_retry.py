"""延迟续采：仅本地假任务/临时文件，不联网、不启动浏览器、不弹真实提示。"""
from contextlib import ExitStack
from email.utils import formatdate
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start_all as app
from test_alerts import job


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.run = self.root / "logs/runs/fixture"
        self.run.mkdir(parents=True)
        self.stack.enter_context(patch.object(app, "ROOT", self.root))
        self.stack.enter_context(patch.object(app, "desktop_notice"))
        self.stack.enter_context(patch.object(sys, "stdout", io.StringIO()))

    def test_failure_waits_then_resumes_checkpoint_without_blocking_peer(self):
        recovering = job("""from pathlib import Path
cursor = Path('cursor.txt')
data = Path('data.csv')
if not cursor.exists():
    data.write_text('id\\na\\n')
    cursor.write_text('saved-page-1')
    print('first page saved', flush=True)
    print('采集未完成：抖音评论响应异常（comments=NoneType，has_more=0，total=None）', flush=True)
    raise SystemExit(1)
assert cursor.read_text() == 'saved-page-1'
assert data.read_text() == 'id\\na\\n'
with data.open('a') as f: f.write('b\\n')
cursor.write_text('saved-page-2')
print('resume page 2', flush=True)
Path('resumed').touch()
""")
        peer = job("""import time
from pathlib import Path
count = 0
end = time.monotonic() + 6
while not Path('resumed').exists() and time.monotonic() < end:
    count += 1
    print('peer is collecting', flush=True)
    time.sleep(0.05)
Path('peer-count').write_text(str(count))
assert Path('resumed').exists()
""")
        self.assertEqual(app.supervise({"douyin": recovering, "xiaohongshu": peer}, self.run,
                                       threading.Event(), max_retries=1, retry_delay=0.35), 0)
        self.assertEqual((self.root / "data.csv").read_text(), "id\na\nb\n")
        self.assertEqual((self.root / "cursor.txt").read_text(), "saved-page-2")
        self.assertGreaterEqual(int((self.root / "peer-count").read_text()), 4)
        log = (self.run / "douyin.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("first page saved"), 1)
        self.assertIn("【自动续采 1/1】", log)
        self.assertIn("resume page 2", log)
        self.assertEqual(sys.stdout.getvalue().count("[抖音] first page saved"), 1)
        status = (self.root / "logs/最新状态.txt").read_text(encoding="utf-8")
        self.assertIn("抖音：正常结束（退出码0）", status)
        self.assertIn("小红书：正常结束（退出码0）", status)
        self.assertNotIn("冷却等待", status)
        self.assertIn("冷却等待", (self.run / "警报.txt").read_text(encoding="utf-8"))

    def test_retry_cap_and_exponential_delays_are_per_platform(self):
        source = """from pathlib import Path
p = Path('attempts')
p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))
print('采集未完成：接口 HTTP 503；停止采集', flush=True)
raise SystemExit(1)
"""
        policy = app.retry_wait
        def fast_policy(name, code, reason, delay, paused):
            return policy(name, code, reason, 0, paused)
        with patch.object(app, "retry_wait", side_effect=fast_policy) as calls:
            self.assertEqual(app.supervise({"kuaishou": job(source)}, self.run, threading.Event(),
                                           max_retries=3, retry_delay=300), 1)
        self.assertEqual([c.args[3] for c in calls.call_args_list], [300, 600, 1200, 2400])
        self.assertEqual((self.root / "attempts").read_text(), "4")  # 初次+最多3次；最后2400秒不再安排。
        self.assertEqual((self.run / "kuaishou.log").read_text(encoding="utf-8").count("HTTP 503"), 4)
        self.assertIn("次数用尽", (self.root / "logs/最新状态.txt").read_text(encoding="utf-8"))

    def test_old_attempt_errors_never_authorize_new_unknown_or_login_failure(self):
        for index, last_line in enumerate(("采集未完成：需要人工登录", "new failure without a known retry reason")):
            with self.subTest(last_line=last_line):
                run = self.run / str(index)
                run.mkdir()
                source = f"""from pathlib import Path
p = Path('counter-{index}')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
print('采集未完成：接口 HTTP 503；停止采集' if n == 1 else {last_line!r}, flush=True)
raise SystemExit(1)
"""
                self.assertEqual(app.supervise({"kuaishou": job(source)}, run, threading.Event(),
                                               max_retries=3, retry_delay=0), 1)
                self.assertEqual((self.root / f"counter-{index}").read_text(), "2")
                self.assertIn("需人工处理", (self.root / "logs/最新状态.txt").read_text(encoding="utf-8"))

    def test_ctrl_c_cancels_wait_without_respawning(self):
        stop = threading.Event()
        def display(text, **kwargs):
            if text.startswith("[平台状态]") and "冷却等待" in text:
                stop.set()
        source = "from pathlib import Path; Path('once').touch(); print('采集未完成：接口 HTTP 503'); raise SystemExit(1)"
        with patch.object(app, "print", side_effect=display, create=True), \
             patch.object(app.subprocess, "Popen", wraps=app.subprocess.Popen) as spawn:
            self.assertEqual(app.supervise({"kuaishou": job(source)}, self.run, stop,
                                           max_retries=3, retry_delay=300), 130)
            self.assertEqual(spawn.call_count, 1)
        self.assertIn("自动续采已取消", (self.root / "logs/最新状态.txt").read_text(encoding="utf-8"))

    def test_paused_comments_partial_exit_can_resume_but_plain_pending_cannot(self):
        source = """from pathlib import Path
p = Path('paused-once')
if not p.exists():
    p.touch()
    print('2026-09-28 18:00:00,000 | kuaishou | WARNING | 连续3篇评论分页停滞：本轮暂停评论请求，继续搜索并保存贴文')
    print('2026-09-28 18:00:01,000 | kuaishou | WARNING | 尚有3篇评论未完成')
    raise SystemExit(2)
print('saved comments from old cursor')
"""
        self.assertEqual(app.supervise({"kuaishou": job(source)}, self.run, threading.Event(),
                                       max_retries=1, retry_delay=0), 0)
        self.assertIn("自动续采 1/1", (self.run / "kuaishou.log").read_text(encoding="utf-8"))
        self.assertIsNone(app.retry_wait("xiaohongshu", 2, "尚有旧评论缺少候选令牌", 300))
        self.assertIsNone(app.retry_wait("kuaishou", 2, "还有未完成评论", 300))

    def test_policy_observed_errors_hard_stops_and_retry_after(self):
        for name, message in (("douyin", "抖音评论响应异常（内容ID x，comments=NoneType，has_more=int）"),
                              ("kuaishou", "接口拒绝 result=2；停止，不自动重试"),
                              ("xiaohongshu", "GET /browser-page HTTP 461 code=unknown Retry-After='未提供'：访问被拒绝，具体原因待排查；已停止，不自动重试或重新登录")):
            self.assertEqual(app.retry_wait(name, 1, message, 300), 900 if name == 'kuaishou' else 300)
        for message in ("需要重新登录 HTTP 503", "HTTP 461 安全验证", "NEED_LOGIN", "CSV 被占用 HTTP 503",
                        "HTTP 401", "HTTP 403", "HTTP 461 code=-100", "断点损坏 HTTP 503", "未知问题",
                        "抖音评论响应异常" + "x" * 500 + "需要人工登录"):
            self.assertIsNone(app.retry_wait("douyin", 1, message, 300))
        self.assertIsNone(app.retry_wait("douyin", 130, "TimeoutError", 300))
        self.assertEqual(app.retry_wait("xiaohongshu", 1, "HTTP 429 Retry-After='900'", 300), 900)
        self.assertEqual(app.retry_wait("xiaohongshu", 1, "HTTP 429 Retry-After='30'", 300), 300)
        with patch.object(app.time, "time", return_value=1700000000):
            future = formatdate(1700000900, usegmt=True)
            self.assertEqual(app.retry_wait("xiaohongshu", 1, f"HTTP 429 Retry-After='{future}'", 300), 900)
        self.assertIsNone(app.retry_wait("xiaohongshu", 1, "HTTP 429 Retry-After='invalid'", 300))

    def test_uncertain_rpc_timeout_cannot_trigger_automatic_retry(self):
        for name in app.PROJECTS:
            for reason in ('timeout_no_retry',
                           '页面原生请求失败或超时（timeout_no_retry, HTTP=None）；结果可能不确定，不自动重发',
                           'TimeoutError: RPC结果未知',
                           'HTTP 503；结果不确定，不自动重发'):
                with self.subTest(name=name, reason=reason):
                    self.assertIsNone(app.retry_wait(name, 1, reason, 300))

    def test_manual_confirmation_is_per_platform_and_never_replays_old_keys(self):
        def worker(name, failures):
            return job(f"""from pathlib import Path
name, failures = {name!r}, {failures}
p = Path(name + '-attempts')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
cursor, data = Path(name + '-cursor'), Path(name + '-data.csv')
if n == 1:
    cursor.write_text('page-1')
    data.write_text('id\\na\\n')
assert cursor.read_text() == 'page-1'
assert data.read_text() == 'id\\na\\n'
if n <= failures:
    print('采集未完成：浏览器需要人工登录或验证', flush=True)
    raise SystemExit(1)
with data.open('a') as f: f.write('b\\n')
cursor.write_text('page-2')
Path(name + '-resumed').touch()
""")
        peer = job("""import time
from pathlib import Path
p = Path('peer-starts')
p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))
end = time.monotonic() + 10
n = 0
while time.monotonic() < end:
    n += 1
    Path('peer-ticks').write_text(str(n))
    if Path('douyin-resumed').exists() and Path('xiaohongshu-resumed').exists(): break
    time.sleep(0.05)
else: raise SystemExit(7)
""")
        stop = threading.Event()
        phase = polls = waited = 0
        def confirm():
            nonlocal phase, polls, waited
            polls += 1
            if polls > 40:
                stop.set()
                self.fail('人工续采状态机未结束')
            status = (self.root / 'logs/最新状态.txt').read_text(encoding='utf-8')
            if phase == 0:
                phase = 1
                return ['douyin', 'xiaohongshu']  # 失败被识别前的按键不授权后续重启。
            if phase == 1 and '抖音：等待人工登录' in status and '小红书：等待人工登录' in status:
                waited += 1
                self.assertEqual((self.root / 'douyin-attempts').read_text(), '1')
                self.assertEqual((self.root / 'xiaohongshu-attempts').read_text(), '1')
                if waited >= 3:
                    phase = 2
                    self.assertGreater(int((self.root / 'peer-ticks').read_text()), 3)
                    return ['douyin', 'douyin', 'kuaishou']  # 重复确认和运行中平台均不多开进程。
            if phase == 2 and '抖音：等待人工登录' in status and (self.root / 'douyin-attempts').read_text() == '2':
                self.assertEqual((self.root / 'xiaohongshu-attempts').read_text(), '1')
                phase = 3
                return ['xiaohongshu']
            if phase == 3 and '小红书：正常结束' in status:
                self.assertIn('抖音：等待人工登录', status)
                self.assertEqual((self.root / 'douyin-attempts').read_text(), '2')
                phase = 4
                return ['douyin']  # 上次确认后仍需登录，必须重新确认一次。
            return []
        self.assertEqual(app.supervise({'douyin': worker('douyin', 2), 'xiaohongshu': worker('xiaohongshu', 1),
                                       'kuaishou': peer}, self.run, stop, max_retries=0, resume_poll=confirm), 0)
        self.assertEqual(phase, 4)
        self.assertEqual((self.root / 'peer-starts').read_text(), '1')
        for name, confirmations in (('douyin', 2), ('xiaohongshu', 1)):
            self.assertEqual((self.root / (name + '-data.csv')).read_text(), 'id\na\nb\n')
            self.assertEqual((self.root / (name + '-cursor')).read_text(), 'page-2')
            log = (self.run / (name + '.log')).read_text(encoding='utf-8')
            self.assertEqual(log.count('采集未完成：'), confirmations)
            self.assertEqual(log.count('【人工确认续采'), confirmations)
            self.assertNotIn('【自动续采', log)
        self.assertNotIn('等待人工', (self.run / '状态.txt').read_text(encoding='utf-8'))

    def test_manual_wait_can_be_cancelled_or_loses_input_without_restart(self):
        for mode in ('cancel', 'disconnect'):
            with self.subTest(mode=mode):
                run = self.run / mode
                run.mkdir()
                stop = threading.Event()
                def confirm():
                    status = (self.root / 'logs/最新状态.txt').read_text(encoding='utf-8')
                    if '小红书：等待人工登录' in status:
                        if mode == 'disconnect':
                            return None
                        stop.set()
                        return ['xiaohongshu']  # 同时到达的确认不能越过Ctrl+C。
                    return []
                with patch.object(app.subprocess, 'Popen', wraps=app.subprocess.Popen) as spawn:
                    code = app.supervise({'xiaohongshu': job("print('采集未完成：需要人工登录'); raise SystemExit(1)")},
                                         run, stop, resume_poll=confirm)
                    self.assertEqual(code, 130 if mode == 'cancel' else 1)
                    self.assertEqual(spawn.call_count, 1)
                self.assertIn('手动停止' if mode == 'cancel' else '输入通道不可用',
                              (run / '状态.txt').read_text(encoding='utf-8'))

    def test_manual_resume_does_not_reset_automatic_retry_budget(self):
        source = """from pathlib import Path
p = Path('attempts')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
print('采集未完成：需要人工登录' if n == 2 else '采集未完成：接口 HTTP 503', flush=True)
raise SystemExit(1)
"""
        def confirm():
            status = (self.root / 'logs/最新状态.txt').read_text(encoding='utf-8')
            return ['douyin'] if '抖音：等待人工登录' in status else []
        self.assertEqual(app.supervise({'douyin': job(source)}, self.run, threading.Event(),
                                       max_retries=1, retry_delay=0, resume_poll=confirm), 1)
        self.assertEqual((self.root / 'attempts').read_text(), '3')
        log = (self.run / 'douyin.log').read_text(encoding='utf-8')
        self.assertEqual(log.count('【自动续采'), 1)
        self.assertEqual(log.count('【人工确认续采'), 1)
        self.assertIn('次数用尽', (self.run / '状态.txt').read_text(encoding='utf-8'))

    def test_manual_confirmation_cannot_override_retry_after_or_corruption(self):
        stop = threading.Event()
        pressed = 0
        def confirm():
            nonlocal pressed
            status = (self.root / 'logs/最新状态.txt').read_text(encoding='utf-8')
            if '小红书：等待人工登录' in status:
                pressed += 1
                if pressed > 2: stop.set()
                return ['xiaohongshu']
            return []
        with patch.object(app.subprocess, 'Popen', wraps=app.subprocess.Popen) as spawn:
            self.assertEqual(app.supervise({'xiaohongshu': job(
                "print(\"采集未完成：HTTP 429 NEED_LOGIN Retry-After='900'\"); raise SystemExit(1)")},
                self.run, stop, resume_poll=confirm), 130)
            self.assertEqual(spawn.call_count, 1)
        self.assertIn('仍须遵守冷却要求', sys.stdout.getvalue())
        for reason in ('NEED_LOGIN 断点损坏', "NEED_LOGIN Retry-After='invalid'", '未知错误 HTTP 403'):
            with self.subTest(reason=reason), patch.object(app.subprocess, 'Popen', wraps=app.subprocess.Popen) as spawn:
                self.assertEqual(app.supervise({'douyin': job(f'print({("采集未完成：" + reason)!r}); raise SystemExit(1)')},
                                               self.run, threading.Event(), resume_poll=lambda: ['douyin']), 1)
                self.assertEqual(spawn.call_count, 1)
        for reason in ('浏览器需要人工登录或验证', 'NEED_LOGIN', 'HTTP 401', 'manual_verification'):
            self.assertTrue(app.needs_manual_login(reason))
        for reason in ('HTTP 461 code=unknown', 'HTTP 403', 'NEED_LOGIN 断点损坏', '需要人工登录' + 'x' * 8000):
            self.assertFalse(app.needs_manual_login(reason))

    def test_native_confirmation_keys_and_cli_interactive_gate(self):
        keys = list('\xe0333x12\r')
        native = Mock()
        native.kbhit.side_effect = lambda: bool(keys)
        native.getwch.side_effect = lambda: keys.pop(0)
        with patch.object(sys, 'stdin', Mock(isatty=lambda: True)), patch.object(app.os, 'name', 'nt'), \
             patch.dict(sys.modules, {'msvcrt': native}):
            self.assertEqual(app.read_resume_keys(), ['xiaohongshu', 'douyin', 'kuaishou'])
            self.assertEqual(app.read_resume_keys(), [])
            native.kbhit.side_effect = OSError('closed')
            self.assertIsNone(app.read_resume_keys())
        with patch.object(sys, 'stdin', None):
            self.assertIsNone(app.read_resume_keys())
        with patch.object(sys, 'stdin', Mock(isatty=Mock(side_effect=ValueError('closed')))):
            self.assertIsNone(app.read_resume_keys())
        with patch.object(app, 'check_project'), patch.object(app, 'supervise', return_value=0) as run, \
             patch.object(app.signal, 'signal'):
            for interactive in (True, False):
                with patch.object(sys, 'stdin', Mock(isatty=lambda: interactive)), \
                     patch.object(app, 'ROOT', self.root / str(interactive)):
                    self.assertEqual(app.main(['--only', 'xiaohongshu']), 0)
                    self.assertIs(run.call_args.kwargs['resume_poll'], app.read_resume_keys if interactive else None)

    def test_csv_lock_manual_resume_and_access_restriction_wait(self):
        source = """from pathlib import Path
p = Path('csv-attempts')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
if n == 1:
    Path('saved-cursor').write_text('page-7')
    print('采集未完成：CSV 被占用或写入权限不足（系统错误码32；本地等待1831秒仍失败），未能更新：fixture.csv；已采候选仍保存在状态库', flush=True)
    raise SystemExit(1)
assert Path('released').exists() and Path('saved-cursor').read_text() == 'page-7'
"""
        def confirm_csv():
            status = (self.run / '状态.txt').read_text(encoding='utf-8')
            if '抖音：等待人工解除CSV占用' in status:
                (self.root / 'released').touch()
                return ['douyin']
            return []
        self.assertEqual(app.supervise({'douyin':job(source)}, self.run, threading.Event(), resume_poll=confirm_csv),0)
        self.assertEqual((self.root / 'csv-attempts').read_text(), '2')
        self.assertEqual((self.root / 'saved-cursor').read_text(), 'page-7')
        stop = threading.Event()
        pressed = 0
        def confirm_access():
            nonlocal pressed
            status = (self.run / '状态.txt').read_text(encoding='utf-8')
            if '小红书：等待人工检查访问限制' in status:
                pressed += 1
                if pressed > 1: stop.set()
                return ['xiaohongshu']
            return []
        with patch.object(app.subprocess,'Popen',wraps=app.subprocess.Popen) as spawn:
            self.assertEqual(app.supervise({'xiaohongshu':job("print('采集未完成：浏览器访问被限制（code=unknown，原因未明）'); raise SystemExit(1)")},
                                           self.run, stop, resume_poll=confirm_access),130)
            self.assertEqual(spawn.call_count,1)
        self.assertIn('仍须遵守冷却要求',sys.stdout.getvalue())
        for reason in ('断点损坏','PermissionError: unknown','HTTP 403','签名引擎失败'):
            self.assertIsNone(app.manual_pause_kind('douyin',reason))

    def test_cooldown_survives_supervisor_restart_and_corruption_fails_closed(self):
        path = self.root / 'cooldowns.json'
        stop = threading.Event()
        def display(text, **kw):
            if text.startswith('[平台状态]') and '冷却等待' in text: stop.set()
        worker = job("print('采集未完成：接口 HTTP 503'); raise SystemExit(1)")
        with patch.object(app,'print',side_effect=display,create=True):
            self.assertEqual(app.supervise({'kuaishou':worker},self.run,stop,cooldown_path=path),130)
        before = path.read_bytes()
        deadline = json.loads(before)['kuaishou']
        self.assertGreater(deadline,app.time.time())
        stop = threading.Event()
        def next_display(text, **kw):
            if text.startswith('[平台状态]') and '沿用上轮冷却' in text: stop.set()
        with patch.object(app,'print',side_effect=next_display,create=True), \
             patch.object(app.subprocess,'Popen',wraps=app.subprocess.Popen) as spawn:
            self.assertEqual(app.supervise({'kuaishou':worker},self.run,stop,cooldown_path=path),130)
            spawn.assert_not_called()
        self.assertEqual(path.read_bytes(),before)
        with patch.object(app.time,'time',return_value=deadline+1):
            self.assertEqual(app.supervise({'kuaishou':job("print('ok')")},self.run,threading.Event(),cooldown_path=path),0)
        self.assertNotIn('【自动续采', (self.run/'kuaishou.log').read_text(encoding='utf-8'))
        for invalid in ('{', '{"kuaishou":NaN}', '{"kuaishou":-1}', '{"kuaishou":true}', '[]'):
            path.write_text(invalid,encoding='utf-8')
            with self.subTest(invalid=invalid), patch.object(app.subprocess,'Popen') as spawn:
                with self.assertRaises((ValueError, RuntimeError)):
                    app.supervise({'kuaishou':worker},self.run,threading.Event(),cooldown_path=path)
                spawn.assert_not_called()

    def test_cli_validates_and_passes_retry_configuration(self):
        with patch.object(app, "check_project"), patch.object(app, "supervise", return_value=0) as run, \
             patch.object(app.signal, "signal"), patch.object(sys, "stderr", io.StringIO()):
            for flags in (("--retries", "-1"), ("--retries", "11"), ("--retry-delay", "0"), ("--retry-delay", "nan")):
                with self.assertRaises(SystemExit):
                    app.main([*flags, "--check"])
            run.assert_not_called()
            self.assertEqual(app.main(["--only", "douyin", "--retries", "2", "--retry-delay", "900"]), 0)
            self.assertEqual(run.call_args.args[3:], (2, 900))


if __name__ == "__main__":
    unittest.main()
