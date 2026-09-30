"""旧贴文评论补采：全部离线，不修改正式CSV、断点或浏览器。"""
import asyncio
import csv
import importlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def comment(ident):
    return {"评论ID": ident, "评论内容": "旧贴文补到的评论", "评论点赞数": 9,
            "评论时间": "2023-01-01", "评论者昵称": "测试"}


class CommentBackfillTest(unittest.TestCase):
    def test_round_robin_keeps_saved_cursor_and_updates_csv_each_page(self):
        for name, platform in (("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书")):
            m = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "posts.csv"
                with closing(m.CommentStore(path, platform)) as store:
                    for ident in ("deep", "untouched", "complete"):
                        store.register(ident)
                    store.save_page("deep", [comment("old")], "saved-cursor", False)
                    store.save_page("complete", [], "", True)
                    calls = []
                    for ident in m.comment_jobs(store, ["deep", "untouched", "complete"], page_limit=2):
                        state = store.state(ident)
                        calls.append((ident, state["cursor"]))
                        page = state["pages"] + 1
                        store.save_page(ident, [comment(f"{ident}-{page}")], f"next-{page}", False)
                        self.assertIn(f"{ident}-{page}", {r["评论ID"] for r in rows(store.output)})
                    self.assertEqual(calls, [("untouched", ""), ("deep", "saved-cursor"),
                                             ("untouched", "next-1"), ("deep", "next-2")])
                    self.assertFalse(store.state("deep")["done"])  # 调度分片/页数上限不能冒充完成。
                    self.assertEqual(store.state("deep")["cursor"], "next-3")
                    # CSV被占用时，已提交的候选和游标仍保留；恢复导出无需重新请求页面。
                    with patch.object(m, "export_csv", side_effect=RuntimeError("CSV 被占用")):
                        with self.assertRaisesRegex(RuntimeError, "CSV 被占用"):
                            store.save_page("deep", [comment("saved-before-export-error")], "next-4", True)
                    self.assertTrue(store.state("deep")["done"])
                    self.assertEqual(store.state("deep")["cursor"], "next-4")
                    store.export()
                    self.assertIn("saved-before-export-error", {r["评论ID"] for r in rows(store.output)})

    def test_douyin_existing_csv_without_comment_store_collects_all_old_ids(self):
        import douyin as d
        with tempfile.TemporaryDirectory() as directory, patch.object(d, "OUTPUT", Path(directory) / "csv"):
            path = d.OUTPUT / "douyin.csv"
            d.export_csv(path, d.COLUMNS, [d.canonical_post("抖音", {
                "内容ID": ident, "标题": ident, "发布时间": "2023-01-01"}) for ident in ("a", "b")])
            calls = []
            def fetch(project, args, cookie):
                self.assertEqual(project, "comment_list")  # 不搜旧贴文、不请求旧详情。
                ident = args[args.index("--aweme-id") + 1]
                cursor = args[args.index("--cursor") + 1]
                calls.append((ident, cursor))
                if len(calls) > 1:
                    self.assertTrue(rows(path.with_name("douyin_高赞评论.csv")))
                return {"status_code": 0, "has_more": int(cursor == "0"), "cursor": 1 if cursor == "0" else 0,
                        "comments": [{"cid": ident + cursor, "text": "补采", "digg_count": 9,
                                      "create_time": "2023-01-01", "user": {"nickname": "作者"}}]}
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual(calls, [("a", "0"), ("b", "0"), ("a", "1"), ("b", "1")])
            self.assertEqual(len(rows(path.with_name("douyin_高赞评论.csv"))), 4)
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual(len(calls), 4)
            self.assertEqual([r["内容ID"] for r in rows(path)], ["a", "b"])

    def test_kuaishou_legacy_enriched_and_finished_search_still_backfill_comments(self):
        import kuaishou as k
        with tempfile.TemporaryDirectory() as directory:
            path, db_path = Path(directory) / "posts.csv", Path(directory) / "search.sqlite3"
            with closing(k.SearchState(db_path, path, k.START, k.END)) as state:
                for ident in ("a", "b"):
                    state.put(ident, k.canonical_post("快手", {"内容ID": ident, "发布时间": "2023-01-01"}), True)
                state.begin(["双减"], False)
                state.checkpoint("双减", "", "old-session", 3, [], True)
                state.sync_csv()
            client = Mock()
            calls = []
            def comments(ident, cursor, page):
                calls.append((ident, cursor, page))
                if len(calls) > 1:
                    self.assertTrue(rows(path.with_name("posts_高赞评论.csv")))
                return {"result": 1, "pcursorV2": "saved-next" if page == 1 else "no_more", "commentCountV2": 2,
                        "rootCommentsV2": [{"comment_id": f"{ident}{page}", "content": "补采",
                                            "likeCount": 1, "timestamp": "2023-01-01", "author_name": "作者"}]}
            client.comments.side_effect = comments
            self.assertEqual(k.export_search(client, ["双减"], path, k.START, k.END, state_path=db_path), 0)
            self.assertEqual(calls, [("a", "", 1), ("b", "", 1), ("a", "saved-next", 2), ("b", "saved-next", 2)])
            client.search.assert_not_called()
            self.assertEqual(len(rows(path.with_name("posts_高赞评论.csv"))), 4)
            client.reset_mock()
            k.export_search(client, ["双减"], path, k.START, k.END, state_path=db_path)
            client.comments.assert_not_called()
            client.search.assert_not_called()

    def test_douyin_confirmed_null_zero_comments_does_not_stop_other_posts(self):
        import douyin as d
        with tempfile.TemporaryDirectory() as directory, patch.object(d, "OUTPUT", Path(directory) / "csv"):
            path = d.OUTPUT / "douyin.csv"
            d.export_csv(path, d.COLUMNS, [d.canonical_post("抖音", {
                "内容ID": ident, "发布时间": "2023-01-01"}) for ident in ("empty", "good")])
            fetch = Mock(side_effect=[
                {"status_code": 0, "comments": None, "has_more": 0, "total": 0, "cursor": 10},
                {"status_code": 0, "comments": [{"cid": "c", "text": "已保存", "digg_count": 8,
                                                "create_time": "2023-01-01"}],
                 "has_more": 0, "total": 1, "cursor": 10}])
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual([r["评论ID"] for r in rows(path.with_name("douyin_高赞评论.csv"))], ["c"])
            with closing(d.CommentStore(path, "抖音")) as store:
                self.assertTrue(store.state("empty")["done"])
                self.assertEqual(store.state("empty")["total"], 0)
                self.assertEqual(store.top("empty"), [])
                self.assertTrue(store.state("good")["done"])
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual(fetch.call_count, 2)

    def test_douyin_unknown_or_rejected_null_response_keeps_cursor(self):
        import douyin as d
        normal = {"status_code": 0, "comments": None, "has_more": 0, "total": 0, "cursor": 10}
        malformed = [{**normal, **update} for update in (
            {"total": None}, {"total": 5}, {"has_more": 1}, {"has_more": "0"},
            {"has_more": None}, {"status_code": 109}, {"status_code": False})]
        malformed += [{k: v for k, v in normal.items() if k != "comments"}, []]
        with tempfile.TemporaryDirectory() as directory:
            for body in malformed:
                with self.subTest(body=body), closing(d.CommentStore(Path(directory) / "posts.csv", "抖音", memory=True)) as store:
                    store.register("post")
                    with self.assertRaises(RuntimeError):
                        d.collect_comments(Mock(return_value=body), None, store, "post")
                    self.assertEqual(store.state("post")["cursor"], "")
                    self.assertEqual(store.state("post")["pages"], 0)
                    self.assertFalse(store.state("post")["done"])

    def test_douyin_unavailable_item_does_not_block_other_posts_or_lose_cursor(self):
        import douyin as d
        unavailable = {"status_code": 0, "comments": None, "has_more": 0}
        with tempfile.TemporaryDirectory() as directory, patch.object(d, 'OUTPUT', Path(directory) / 'csv'):
            path = d.OUTPUT / 'douyin.csv'
            d.export_csv(path, d.COLUMNS, [d.canonical_post('抖音', {
                '内容ID': ident, '发布时间': '2023-01-01'}) for ident in ('private', 'good')])
            with closing(d.CommentStore(path, '抖音')) as store:
                store.register('private')
                store.save_page('private', [comment('old')], 'saved-page', False, 10)
            calls = []
            def fetch(project, args, cookie):
                self.assertEqual(project, 'comment_list')  # 不用补采旧详情来绕过权限问题。
                ident, cursor = args[1], args[3]
                calls.append((ident, cursor))
                if ident == 'private': return unavailable
                return {'status_code': 0, 'comments': [{'cid':'good-comment','text':'正常评论',
                    'digg_count':3, 'create_time':'2023-01-01'}], 'has_more':0, 'cursor':10, 'total':1}
            attempted = set()
            d._sync_comments(fetch, None, d.START, d.END, None, attempted=attempted)
            with closing(d.CommentStore(path, '抖音')) as store:
                state = store.state('private')
                self.assertEqual((state['cursor'], state['pages'], state['total'], state['done']), ('saved-page',1,10,0))
                self.assertIn('评论暂不可用', state['status'])
                self.assertTrue(store.state('good')['done'])
            self.assertEqual({r['评论ID'] for r in rows(path.with_name('douyin_高赞评论.csv'))}, {'old','good-comment'})
            self.assertEqual(calls, [('good','0'), ('private','saved-page')])
            d._sync_comments(fetch, None, d.START, d.END, None, attempted=attempted)
            self.assertEqual(len(calls), 2)  # 同轮后续关键词不再次尝试这篇。
            report = json.loads(d.sidecar_path(path.with_name('douyin_覆盖报告.json'), 'reports').read_text(encoding='utf-8'))
            self.assertEqual(report['未完成评论篇数'], 1)
            # 新一轮仍可按原游标补采，不永久跳过、也不伪造完成。
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual(calls[-1], ('private','saved-page'))

    def test_douyin_positive_total_without_list_is_isolated_and_retried_later(self):
        import douyin as d
        with tempfile.TemporaryDirectory() as directory, patch.object(d, 'OUTPUT', Path(directory) / 'csv'):
            path = d.OUTPUT / 'douyin.csv'
            ids = ('new-unavailable', 'good', 'old-unavailable-1', 'old-unavailable-2')
            d.export_csv(path, d.COLUMNS, [d.canonical_post('抖音', {
                '内容ID': ident, '发布时间': '2023-01-01'}) for ident in ids])
            with closing(d.CommentStore(path, '抖音')) as store:
                for ident in ids[2:]:
                    store.register(ident)
                    store.mark(ident, '评论暂不可用，保留原游标待补')
            calls = []
            def fetch(project, args, cookie):
                ident = args[1]
                calls.append(ident)
                if ident == 'new-unavailable':
                    return {'status_code': 0, 'comments': None, 'has_more': 0, 'total': 1}
                if ident.startswith('old-'):
                    return {'status_code': 0, 'comments': None, 'has_more': 0}
                return {'status_code': 0, 'comments': [{'cid': 'saved', 'text': '可见',
                         'digg_count': 1, 'create_time': '2023-01-01'}],
                        'has_more': 0, 'total': 1, 'cursor': 10}
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertEqual(calls, ['new-unavailable', 'good', *ids[2:]])
            with closing(d.CommentStore(path, '抖音')) as store:
                for ident in (ids[0], *ids[2:]):
                    state = store.state(ident)
                    self.assertEqual((state['cursor'], state['pages'], state['done']), ('', 0, 0))
                    self.assertTrue(state['status'].startswith('评论暂不可用'))
                self.assertTrue(store.state('good')['done'])
            d._sync_comments(fetch, None, d.START, d.END, None)
            self.assertCountEqual(calls[4:], [ids[0], *ids[2:]])  # 仍待补，但已知异常不会每轮熔断。
            report = json.loads(d.sidecar_path(path.with_name(path.stem + '_覆盖报告.json'), 'reports').read_text(encoding='utf-8'))
            self.assertEqual(report['未完成评论篇数'], 3)

    def test_douyin_widespread_degraded_responses_still_stop(self):
        import douyin as d
        for body in ({'status_code':0,'comments':None,'has_more':0},
                     {'status_code':0,'comments':None,'has_more':0,'total':1}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory, \
                 patch.object(d, 'OUTPUT', Path(directory) / 'csv'):
                path = d.OUTPUT / 'douyin.csv'
                d.export_csv(path, d.COLUMNS, [d.canonical_post('抖音', {
                    '内容ID': ident, '发布时间': '2023-01-01'}) for ident in ('a','b','c','d')])
                fetch = Mock(return_value=body)
                with self.assertRaisesRegex(RuntimeError, '连续3篇'):
                    d._sync_comments(fetch, None, d.START, d.END, None)
                self.assertEqual(fetch.call_count, 3)
                with closing(d.CommentStore(path, '抖音')) as store:
                    for ident in ('a','b','c','d'):
                        state=store.state(ident)
                        self.assertEqual((state['pages'],state['cursor'],state['done']), (0,'',0))
                fetch = Mock(side_effect=RuntimeError('NEED_LOGIN/人机验证'))
                with self.assertRaisesRegex(RuntimeError, 'NEED_LOGIN'):
                    d._sync_comments(fetch, None, d.START, d.END, None)
                self.assertEqual(fetch.call_count, 1)  # 登录/验证不是单篇不可用，不能继续其它请求。

    def test_kuaishou_comment_circuit_keeps_search_streaming_and_resumes_later(self):
        import kuaishou as k
        with tempfile.TemporaryDirectory() as directory:
            path, db_path = Path(directory) / "posts.csv", Path(directory) / "search.sqlite3"
            with closing(k.SearchState(db_path, path, k.START, k.END)) as state, \
                 closing(k.CommentStore(path, "快手")) as store:
                for ident in ("a", "b", "c"):
                    state.put(ident, k.canonical_post("快手", {"内容ID": ident, "发布时间": "2023-01-01"}), False)
                    store.register(ident)
                    store.save_page(ident, [comment(ident)], "saved-" + ident, False)
                state.begin(["已完成", "未完成"], False)
                state.checkpoint("已完成", "", "session", 1, [], True)
                state.checkpoint("未完成", "saved-search", "session", 2, ["saved-search"], False)
                state.sync_csv()
            client = Mock()
            client.comments.return_value = {"result": 1, "rootCommentsV2": [], "pcursorV2": "advancing-but-empty", "commentCountV2": 9960}
            def search(keyword, cursor, session):
                self.assertEqual(keyword, "未完成")
                self.assertEqual(session, "session")
                # 在下一网络页执行前，上一页贴文已经写进数据库和CSV。
                if cursor == "next-search":
                    self.assertIn("new-1", {r["内容ID"] for r in rows(path)})
                    with closing(k.SearchState(db_path, path, k.START, k.END)) as state:
                        self.assertIsNotNone(state.get("new-1"))
                        self.assertEqual(state.progress(keyword)[0], cursor)
                else:
                    self.assertEqual(cursor, "saved-search")
                ident = "new-1" if cursor == "saved-search" else "new-2"
                return {"result": 1, "pcursor": "next-search" if ident == "new-1" else "no_more",
                        "feeds": [{"photo": {"id": ident, "timestamp": 1672531200000, "caption": "双减"}}]}
            client.search.side_effect = search
            self.assertEqual(k.export_search(client, ["已完成", "未完成"], path, k.START, k.END, state_path=db_path), 2)
            self.assertEqual([call.args for call in client.comments.call_args_list],
                             [(ident, "saved-" + ident, 2) for ident in ("a", "b", "c")])
            self.assertEqual(client.search.call_count, 2)
            self.assertEqual(len(rows(path)), 5)
            self.assertEqual([r["评论数"] for r in rows(path)[:3]], ["9960"] * 3)
            with closing(k.CommentStore(path, "快手")) as store:
                for ident in ("a", "b", "c"):
                    self.assertEqual(store.state(ident)["cursor"], "saved-" + ident)
                    self.assertEqual(store.state(ident)["pages"], 1)
                    self.assertEqual(store.state(ident)["total"], 9960)
                    self.assertFalse(store.state(ident)["done"])
                for ident in ("new-1", "new-2"):
                    self.assertFalse(store.state(ident)["done"])
                    self.assertEqual(store.state(ident)["pages"], 0)
            report = json.loads(k.sidecar_path(path.with_name("posts_覆盖报告.json"), "reports").read_text(encoding="utf-8"))
            self.assertEqual(report["未完成评论篇数"], 5)
            # 本轮熔断不永久关闭评论；新一轮从旧游标补采，不重搜已提交页。
            client.reset_mock()
            client.comments.return_value = {"result": 1, "rootCommentsV2": [], "pcursorV2": "no_more", "commentCountV2": 0}
            k.export_search(client, ["已完成", "未完成"], path, k.START, k.END, state_path=db_path)
            client.search.assert_not_called()
            self.assertEqual(set(call.args for call in client.comments.call_args_list),
                             {(ident, "saved-" + ident, 2) for ident in ("a", "b", "c")} |
                             {(ident, "", 1) for ident in ("new-1", "new-2")})

    def test_douyin_batch_does_not_retry_unavailable_comments_for_each_keyword(self):
        import douyin as d
        with tempfile.TemporaryDirectory() as directory, patch.object(d, 'OUTPUT', Path(directory) / 'csv'):
            path = d.OUTPUT / 'douyin.csv'
            d.export_csv(path, d.COLUMNS, [d.canonical_post('抖音', {
                '内容ID': 'old', '发布时间': '2023-01-01'})])
            fetch = Mock(return_value={'status_code': 0, 'comments': None, 'has_more': 0, 'total': 1})
            with patch.object(d, '_collect_posts', return_value=(path, 1)):
                d._collect_browser_batch(['first', 'second'], fetch, d.START, d.END)
            self.assertEqual(fetch.call_count, 1)
            with closing(d.CommentStore(path, '抖音')) as store:
                state = store.state('old')
                self.assertEqual((state['pages'], state['cursor'], state['done']), (0, '', 0))

    def test_douyin_blocked_search_creates_missing_checkpoint_but_never_resets_legacy_cursor(self):
        import douyin as d
        for previous in (None, {'offset': 60, 'page': 4, 'source': 'protocol', 'done': False}):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as directory, \
                 patch.object(d, 'OUTPUT', Path(directory) / 'csv'):
                path = d.OUTPUT / 'douyin.csv'
                d.export_csv(path, d.COLUMNS, [])
                checkpoint = d.sidecar_path(d.OUTPUT / 'douyin_progress.json', 'state')
                if previous is not None:
                    d._save_progress(checkpoint, {'word': previous})
                with patch.object(d, 'collect', side_effect=RuntimeError('word 第1页缺少有效 cursor，断点未推进')):
                    _, blocked = d._collect_browser_batch(['word'], Mock(), d.START, d.END)
                self.assertEqual(blocked, ['word'])
                saved = json.loads(checkpoint.read_text(encoding='utf-8'))['word']
                self.assertEqual(saved['offset'], 60 if previous else 0)
                self.assertEqual(saved['page'], 4 if previous else 0)
                self.assertFalse(saved['done'])

    def test_kuaishou_known_stalls_do_not_trip_new_failure_circuit(self):
        import kuaishou as k
        with tempfile.TemporaryDirectory() as directory:
            path, db = Path(directory) / 'posts.csv', Path(directory) / 'search.sqlite3'
            with closing(k.SearchState(db, path, k.START, k.END)) as state, closing(k.CommentStore(path, '快手')) as store:
                for ident in ('old-a', 'old-b', 'old-c'):
                    state.put(ident, k.canonical_post('快手', {'内容ID': ident, '发布时间': '2023-01-01'}), False)
                    store.register(ident)
                    store.save_page(ident, [comment(ident)], 'saved-' + ident, False)
                    store.mark(ident, '分页停滞，保留原游标待检查')
                state.sync_csv()
            client = Mock()
            client.search.return_value = {'result': 1, 'pcursor': 'no_more', 'feeds': [
                {'photo': {'id': 'new', 'timestamp': 1672531200000, 'caption': 'fixture'}}]}
            client.comments.side_effect = lambda ident, *args: {
                'result': 1, 'rootCommentsV2': [], 'pcursorV2': 'no_more' if ident == 'new' else 'advancing-empty',
                'commentCountV2': 0 if ident == 'new' else 8}
            with self.assertLogs(k.logger, level='WARNING') as captured:
                self.assertEqual(k.export_search(client, ['word'], path, k.START, k.END, state_path=db), 1)
            self.assertFalse(any('连续3篇评论分页停滞' in text for text in captured.output))
            self.assertEqual(client.comments.call_count, 4)
            with closing(k.CommentStore(path, '快手')) as store:
                for ident in ('old-a', 'old-b', 'old-c'):
                    self.assertEqual(store.state(ident)['cursor'], 'saved-' + ident)
                    self.assertFalse(store.state(ident)['done'])
                self.assertTrue(store.state('new')['done'])

    def test_kuaishou_explicit_access_error_still_stops_requests(self):
        import kuaishou as k
        with tempfile.TemporaryDirectory() as directory:
            path, db_path = Path(directory) / "posts.csv", Path(directory) / "search.sqlite3"
            with closing(k.SearchState(db_path, path, k.START, k.END)) as state:
                state.put("post", k.canonical_post("快手", {"内容ID": "post", "发布时间": "2023-01-01"}), False)
                state.sync_csv()
            client = Mock()
            client.comments.side_effect = RuntimeError("接口 HTTP 429；停止采集")
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                k.export_search(client, ["双减"], path, k.START, k.END, state_path=db_path)
            client.comments.assert_called_once()
            client.search.assert_not_called()
            self.assertEqual(len(rows(path)), 1)

    def test_xhs_completed_keywords_backfill_old_csv_and_report_missing_token(self):
        import xiaohongshu as x
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(x, "OUTPUT", root / "csv"), patch.object(x, "STATE_DIR", root / "runtime"):
                x.STATE_DIR.mkdir()
                path = x.OUTPUT / "小红书_双减_笔记.csv"
                x.write_notes_csv([{"status": "success", "note_id": ident,
                                    "content": {"title": ident, "time": "2023-01-01"}} for ident in ("a", "b", "missing")], path)
                events = [{"note_id": ident} for ident in ("a", "b", "missing")] + [{"keyword": "双减"}, {"run_complete": True}]
                (x.STATE_DIR / "full_run_state.jsonl").write_text(
                    "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
                cache = x.STATE_DIR / "candidate_pages_rpc.json"
                cache.write_text(json.dumps({"keyword": "双减", "search_id": "fixture", "pages": [
                    {"has_more": False, "notes": [{"note_id": ident, "xsec_token": "fixture"} for ident in ("a", "b")]}]}), encoding="utf-8")
                before = cache.read_bytes()
                calls = []
                async def comments(client, ident, cursor, token, limiter):
                    self.assertEqual(token, "fixture")
                    calls.append((ident, cursor))
                    if len(calls) > 1:
                        self.assertTrue(rows(path.with_name(path.stem + "_高赞评论.csv")))
                    return {"code": 0, "data": {"has_more": not bool(cursor), "cursor": "next" if not cursor else "",
                            "comments": [{"id": ident + (cursor or "first"), "content": "补采", "like_count": 8,
                                          "create_time": 1672531200000, "user_info": {"nickname": "作者"}}]}}
                client = Mock(post=AsyncMock(), close=AsyncMock())
                args = SimpleNamespace(requirements=None, no_year_queries=True)
                with patch.object(x, "load_requirement_keywords", return_value=["双减"]), \
                     patch.object(x, "AsyncBrowserRPCClient", return_value=client), \
                     patch.object(x, "async_fetch_comments", side_effect=comments), \
                     patch.object(x, "async_collect_note", new_callable=AsyncMock) as detail:
                    self.assertEqual(asyncio.run(x.cmd_all_notes(args)), 2)
                    self.assertEqual(calls, [("a", ""), ("b", ""), ("a", "next"), ("b", "next")])
                    self.assertEqual(len(rows(path.with_name(path.stem + "_高赞评论.csv"))), 4)
                    self.assertEqual(asyncio.run(x.cmd_all_notes(args)), 2)
                    self.assertEqual(len(calls), 4)
                    client.post.assert_not_called()
                    detail.assert_not_called()
                with closing(x.CommentStore(path, "小红书")) as store:
                    self.assertFalse(store.state("missing")["done"])
                    self.assertIn("缺少候选令牌", store.state("missing")["status"])
                    self.assertTrue(all(store.state(ident)["done"] for ident in ("a", "b")))
                self.assertEqual(cache.read_bytes(), before)
                self.assertEqual([r["内容ID"] for r in rows(path)], ["a", "b", "missing"])


if __name__ == "__main__":
    unittest.main()
