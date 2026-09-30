"""仅假时钟验证限速，不联网、不真实等待；不能当作平台稳定性证明。"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collectors'))
import douyin as dy
import kuaishou as ks
import xiaohongshu as xhs
from test_kuaishou_rpc import make_client as make_ks_rpc


class FrequencyTest(unittest.TestCase):
    def test_history_metrics_are_commit_only_and_separate_attempts(self):
        module_path = Path(__file__).resolve().parents[1] / 'js_reverse_cache/summarize_sampling_runs.py'
        spec = importlib.util.spec_from_file_location('sampling_metrics_fixture', module_path)
        metrics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metrics)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'logs/runs/fixture'
            run.mkdir(parents=True)
            (root/'js_reverse_cache').mkdir()
            (run/'kuaishou.log').write_text(
                '2026-09-28 10:00:00,000 | kuaishou | INFO | 评论页已提交 | {}\n'
                '2026-09-28 10:00:10,000 | kuaishou | INFO | 评论页已提交 | {}\n'
                '2026-09-28 10:00:30,000 | kuaishou | INFO | 评论页已提交 | {}\n'
                '2026-09-28 10:00:31,000 | kuaishou | INFO | 评论候选已保存 | {"评论内容":"Cookie: fixture-private 采集未完成"}\n'
                '采集未完成：快手请求频率受限\n'
                '【自动续采 1/3】\n'
                '2026-09-28 11:00:00,000 | kuaishou | INFO | 评论页已提交 | {}\n', encoding='utf-8')
            with patch.object(metrics,'ROOT',root), patch.object(sys,'stdout',io.StringIO()):
                metrics.main()
            output=(root/'js_reverse_cache/sampling_history_metrics.json').read_text(encoding='utf-8')
            a,b=json.loads(output)['runs']
            self.assertEqual((a['committed_comment_pages'],a['commit_interval_p50_s'],a['commit_interval_p95_s']), (3,15,20))
            self.assertEqual(a['terminal_error_category'],'explicit_rate_limit')
            self.assertEqual((b['attempt'],b['committed_comment_pages']), (1,1))
            self.assertIsNone(b['commit_interval_p50_s'])
            self.assertNotIn('fixture-private',output)

    def test_ks_trial_stops_on_any_rejection_without_leaking_message(self):
        path = Path(__file__).resolve().parents[1] / 'js_reverse_cache/ks_stability_round.py'
        spec = importlib.util.spec_from_file_location('ks_stability_fixture', path)
        trial = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(trial)
        self.assertEqual(trial.classify('接口拒绝 result=2；操作太快了，请稍微休息一下'), 'frequency_limit')
        self.assertEqual(trial.classify('接口拒绝 result=2；服务端说明：未提供'), 'business_result_2_unknown')
        self.assertEqual(trial.classify('HTTP 403'), 'login_or_access')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            order = root/'order.json'
            order.write_text(json.dumps({'budget':{'remaining':1,'batchConsumed':0,'batchLimit':1}}),encoding='utf-8')
            with patch.object(trial, 'ORDER', order):
                trial.reserve()
                self.assertEqual(json.loads(order.read_text(encoding='utf-8'))['budget']['remaining'],0)
                with self.assertRaisesRegex(RuntimeError,'额度已用完'):
                    trial.reserve()

    def test_douyin_trial_budget_stops_at_auto_observation_limit(self):
        path=Path(__file__).resolve().parents[1]/'js_reverse_cache/douyin_stability_round.py'
        spec=importlib.util.spec_from_file_location('dy_trial_fixture',path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            order=Path(directory)/'order.json'
            order.write_text(json.dumps({'budget':{'remaining':2,'batchConsumed':0,'batchLimit':2,
                'automaticObserved':670,'automaticStopThreshold':1000}}),encoding='utf-8')
            with patch.object(module,'ORDER',order):
                self.assertTrue(module.update(auto=100,reserve=True))
                self.assertEqual(json.loads(order.read_text(encoding='utf-8'))['budget']['remaining'],1)
                self.assertFalse(module.update(auto=230))
                with self.assertRaisesRegex(RuntimeError,'观察上限'):
                    module.update(reserve=True)
                self.assertEqual(json.loads(order.read_text(encoding='utf-8'))['budget']['remaining'],1)

    def test_xhs_trial_budget_and_classification_are_fail_closed(self):
        path=Path(__file__).resolve().parents[1]/'js_reverse_cache/xhs_stability_round.py'
        spec=importlib.util.spec_from_file_location('xhs_trial_fixture',path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.classify(RuntimeError('HTTP 461 访问被限制')),'access_or_frequency_limit')
        self.assertEqual(module.classify(RuntimeError('需要人工登录')),'manual_login_or_verification')
        with tempfile.TemporaryDirectory() as directory:
            order=Path(directory)/'order.json'
            order.write_text(json.dumps({'allowedScope':['https://www.xiaohongshu.com/api/sns/web/v1/feed'],
                'budget':{'remaining':1,'batchConsumed':0,'batchLimit':1,'automaticObserved':999,
                          'automaticStopThreshold':1000}}),encoding='utf-8')
            with patch.object(module,'ORDER',order):
                self.assertFalse(module.update(auto=1))
                with self.assertRaisesRegex(RuntimeError,'观察上限'):
                    module.update(reserve=True)
                self.assertEqual(json.loads(order.read_text(encoding='utf-8'))['budget']['remaining'],1)

    def test_bat_test_budget_is_forwarded_per_platform(self):
        import start_all
        self.assertEqual(start_all.arguments('douyin',9222,9223,3)[-2:],['--test-requests','3'])
        self.assertEqual(start_all.arguments('kuaishou',9222,9223,3),['--rpc-port','9224','--max-requests','3'])
        self.assertEqual(start_all.arguments('xiaohongshu',9222,9223,3)[-2:],['--test-requests','3'])
        with self.assertRaises(SystemExit):
            start_all.main(['--test-requests','3','--only','douyin','--retries','1'])

    def test_bounded_real_collector_requests_stop_before_extra_egress(self):
        dy_client=object.__new__(dy.BrowserRPC)
        dy_client.test_request_limit,dy_client.test_requests_used=2,0
        dy_client._pace=Mock()
        dy_client.page=Mock()
        dy_client.page.evaluate.return_value={'http':200,'type':'application/json','body':{'status_code':0}}
        for _ in range(2):
            dy_client._request('/aweme/v1/web/comment/list/',{})
        with self.assertRaises(dy.TestBudgetExhausted):
            dy_client._request('/aweme/v1/web/comment/list/',{})
        self.assertEqual(dy_client.page.evaluate.call_count,2)

        async def limited_xhs():
            client=xhs.AsyncXHSClient()
            client.test_request_limit=2
            client._rate_limit=AsyncMock()
            client._run=AsyncMock(return_value='ok')
            for _ in range(2):
                self.assertEqual(await client._action(lambda:None),'ok')
            with self.assertRaises(xhs.TestBudgetExhausted):
                await client._action(lambda:None)
            self.assertEqual(client._run.await_count,2)
        asyncio.run(limited_xhs())

        client=make_ks_rpc()
        client.limit,client.used=2,2
        with self.assertRaises(ks.BudgetExhausted):
            client.comments('known')
        client.page.evaluate.assert_not_called()

    def test_douyin_spacing_and_periodic_rest(self):
        self.assertEqual(dy.BrowserRPC.__init__.__defaults__[-1],30)
        for minimum in (10, 30, 230, 400):
            with self.subTest(minimum=minimum):
                clock, sleeps, requests = [10000.0], [], []
                delays = [minimum + (i % 3) * 7.5 for i in range(32)]
                def sleep(seconds):
                    sleeps.append(seconds)
                    clock[0] += seconds
                client = object.__new__(dy.BrowserRPC)
                client.interval, client.last_action, client._actions_since_rest = minimum, 0, 0
                with patch.object(dy.time, 'monotonic', side_effect=lambda: clock[0]), \
                     patch.object(dy.time, 'sleep', side_effect=sleep), \
                     patch.object(dy.random, 'uniform', side_effect=delays) as draw:
                    for _ in range(32):
                        client._pace()
                        requests.append(clock[0])
                self.assertEqual(requests[0], 10000)
                for i in range(1, 32):
                    self.assertEqual(requests[i] - requests[i-1], max(300, delays[i]) if i == 30 else delays[i])
                self.assertEqual(sleeps.count(300),1)
                self.assertTrue(all(call.args == (minimum, minimum + 15) for call in draw.call_args_list))

    def test_douyin_random_pacing_applies_to_search_detail_and_comments(self):
        clock = [10000.0]
        client = object.__new__(dy.BrowserRPC)
        client.interval, client.last_action, client._actions_since_rest = 10, 0, 0
        client.page = Mock()
        calls = []
        def reply(script, params):
            calls.append((params['path'], clock[0]))
            return {'http':200, 'type':'application/json', 'body':{'status_code':0}}
        client.page.evaluate.side_effect = reply
        with patch.object(dy.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(dy.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(dy.random, 'uniform', side_effect=[10, 25, 17.5, 10]):
            client._search_page('fixture', 40)
            clock[0] += 3  # Work/network time counts toward the start-to-start interval.
            client._detail('fixture')
            client._comments('fixture', 20)
            clock[0] += 40  # A long operation must not incur the entire delay again.
            client._comments('fixture', 40)
        self.assertEqual([time for path, time in calls], [10000, 10025, 10042.5, 10082.5])
        self.assertEqual(len({path for path, time in calls}), 3)

    def test_kuaishou_spacing_rest_and_native_call_time(self):
        clock, sleeps, calls = [10000.0], [], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        client = make_ks_rpc()
        client.used, client.last_request = 30, clock[0]
        invoke = client.page.evaluate.side_effect
        client.page.evaluate.side_effect = lambda *args: calls.append(clock[0]) or invoke(*args)
        with patch.object(ks.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(ks.time, 'sleep', side_effect=sleep):
            client.comments('fixture')
            client.comments('fixture','next',2)
        self.assertEqual(calls,[10300.0,10360.0])
        self.assertEqual(sleeps.count(300),1)
        self.assertEqual(client.used,32)

    def test_xhs_ultra_delay_is_explicit_and_bounded(self):
        with patch.object(sys, 'argv', ['xiaohongshu.py', '--min-delay', '10']), \
             patch.object(xhs, 'cmd_all_notes', new_callable=AsyncMock, return_value=0) as run:
            self.assertEqual(xhs.main(), 0)
            self.assertEqual(run.call_args.args[0].min_delay, 10)
        with patch.object(sys, 'argv', ['xiaohongshu.py', '--min-delay', '9.9', '--check']), \
             patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            xhs.main()

    def test_xhs_spacing_and_periodic_rest(self):
        async def run():
            client = xhs.AsyncXHSClient()
            self.assertEqual(client._request_delay,(60.0,75.0))
            clock, sleeps, requests = [10000.0], [], []
            async def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds
            client._run = AsyncMock(side_effect=lambda *args: requests.append(clock[0]))
            with patch.object(xhs.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(xhs.random,'uniform',return_value=60), \
                 patch.object(xhs.asyncio,'sleep',side_effect=sleep):
                for _ in range(62):
                    await client._action(lambda: None)
            for i in range(1, 62):
                self.assertEqual(requests[i]-requests[i-1], 300 if i in (30, 60) else 60)
            self.assertEqual(sleeps.count(300),2)
            self.assertEqual(client._actions_since_rest,2)
            self.assertEqual(client.test_requests_used,62)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
