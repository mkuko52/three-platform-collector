"""警报离线验证：临时目录、本地假进程、模拟桌面提示，不触碰业务请求/正式数据。"""
import ctypes
from contextlib import nullcontext
import io
import itertools
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start_all as app

DESKTOP_NOTICE = app.desktop_notice


def job(source):
    return [sys.executable, "-u", "-c", source]


class AlertsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.run = self.root / "logs/runs/fixture"
        self.run.mkdir(parents=True)
        for target, attribute, value in ((app, "ROOT", self.root), (app, "desktop_notice", Mock()),
                                          (sys, "stdout", io.StringIO())):
            guard = patch.object(target, attribute, value)
            guard.start()
            self.addCleanup(guard.stop)

    def notices(self):
        return [call.args[1] for call in app.desktop_notice.call_args_list if len(call.args) > 1]

    def status(self):
        return (self.root / "logs/最新状态.txt").read_text(encoding="utf-8")

    def test_failure_before_log_backlog_drains_and_peer_keeps_collecting(self):
        bad = job("print('x' * 70000); print('采集未完成：需要人工登录', end='', flush=True); raise SystemExit(1)")
        good = job("""import time
from pathlib import Path
end = time.monotonic() + 5
while time.monotonic() < end:
    path = Path('logs/最新状态.txt')
    if path.exists() and '抖音：失败退出' in path.read_text(encoding='utf-8'):
        Path('peer-kept-running').touch()
        for i in range(40): print('仍在保存数据', i, flush=True)
        break
    time.sleep(0.02)
else: raise SystemExit(7)
""")
        self.assertEqual(app.supervise({"douyin": bad, "xiaohongshu": good}, self.run, threading.Event()), 1)
        self.assertTrue((self.root / "peer-kept-running").exists())
        self.assertIn("抖音：失败退出（退出码1）", self.status())
        self.assertIn("小红书：正常结束（退出码0）", self.status())
        self.assertIn("需要人工登录", self.status())
        self.assertEqual((self.run / "状态.txt").read_text(encoding="utf-8"), self.status())
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("需要人工登录", self.notices()[0])
        self.assertEqual((self.run / "警报.txt").read_text(encoding="utf-8").count("【采集警报"), 1)
        # stderr/尾部无换行照样读取，警報摘要不改动平台原始日志。
        self.assertTrue((self.run / "douyin.log").read_text(encoding="utf-8").endswith("采集未完成：需要人工登录"))
        self.assertIn("持续警报", sys.stdout.getvalue())

    def test_full_run_fail_fast_stops_all_and_only_slows_request_failures(self):
        rate_path=self.root/'runtime/collector_rate_overrides.json'
        peer=job("import time; time.sleep(10)")
        limited=job("print('采集未完成：快手请求频率受限，操作太快', flush=True); raise SystemExit(1)")
        self.assertEqual(app.supervise({'kuaishou':limited,'douyin':peer},self.run,threading.Event(),
                         max_retries=0,fail_fast=True,cooldown_path=self.root/'cooldowns.json'),1)
        self.assertEqual(app.rate_overrides(rate_path),{'kuaishou':135})
        self.assertIn('严格模式触发全停',self.status())
        self.assertIn('其它平台故障导致全停',self.status())
        self.assertIn('135秒',self.status())
        self.assertNotIn('其它平台可继续运行', sys.stdout.getvalue())
        self.assertNotIn('其它平台不受此提示影响', self.notices()[0])
        self.assertEqual(len(self.notices()),1)
        with patch.object(app,'ROOT',self.root):
            self.assertIsNone(app.lower_rate_if_needed('douyin','CSV 被占用'))
            self.assertEqual(app.rate_overrides(),{'kuaishou':135})
        self.assertEqual(app.arguments('kuaishou',9222,9223,rates={'kuaishou':90})[-2:],['--delay','90'])
        self.assertEqual(app.arguments('xiaohongshu',9222,9223,rates={'xiaohongshu':90})[-2:],['--min-delay','90'])

    def test_fast_bat_rates_and_backoff(self):
        path = self.root / 'runtime/collector_rate_overrides.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"douyin":45,"kuaishou":90,"xiaohongshu":90}', encoding='utf-8')
        self.assertEqual(app.selected_rates(path=path), app.NORMAL_SEED_RATES)
        self.assertEqual(app.selected_rates(True, path), app.RATE_DEFAULTS)
        with (patch.object(app, 'check_project'), patch.object(app, 'validate_scope'),
              patch.object(app, 'workspace_lock', return_value=nullcontext()),
              patch.object(app.signal, 'signal'), patch.object(app, 'supervise', return_value=0) as supervise):
            self.assertEqual(app.main(['--fail-fast', '--retries', '0', '--fast']), 0)
        jobs = supervise.call_args.args[0]
        self.assertTrue(all('--fast' in command for command in jobs.values()))
        self.assertEqual(supervise.call_args.kwargs['fast'], True)
        self.assertIn('提速档位：抖音30–45秒、快手60秒、小红书60–75秒', sys.stdout.getvalue())
        with (patch.object(app.os, 'chdir'), patch.object(app.signal, 'signal'),
              patch.object(sys, 'argv', []), patch.object(sys, 'path', sys.path[:]),
              patch.object(app.runpy, 'run_path')):
            self.assertEqual(app.run_worker('xiaohongshu', 9222, 9223, fast=True), 0)
            self.assertEqual(sys.argv[-2:], ['--min-delay', '60'])
        self.assertEqual(app.lower_rate_if_needed('douyin', 'HTTP 429', path, fast=True), (30, 68))
        self.assertEqual(app.selected_rates(True, path)['douyin'], 68)
        self.assertEqual(app.selected_rates(False, path)['douyin'], 68)
        self.assertEqual(app.rate_overrides(path)['kuaishou'], 90)
        self.assertEqual(app.lower_rate_if_needed('kuaishou', '频率受限', path, fast=False), (90, 135))
        self.assertEqual(app.selected_rates(True, path)['kuaishou'], 135)

    def test_high_speed_bat_keeps_xhs_floor_and_backoff(self):
        path = self.root / 'runtime/collector_rate_overrides.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"douyin":45,"kuaishou":90,"xiaohongshu":90}', encoding='utf-8')
        self.assertEqual(app.selected_rates(path=path, high=True), {'douyin':20,'kuaishou':40,'xiaohongshu':60})
        with (patch.object(app, 'check_project'), patch.object(app, 'validate_scope'),
              patch.object(app, 'workspace_lock', return_value=nullcontext()),
              patch.object(app.signal, 'signal'), patch.object(app, 'supervise', return_value=0) as supervise):
            self.assertEqual(app.main(['--fail-fast', '--retries', '0', '--high-speed']), 0)
        self.assertTrue(all('--high-speed' in command and '--fast' not in command
                            for command in supervise.call_args.args[0].values()))
        self.assertTrue(supervise.call_args.kwargs['high'])
        self.assertIn('高速档位：抖音20–35秒、快手40秒、小红书60–75秒', sys.stdout.getvalue())
        with (patch.object(app.os, 'chdir'), patch.object(app.signal, 'signal'),
              patch.object(sys, 'argv', []), patch.object(sys, 'path', sys.path[:]),
              patch.object(app.runpy, 'run_path')):
            for name, expected in [('douyin', ['--interval','20']), ('kuaishou', ['--delay','40']),
                                   ('xiaohongshu', ['--min-delay','60'])]:
                self.assertEqual(app.run_worker(name, 9222, 9223, high=True), 0)
                self.assertEqual(sys.argv[-2:], expected)
        self.assertEqual(app.lower_rate_if_needed('kuaishou', 'HTTP 429', path, high=True), (40, 135))
        self.assertEqual(app.selected_rates(path=path, high=True)['kuaishou'], 135)
        self.assertEqual(app.selected_rates(True, path)['kuaishou'], 135)
        with patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            app.main(['--fast', '--high-speed'])

    def test_ultra_speed_bat_preserves_limits_and_cooldown(self):
        path = self.root / 'runtime/collector_rate_overrides.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"douyin":45,"kuaishou":90,"xiaohongshu":90}', encoding='utf-8')
        self.assertEqual(app.selected_rates(path=path, ultra=True), app.ULTRA_RATES)
        with (patch.object(app, 'check_project'), patch.object(app, 'validate_scope'),
              patch.object(app, 'workspace_lock', return_value=nullcontext()),
              patch.object(app.signal, 'signal'), patch.object(app, 'supervise', return_value=0) as supervise):
            self.assertEqual(app.main(['--fail-fast', '--retries', '0', '--ultra-speed']), 0)
        self.assertTrue(all('--ultra-speed' in command for command in supervise.call_args.args[0].values()))
        self.assertTrue(supervise.call_args.kwargs['high'] is False and supervise.call_args.kwargs['ultra'])
        self.assertIn('超高速档位：抖音10–25秒、快手20秒、小红书10–25秒', sys.stdout.getvalue())
        with (patch.object(app.os, 'chdir'), patch.object(app.signal, 'signal'),
              patch.object(sys, 'argv', []), patch.object(sys, 'path', sys.path[:]),
              patch.object(app.runpy, 'run_path')):
            for name, expected in [('douyin', ['--interval','10']), ('kuaishou', ['--delay','20']),
                                   ('xiaohongshu', ['--min-delay','10'])]:
                self.assertEqual(app.run_worker(name, 9222, 9223, ultra=True), 0)
                self.assertEqual(sys.argv[-2:], expected)
        self.assertEqual(app.lower_rate_if_needed('douyin', 'HTTP 429', path, ultra=True), (10, 68))
        self.assertEqual(app.selected_rates(path=path, ultra=True)['douyin'], 68)
        self.assertEqual(app.selected_rates(True, path)['douyin'], 68)
        self.assertEqual(app.lower_rate_if_needed('xiaohongshu', 'HTTP 429', path, ultra=True), (10, 135))
        self.assertEqual(app.selected_rates(path=path, ultra=True)['xiaohongshu'], 135)
        with patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            app.main(['--ultra-speed', '--high-speed'])

    def test_ultimate_speed_bat_only_accelerates_kuaishou(self):
        path = self.root / 'runtime/collector_rate_overrides.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"douyin":45,"kuaishou":90,"xiaohongshu":90}', encoding='utf-8')
        self.assertEqual(app.selected_rates(path=path, ultimate=True), app.ULTIMATE_RATES)
        with (patch.object(app, 'check_project'), patch.object(app, 'validate_scope'),
              patch.object(app, 'workspace_lock', return_value=nullcontext()),
              patch.object(app.signal, 'signal'), patch.object(app, 'supervise', return_value=0) as supervise):
            self.assertEqual(app.main(['--fail-fast', '--retries', '0', '--ultimate-speed']), 0)
        jobs = supervise.call_args.args[0]
        self.assertTrue(all('--ultimate-speed' in command and '--ultra-speed' not in command
                            for command in jobs.values()))
        self.assertTrue(supervise.call_args.kwargs['ultimate'])
        self.assertIn('究极档位：抖音10–25秒、快手10秒、小红书10–25秒', sys.stdout.getvalue())
        with (patch.object(app.os, 'chdir'), patch.object(app.signal, 'signal'),
              patch.object(sys, 'argv', []), patch.object(sys, 'path', sys.path[:]),
              patch.object(app.runpy, 'run_path')):
            for name, expected in [('douyin', ['--interval','10']), ('kuaishou', ['--delay','10']),
                                   ('xiaohongshu', ['--min-delay','10'])]:
                self.assertEqual(app.run_worker(name, 9222, 9223, ultimate=True), 0)
                self.assertEqual(sys.argv[-2:], expected)
        self.assertEqual(app.lower_rate_if_needed('kuaishou', 'HTTP 429', path, ultimate=True), (10, 135))
        self.assertEqual(app.selected_rates(path=path, ultimate=True)['kuaishou'], 135)
        self.assertEqual(app.selected_rates(path=path, ultra=True)['kuaishou'], 135)
        with patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            app.main(['--ultimate-speed', '--ultra-speed'])

    def test_full_run_fail_fast_stops_on_incomplete_exit_too(self):
        self.assertEqual(app.supervise({'douyin':job("raise SystemExit(2)"),
                                        'kuaishou':job("import time; time.sleep(10)")},
                                       self.run,threading.Event(),max_retries=0,fail_fast=True),1)
        self.assertIn('尚有未完成任务，严格模式触发全停',self.status())
        self.assertFalse((self.root/'runtime/collector_rate_overrides.json').exists())

    def test_live_comment_pause_and_partial_exit_are_distinct_and_reminded(self):
        source = """import time
from pathlib import Path
warning = '2026-09-28 18:00:00,000 | kuaishou | WARNING | 连续3篇评论分页停滞：本轮暂停评论请求，继续搜索并保存贴文'
print(warning, flush=True)
print(warning, flush=True)
end = time.monotonic() + 5
while time.monotonic() < end:
    path = Path('logs/最新状态.txt')
    if path.exists() and '评论暂停，贴文仍在采集' in path.read_text(encoding='utf-8'):
        Path('pause-seen-while-alive').touch()
        break
    time.sleep(0.02)
else: raise SystemExit(7)
print('2026-09-28 18:01:00,000 | kuaishou | WARNING | 尚有7篇评论未完成', flush=True)
raise SystemExit(2)
"""
        # 快进心跳时间，不等待真实30秒；提示重复但不能重复弹同一故障窗口。
        with patch.object(app.time, "monotonic", side_effect=itertools.count(0, 31).__next__):
            self.assertEqual(app.supervise({"kuaishou": job(source)}, self.run, threading.Event(), max_retries=0), 2)
        self.assertTrue((self.root / "pause-seen-while-alive").exists())
        self.assertEqual(len(self.notices()), 2)
        self.assertIn("评论暂停，贴文仍在采集", self.notices()[0])
        self.assertIn("结束但未全部完成（退出码2）", self.notices()[1])
        self.assertIn("尚有7篇评论未完成", self.status())
        self.assertGreaterEqual(sys.stdout.getvalue().count("持续警报"), 3)
        self.assertEqual((self.run / "警报.txt").read_text(encoding="utf-8").count("【采集警报"), 2)

    def test_normal_text_and_manual_stop_never_raise_failure_alerts(self):
        normal = job("print('2026-09-28 18:00:00,000 | douyin | INFO | 评论候选已保存 | "
                     "{\"评论内容\":\"采集未完成：失败！连续3篇评论分页停滞：本轮暂停评论请求\"}'); "
                     "print('2026-09-28 18:00:00,000 | douyin | WARNING | 正常休息三分钟')")
        self.assertEqual(app.supervise({"douyin": normal}, self.run, threading.Event()), 0)
        self.assertEqual(self.notices(), [])
        self.assertFalse((self.run / "警报.txt").exists())
        stop = threading.Event()
        display = Mock(side_effect=lambda text, **kw: stop.set() if text == "[抖音] ready" else None)
        worker = job("""import signal,time
signal.signal(getattr(signal, 'SIGBREAK', signal.SIGINT), signal.default_int_handler)
try:
    print('ready', flush=True)
    time.sleep(15)
except KeyboardInterrupt:
    print('采集未完成：人工停止', flush=True)
    raise SystemExit(130)
""")
        with patch.object(app, "print", display, create=True):
            self.assertEqual(app.supervise({"douyin": worker}, self.run, stop), 130)
        self.assertIn("手动停止", self.status())
        self.assertEqual(self.notices(), [])
        self.assertFalse((self.run / "警报.txt").exists())

    def test_failed_spawn_and_precheck_leave_history_without_overwriting_active_status(self):
        jobs = {"douyin": [str(self.root / "no-such-python")], "kuaishou": job("print('ok')")}
        self.assertEqual(app.supervise(jobs, self.run, threading.Event()), 1)
        self.assertIn("抖音：启动失败", self.status())
        self.assertIn("快手：正常结束", self.status())
        self.assertEqual(len(self.notices()), 1)
        before = self.status()
        with patch.object(app, "check_project", side_effect=RuntimeError("缺少依赖")), \
             patch.object(app, "workspace_lock") as lock:
            self.assertEqual(app.main([]), 1)
            lock.assert_not_called()
        self.assertIn("缺少依赖", (self.root / "logs/启动失败.txt").read_text(encoding="utf-8"))
        self.assertEqual(self.status(), before)

    def test_alert_demo_is_offline_and_summary_does_not_copy_credentials(self):
        with patch.object(app, "check_project") as check, patch.object(app, "supervise") as supervise:
            self.assertEqual(app.main(["--test-alert"]), 0)
            check.assert_not_called()
            supervise.assert_not_called()
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("这不是采集故障", self.notices()[0])
        self.assertFalse((self.root / "logs/最新状态.txt").exists())
        for message in ("Cookie: private-fixture", "Authorization=private-fixture",
                        "HTTP error https://example.com/?token=private-fixture", "xsec_token='private-fixture'"):
            self.assertNotIn("private-fixture", app.alert_summary(message))
        self.assertNotIn("\x1b", app.alert_summary("报错\x1b[31m"))
        self.assertEqual(app.alert_log_line('2026 | douyin | INFO | {"评论内容":"采集失败"}'), ("", ""))

    def test_frozen_popup_uses_app_notice_entry_not_python_c(self):
        with patch.object(app.sys, 'frozen', True, create=True), \
             patch.object(app.os, 'name', 'nt'), \
             patch.object(ctypes, 'windll', Mock(), create=True), \
             patch.object(app.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
             patch.object(app.subprocess, 'Popen') as spawn:
            DESKTOP_NOTICE('标题', '提示正文')
            self.assertEqual(spawn.call_args.args[0], [sys.executable, '--show-notice', '标题', '提示正文'])

    def test_notice_entry_never_starts_collector_or_touches_status(self):
        with patch.object(ctypes, 'windll', Mock(), create=True) as api, \
             patch.object(app, 'check_project') as check, patch.object(app, 'workspace_lock') as lock:
            self.assertEqual(app.main(['--show-notice', '标题', '中文 & "正文"']), 0)
            api.user32.MessageBoxW.assert_called_once_with(None, '中文 & "正文"', '标题', 0x50030)
            check.assert_not_called()
            lock.assert_not_called()
            self.assertFalse((self.root / 'logs/最新状态.txt').exists())

    def test_native_popup_uses_separate_process_no_shell_or_wait(self):
        with patch.object(app.os, "name", "nt"), \
             patch.object(ctypes, "windll", Mock(), create=True), \
             patch.object(app.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             patch.object(app.subprocess, "Popen") as spawn:
            DESKTOP_NOTICE("故障标题", '中文\n"特殊字符" & 不得执行命令')
            args, kwargs = spawn.call_args
            self.assertEqual(args[0][-2:], ["故障标题", '中文\n"特殊字符" & 不得执行命令'])
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["creationflags"], 0x08000000)
            spawn.return_value.wait.assert_not_called()
            spawn.return_value.communicate.assert_not_called()
            spawn.side_effect = OSError("桌面提示不可用")
            DESKTOP_NOTICE("故障标题", "故障正文")  # 提示系统失效不能反过来停止采集。
            self.assertIn("系统提示不可用", sys.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
