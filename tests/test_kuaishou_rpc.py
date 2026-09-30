"""Offline only: fake CDP/page and our own bridge JS; never execute the site's SDK."""
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collectors'))
import kuaishou as ks


def make_client(payload=None, status=200, content_type='application/json', retry=None):
    client = object.__new__(ks.SearchClient)
    client.limit, client.used, client.delay, client.last_request = None, 0, 60, 0
    client._stopped = client._owns_bridge = False
    client.playwright = None
    client.page = Mock()
    def invoke(_source, args):
        path, body = args
        response = Mock(url=ks.BASE + path + '?fixture-signature=not-real', status=status,
                        request=Mock(method='POST', post_data_json=body),
                        headers={'content-type': content_type, 'retry-after': retry})
        response.header_value.side_effect = AssertionError('Do not yield inside the response observer')
        client.page.on.call_args.args[1](response)
        return json.dumps({'ok': True, 'payload': {'result': 1} if payload is None else payload})
    client.page.evaluate.side_effect = invoke
    return client


class KuaishouRPCTest(unittest.TestCase):
    def test_launcher_three_unique_ports_and_worker_forwarding(self):
        import start_all
        with patch.object(start_all, 'run_worker', return_value=0) as run:
            self.assertEqual(start_all.main(['--worker', 'kuaishou', '--ks-port', '9334']), 0)
            self.assertEqual(run.call_args.args[-1], 9334)
        for args in (['--ks-port', '9223'], ['--ks-port', '9222'], ['--ks-port', '0'],
                     ['--ks-port', '65536'], ['--dy-port', '9223']):
            with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                start_all.main([*args, '--check'])

    def test_batch_wrappers_forward_check_without_starting_collection(self):
        root = Path(__file__).resolve().parents[1]
        wrappers = [*root.glob('采集速度版本/*.bat'), *root.glob('双机分片/*.bat')]
        self.assertEqual(len(wrappers), 6)
        for path in wrappers:
            call = next(line for line in path.read_text().splitlines() if line.startswith('call '))
            self.assertIn('%*', call, path.name)

    def test_arguments_validate_before_cdp_and_no_node_or_cookie_check(self):
        with patch.object(ks, 'cdp_ready') as cdp, patch.object(ks, 'sync_playwright') as launch:
            for port in (0, 65536, '9224', True):
                with self.assertRaises(ValueError):
                    ks.SearchClient(port)
            for delay in (0, 9.9, math.nan, math.inf, True):
                with self.assertRaises(ValueError):
                    ks.SearchClient(delay=delay)
            for limit in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    ks.SearchClient(request_limit=limit)
            with patch.object(ks.shutil, 'which', return_value=None):
                ks.check_environment()
            cdp.assert_not_called()
            launch.assert_not_called()

    def test_browser_reuses_profile_and_preserves_configured_proxy(self):
        with patch.object(ks, 'cdp_ready', return_value=True), patch.object(ks.subprocess, 'Popen') as spawn:
            ks.prepare_kuaishou_browser(9224)
            spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / 'chrome.exe'
            exe.touch()
            with patch.object(ks, 'STATE_DIR', root), patch.dict(os.environ, {'CHROME_PATH': str(exe)}), \
                 patch.object(ks, 'cdp_ready', side_effect=[False, True]), \
                 patch.object(ks, 'getproxies', return_value={'https': 'http://127.0.0.1:7890'}), \
                 patch.object(ks, 'proxy_bypass', return_value=False), patch.object(ks.subprocess, 'Popen') as spawn:
                ks.prepare_kuaishou_browser(9224)
                args = spawn.call_args.args[0]
                self.assertIn('--user-data-dir=' + str(root / 'browser'), args)
                self.assertIn('--remote-debugging-port=9224', args)
                self.assertIn('--remote-debugging-address=127.0.0.1', args)
                self.assertIn('--proxy-server=http://127.0.0.1:7890', args)
                self.assertEqual(args[-1], 'about:blank')
                self.assertFalse((root / 'cookie.txt').exists())

    def test_cdp_rejects_uncertain_or_nonlocal_listener(self):
        with patch.object(ks, 'build_opener') as opener:
            opener.return_value.open.side_effect = URLError(ConnectionRefusedError())
            self.assertFalse(ks.cdp_ready(9224))
            opener.return_value.open.side_effect = URLError(TimeoutError())
            with self.assertRaises(RuntimeError):
                ks.cdp_ready(9224)
            opener.return_value.open.side_effect = None
            for address in ('ws://outside.example:9224/devtools/a', 'ws://user:secret@127.0.0.1:9224/a',
                            'ws://127.0.0.1:9223/a', 'invalid'):
                opener.return_value.open.return_value = io.BytesIO(json.dumps({'webSocketDebuggerUrl': address}).encode())
                with self.assertRaises(RuntimeError):
                    ks.cdp_ready(9224)

    def test_attach_selects_only_native_client_page_and_never_exports_cookies(self):
        page = Mock(url=ks.BASE + '/new-reco')
        page.evaluate.side_effect = [True, {'ready': True, 'version': 1}, None]
        other = Mock(url=ks.BASE + '/')
        other.evaluate.return_value = False
        context = Mock(pages=[other, page])
        browser = Mock(contexts=[context])
        driver = Mock()
        driver.chromium.connect_over_cdp.return_value = browser
        with patch.object(ks, 'cdp_ready', return_value=True), patch.object(ks, 'sync_playwright') as playwright:
            playwright.return_value.start.return_value = driver
            client = ks.SearchClient()
            self.assertIs(client.page, page)
            self.assertFalse(page.evaluate.call_args.args[1]['exclusive'])
            client.close()
            client.close()
            driver.stop.assert_called_once()
            browser.close.assert_not_called()
            page.goto.assert_not_called()
            context.new_page.assert_not_called()
            context.cookies.assert_not_called()
            context.add_cookies.assert_not_called()

    def test_attach_does_not_choose_between_two_ready_tabs(self):
        pages = [Mock(url=ks.BASE + '/new-reco'), Mock(url=ks.BASE + '/search/fixture')]
        for page in pages:
            page.evaluate.return_value = True
        driver = Mock()
        driver.chromium.connect_over_cdp.return_value = Mock(contexts=[Mock(pages=pages)])
        with patch.object(ks, 'cdp_ready', return_value=True), patch.object(ks, 'sync_playwright') as playwright:
            playwright.return_value.start.return_value = driver
            with self.assertRaisesRegex(RuntimeError, '标签页不唯一'):
                ks.SearchClient()
            driver.stop.assert_called_once()
            self.assertTrue(all(page.evaluate.call_count == 1 for page in pages))

    def test_saved_cursor_is_forwarded_exactly_without_navigation(self):
        client = make_client()
        with patch.object(ks.time, 'sleep'):
            client.comments('existing', 'saved-cursor', 7)
        self.assertEqual(client.page.evaluate.call_args.args[1], [ks.COMMENTS,
            {'photoId': 'existing', 'page': '7', 'pcursor': 'saved-cursor', 'type': 'STATIC'}])
        client.page.goto.assert_not_called()
        self.assertEqual(client.page.remove_listener.call_count, 1)
        self.assertFalse(hasattr(client, 'http'))
        self.assertFalse(hasattr(client, 'sign'))

    def test_no_egress_without_allowlist_budget_or_after_failure(self):
        client = make_client()
        with self.assertRaises(ValueError):
            client.post('/rest/v/photo/like', {})
        with self.assertRaises(ValueError):
            client.post(ks.SEARCH, {'keyword': float('nan')})
        client.limit, client.used = 1, 1
        with self.assertRaises(ks.BudgetExhausted):
            client.comments('fixture')
        client.page.evaluate.assert_not_called()
        client.limit = None
        client.page.evaluate.side_effect = ks.PlaywrightError('signed?token=secret')
        with patch.object(ks.time, 'sleep'):
            with self.assertRaises(RuntimeError) as caught:
                client.comments('fixture')
            self.assertNotIn('secret', str(caught.exception))
            with self.assertRaisesRegex(RuntimeError, '本轮已停止'):
                client.comments('fixture')
        self.assertEqual(client.used, 2)
        self.assertEqual(client.page.evaluate.call_count, 1)

    def test_wire_status_type_and_native_result_must_all_agree(self):
        for status, content_type, payload in ((403, 'application/json', {'result': 1}),
                (200, 'text/html', {'result': 1}), (200, 'application/json', {'result': True}),
                (200, 'application/json', {}), (200, 'application/json', {'result': 2056})):
            client = make_client(payload, status, content_type)
            with self.subTest(status=status, payload=payload), patch.object(ks.time, 'sleep'):
                with self.assertRaises(RuntimeError):
                    client.comments('fixture')
                self.assertTrue(client._stopped)
        client = make_client()
        client.page.evaluate.side_effect = None
        client.page.evaluate.return_value = json.dumps({'ok': True, 'payload': {'result': 1}})
        with patch.object(ks.time, 'sleep'), self.assertRaisesRegex(RuntimeError, '不是唯一'):
            client.comments('fixture')  # Native cached/synthetic success without wire evidence is rejected.

    @unittest.skipUnless(shutil.which('node'), 'Node only for our offline bridge fixture')
    def test_native_bridge_contract_in_fake_page(self):
        fixture = r'''
        const vm=require('node:vm'), assert=require('node:assert/strict');
        const source=require('node:fs').readFileSync(0,'utf8');
        const timers=[], calls=[];
        let native=async config => {calls.push(config); return {result:1,feeds:[],pcursor:'next'}};
        const rest={type:'REST', mutate:config=>native(config)};
        const app={__vue_app__:{config:{globalProperties:{rebornClient:{rest}}}}};
        const sandbox={location:{origin:'https://www.kuaishou.com'},
          document:{cookie:'userId=fixture; kwssectoken=not-real', title:'',
            querySelector:()=>app, querySelectorAll:()=>[]},
          setTimeout:fn=>{timers.push(fn); return fn},clearTimeout:()=>{},TextEncoder,window:{}};
        const install=vm.runInNewContext(source,sandbox), payload={keyword:'fixture',pcursor:'old'};
        const search='/rest/v/search/feed';
        (async()=>{
          assert.equal(install({exclusive:false}).ready,true);
          let bridge=sandbox.window.__ksCollectorRPC;
          assert.equal((await bridge.call('/not-allowed',{})).reason,'invalid_request');
          assert.equal(calls.length,0);
          assert.equal((await bridge.call(search,payload)).payload.pcursor,'next');
          assert.equal(calls.length,1); assert.equal(calls[0].variables,payload);
          assert.equal(calls[0].method,'POST'); assert.equal(calls[0].timeout,25000);
          assert.equal(install({exclusive:false}).ready,false);
          assert.equal(install({exclusive:true}).ready,true);
          bridge=sandbox.window.__ksCollectorRPC;
          let resolve;
          native=()=>new Promise(r=>{resolve=r});
          const pending=bridge.call(search,payload);
          assert.equal((await bridge.call(search,payload)).reason,'bridge_stopped_or_busy');
          assert.equal(install({exclusive:true}).ready,false);
          resolve({result:1}); assert.equal((await pending).ok,true);
          native=async()=>({result:2});
          await bridge.call(search,payload);
          assert.equal(bridge.stopped,true); assert.equal(bridge.uncertain,false);
          assert.equal(install({exclusive:true}).ready,true);
          bridge=sandbox.window.__ksCollectorRPC;
          native=()=>new Promise(()=>{});
          const timeout=bridge.call(search,payload); timers.at(-1)();
          assert.equal((await timeout).ok,false);
          assert.equal(bridge.uncertain,true);
          assert.equal(install({exclusive:true}).ready,false);
          delete sandbox.window.__ksCollectorRPC; install({exclusive:true});
          bridge=sandbox.window.__ksCollectorRPC;
          native=async()=>{throw {config:{cookie:'secret-fixture'},message:'token=secret-fixture'}};
          assert.ok(!JSON.stringify(await bridge.call(search,payload)).includes('secret-fixture'));
          delete sandbox.window.__ksCollectorRPC; install({exclusive:true});
          bridge=sandbox.window.__ksCollectorRPC; sandbox.document.cookie='did=anonymous';
          assert.equal((await bridge.call(search,payload)).reason,'need_login');
          delete sandbox.window.__ksCollectorRPC; install({exclusive:true});
          bridge=sandbox.window.__ksCollectorRPC; sandbox.location.origin='https://outside.example';
          assert.equal((await bridge.call(search,payload)).reason,'page_changed');
          console.log('offline bridge passed');
        })().catch(e=>{console.error(e);process.exitCode=1});
        '''
        result = subprocess.run([shutil.which('node'), '-e', fixture], input=ks.BRIDGE_JS,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('offline bridge passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
