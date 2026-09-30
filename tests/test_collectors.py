"""python -m unittest discover -s tests -v；全部离线，网络/浏览器入口使用测试替身。"""
import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))

from douyin import (START, END, POST_FIELDS, COMMENT_FIELDS, CST, CommentStore,
                            atomic_csv, canonical_post, exact_count, search_queries, timestamp)

ROOT = Path(__file__).resolve().parents[1]


def comment(i, likes=None, when="2026-07-24 23:59:59"):
    return {"评论ID": str(i), "评论内容": "=公式,换行\n文本", "评论点赞数": i if likes is None else likes,
            "评论时间": when, "评论者昵称": "评论人"}


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def check_platform(platform):
    import importlib
    main = importlib.import_module(platform)
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "posts.csv"
        name = {"douyin": "抖音", "kuaishou": "快手", "xiaohongshu": "小红书"}[platform]
        store = CommentStore(target, name)
        store.register("post")
        calls = []
        failed = False
        first = [comment(i) for i in range(1, 21)]
        # 最高赞位于次页；重复ID、边界、未知时间/点赞及区间外都不能污染TOP20。
        second = [comment(21, 1000), comment(1), comment(22, 2000, "2026-07-25 00:00:00"),
                  comment(23, 3000, "2021-07-23 23:59:59"), comment(24, 999, "2021-07-24 00:00:00"),
                  {**comment(25), "评论点赞数": None}, comment(26, 5000, "未知")]

        def page(cursor):
            nonlocal failed
            calls.append(str(cursor))
            if str(cursor) in ("", "0"):
                return first, "20", True
            if not failed:
                failed = True
                raise RuntimeError("测试：次页请求中断")
            return second, "", False

        if platform == "douyin":
            def fetch(project, args, cookie):
                assert project == "comment_list"
                data, cursor, more = page(args[args.index("--cursor") + 1])
                return {"status_code": 0, "cursor": int(cursor or 0), "has_more": int(more), "comments": [
                    {"cid": c["评论ID"], "text": c["评论内容"], "digg_count": c["评论点赞数"],
                     "create_time": c["评论时间"], "user": {"nickname": c["评论者昵称"]}} for c in data]}
            collect = lambda: main.collect_comments(fetch, None, store, "post")
            from datetime import datetime
            video = {"aweme_id": "post", "create_time": int(datetime(2021, 7, 24, tzinfo=CST).timestamp()),
                     "desc": "双减", "statistics": {"digg_count": 0}, "video": {"duration": 1000}}
            normalized = dict(zip(main.COLUMNS, main._row(video, "双减")))
            fields = main.COLUMNS
        elif platform == "kuaishou":
            from kuaishou import collect_comments, normalize, FIELDS
            class Client:
                def comments(self, pid, cursor, number):
                    data, cursor, more = page(cursor)
                    return {"result": 1, "pcursorV2": cursor if more else "no_more", "commentCountV2": 26,
                            "rootCommentsV2": [{"comment_id": c["评论ID"], "content": c["评论内容"],
                            "likeCount": c["评论点赞数"], "timestamp": c["评论时间"], "author_name": c["评论者昵称"]} for c in data]}
            collect = lambda: collect_comments(Client(), store, "post")
            from datetime import datetime
            normalized = normalize({"photo": {"id": "post", "timestamp": datetime(2021, 7, 24, tzinfo=CST).timestamp(),
                        "caption": "双减", "likeCount": 0, "collectCount": 7}}, "双减", START, END)
            assert normalized["收藏数"] == 7
            fields = FIELDS
        else:
            from xiaohongshu import async_collect_comments, async_collect_note
            from xiaohongshu import COLUMNS, write_notes_csv
            class Client:
                async def request(self, method, path, *args, **kwargs):
                    from urllib.parse import urlparse, parse_qs
                    assert method == "GET"
                    cursor = parse_qs(urlparse(path).query).get("cursor", [""])[0]
                    data, cursor, more = page(cursor)
                    return type("Response", (), {"status_code": 200, "json": lambda self: {"code": 0, "data": {
                        "cursor": cursor, "has_more": more, "comments": [{"id": c["评论ID"], "content": c["评论内容"],
                        "like_count": c["评论点赞数"], "create_time": c["评论时间"],
                        "user_info": {"nickname": c["评论者昵称"]}} for c in data]}}})()
            collect = lambda: asyncio.run(async_collect_comments(Client(), "post", "test-token", store))
            result = {"status": "success", "note_id": "post", "content": {"title": "双减", "time": "2021-07-24"},
                      "propagation": {"liked": 0}}
            write_notes_csv([result], target)
            normalized = rows(target)[0]
            fields = COLUMNS
            async def detail(*args):
                return {"publish_time": "2021-07-24", "liked_count": "0", "note_id": "post"}
            with patch("xiaohongshu.async_fetch_note_detail", side_effect=detail):
                data = asyncio.run(async_collect_note(None, "post", comment_limit=0, include_author_red_id=False))
                assert data["propagation"]["liked"] == 0 and data["propagation"]["collected"] is None
        assert fields[:len(POST_FIELDS)] == POST_FIELDS
        assert normalized["内容ID"] == "post" and normalized["平台"] == name
        assert normalized.get("收藏数") in (None, "", 7) and normalized["链接"].endswith("/post")
        atomic_csv(target, fields, [normalized])
        try:
            collect()
        except RuntimeError as exc:
            assert "中断" in str(exc)
        else:
            raise AssertionError("评论失败不能当作成功")
        assert store.state("post")["cursor"] == "20" and not store.state("post")["done"]
        store.export()
        assert len(rows(store.output)) == 20 and "中断" in rows(store.output)[0]["采集状态"]
        store.close()
        store = CommentStore(target, name)
        collect()
        assert calls == (["0", "20", "20"] if platform == "douyin" else ["", "20", "20"])
        assert store.state("post")["done"]
        assert [r["评论ID"] for r in store.top("post")][:2] == ["21", "24"]
        assert len(store.top("post")) == 20
        assert not {"22", "23", "25", "26"} & {r["评论ID"] for r in store.top("post")}
        collect()  # 完成评论不重复发请求。
        assert len(calls) == 3
        store.export()
        store.report()
        exported = rows(store.output)
        assert list(exported[0]) == [k for k in COMMENT_FIELDS if k not in ("评论者ID", "IP属地")]
        assert exported[0]["评论内容"].startswith("'=")
        assert all(r["内容ID"] == "post" and r["链接"] == normalized["链接"] for r in exported)
        report = json.loads((target.parent / "reports/posts_覆盖报告.json").read_text(encoding="utf-8"))
        assert report["按年篇数"]["2021"] == 1 and report["按年篇数"]["2022"] == 0
        assert report["评论采集"][0]["候选数"] == 26 and report["评论采集"][0]["导出数"] == 20
        store.close()

        # 老表升级必须保留关联ID并备份；不能把旧表头和新记录直接拼接。
        legacy_path = Path(directory) / "legacy.csv"
        if platform == "douyin":
            legacy = dict(zip(main.COLUMNS, main._row(video, "双减")))
            atomic_csv(legacy_path, main.LEGACY_COLUMNS, [legacy])
            from douyin import upgrade_csv
            before = legacy_path.read_bytes()
            upgrade_csv(legacy_path, main.LEGACY_COLUMNS, main.COLUMNS, lambda r: canonical_post("抖音", r))
            assert rows(legacy_path)[0]["内容ID"] == "post"
            assert (legacy_path.parent / "backups/legacy.csv.schema-v1.bak").read_bytes() == before
        elif platform == "kuaishou":
            import sqlite3
            from kuaishou import SearchState, LEGACY_FIELDS
            legacy = {k: normalized.get(k) for k in LEGACY_FIELDS}
            atomic_csv(legacy_path, LEGACY_FIELDS, [legacy])
            state_path = Path(directory) / "old.sqlite3"
            with sqlite3.connect(state_path) as db:
                db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);'
                                 'CREATE TABLE videos(photo_id TEXT PRIMARY KEY,payload TEXT,enriched INTEGER);')
                db.execute("INSERT INTO videos VALUES (?,?,1)", ("post", json.dumps(legacy)))
                db.execute("INSERT INTO meta VALUES ('config',?)", (json.dumps({"version": 1,
                    "fields": LEGACY_FIELDS, "start": str(START), "end": str(END),
                    "output": str(legacy_path)}),))
            db.close()
            state = SearchState(state_path, legacy_path, START, END)
            state.sync_csv()
            state.close()
            assert rows(legacy_path)[0]["内容ID"] == "post"
            assert state_path.with_suffix(".sqlite3.schema-v1.bak").exists()
        else:
            from xiaohongshu import LEGACY_COLUMNS, prepare_notes_csv
            atomic_csv(legacy_path, LEGACY_COLUMNS, [normalized])
            try:
                prepare_notes_csv(legacy_path)
            except ValueError:
                pass
            else:
                raise AssertionError("无ID旧表不能猜测迁移")
            before = legacy_path.read_bytes()
            prepare_notes_csv(legacy_path, ["post"])
            assert rows(legacy_path)[0]["内容ID"] == "post"
            assert (legacy_path.parent / "backups/legacy.csv.schema-v1.bak").read_bytes() == before

            # 真实批量入口：评论次页失败仍保存贴文；续跑只补评论，不重采详情。
            from types import SimpleNamespace
            root = Path(directory) / "batch"
            root.mkdir()
            doc = root / "requirements.md"
            doc.write_text("test", encoding="utf-8")
            failed = False
            calls.clear()
            async def search(*args, **kwargs):
                yield {"note_id": "post", "xsec_token": "test-token", "publish_time": "2021-07-24"}
            async def fake_detail(*args, **kwargs):
                return {"status": "success", "note_id": "post", "content": {"title": "双减", "time": "2021-07-24"},
                        "propagation": dict(liked=0, collected=0, comment=0, shared=0)}
            class BatchClient(Client):
                def __init__(self, **kwargs):
                    pass
                def set_cookies(self, cookies):
                    pass
                async def close(self):
                    pass
            with patch.object(main, "ROOT", root), patch.object(main, "OUTPUT", root / "output/csv"), \
                 patch.object(main, "STATE_DIR", root / "runtime"), patch.object(main, "load_requirement_keywords", return_value=["双减"]), \
                 patch.object(main, "AsyncBrowserRPCClient", BatchClient), \
                 patch.object(main, "iter_search_notes", side_effect=search), \
                 patch.object(main, "async_collect_note", side_effect=fake_detail) as detail:
                args = SimpleNamespace(requirements=doc, transport="rpc", no_year_queries=True)
                assert asyncio.run(main.cmd_all_notes(args)) == 1
                assert len(rows(root / "output/csv/小红书_双减_笔记.csv")) == 1
                assert asyncio.run(main.cmd_all_notes(args)) == 0
                assert detail.call_count == 1
                assert calls == ["", "20", "20"]


class DatasetTest(unittest.TestCase):
    def test_counts_dates_queries_and_cursor_safety(self):
        self.assertEqual(exact_count("9007199254740993"), 9007199254740993)
        for value in (None, True, "1.2万", "NaN", -1, 1.5):
            self.assertIsNone(exact_count(value))
        self.assertEqual(timestamp("2021-07-23T16:00:00Z"), "2021-07-24 00:00:00")
        self.assertEqual(timestamp("2026-07-24T15:59:59Z"), "2026-07-24 23:59:59")
        self.assertEqual(len(search_queries(["双减", "双减"])), 7)
        self.assertEqual(search_queries(["双减"], yearly=False), ["双减"])
        with tempfile.TemporaryDirectory() as directory:
            store = CommentStore(Path(directory) / "data.csv", "抖音")
            try:
                store.register("a")
                store.save_page("a", [comment(1)], "next", False)
                for data, cursor in (([comment(2)], "next"), ([], "other"), ([comment(1)], "other")):
                    with self.assertRaises(RuntimeError):
                        store.save_page("a", data, cursor, False)
                self.assertEqual(store.state("a")["pages"], 1)
                with self.assertRaises(RuntimeError):
                    store.save_page("a", [{"评论ID": ""}], "", True)
                store.register("empty")
                store.save_page("empty", [], "", True)
                self.assertTrue(store.state("empty")["done"])
                self.assertEqual(store.top("empty"), [])
            finally:
                store.close()

    def test_saved_comment_logs_are_committed_deduplicated_and_safe(self):
        import importlib
        import sqlite3
        for name, platform in (("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书")):
            m = importlib.import_module(name)
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'posts.csv'
                with closing(m.CommentStore(target, platform)) as store:
                    store.register('post')
                    recorded = []
                    def log(message, *args):
                        if message != '%s已保存 | %s':
                            return
                        data = json.loads(args[1])
                        # 独立连接可见，证明打印发生在事务提交之后。
                        with closing(sqlite3.connect(m.sidecar_path(target.with_suffix('.comments.sqlite3'), 'state'))) as db:
                            self.assertIsNotNone(db.execute('SELECT 1 FROM comments WHERE id=?', (data['评论ID'],)).fetchone())
                        self.assertNotIn('private-fixture', args[1])
                        self.assertNotIn('\n', args[1])
                        self.assertNotIn('\x1b', args[1])
                        recorded.append(data)
                    with patch.object(m.logger, 'info', side_effect=log):
                        first = {**comment(1, 0), '评论内容': '中文\n表情🙂\x1b[31m', 'Cookie': 'private-fixture'}
                        store.save_page('post', [first, first], 'p2', False)
                        store.save_page('post', [first, {**comment(2), '评论点赞数': None,
                                                      'xsec_token': 'private-fixture'}], 'p3', False)
                        self.assertEqual([r['评论ID'] for r in recorded], ['1', '2'])
                        self.assertEqual(recorded[0]['评论点赞数'], 0)
                        self.assertTrue(recorded[0]['可参与高赞排序'])
                        self.assertIsNone(recorded[1]['评论点赞数'])
                        self.assertFalse(recorded[1]['可参与高赞排序'])
                        with self.assertRaises(RuntimeError):
                            store.save_page('post', [first], 'p4', False)
                        store.db.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON posts BEGIN SELECT RAISE(ABORT,'fixture'); END")
                        with self.assertRaises(sqlite3.IntegrityError):
                            store.save_page('post', [comment(3)], 'p4', False)
                        self.assertEqual(len(recorded), 2)  # 失败页/重复记录不能打印为新增保存。
                        self.assertEqual(store.state('post')['pages'], 2)
                with closing(m.CommentStore(target, platform, memory=True)) as store, patch.object(m.logger, 'info') as log:
                    store.register('memory')
                    store.save_page('memory', [comment(1)], '', True)
                    log.assert_not_called()  # 内存候选不能冒充已落盘。

    def test_post_logs_follow_persistence_without_dumping_old_rows(self):
        import importlib
        import sqlite3
        for name in ('douyin', 'kuaishou', 'xiaohongshu'):
            m = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as directory:
                root, logged = Path(directory), []
                target = root / 'csv' / ('douyin.csv' if name == 'douyin' else 'posts.csv')
                db_path = root / 'search.sqlite3'
                def log(message, *args):
                    if message != '%s已保存 | %s':
                        return
                    self.assertEqual(args[0], '贴文')
                    data = json.loads(args[1])
                    if name == 'kuaishou':
                        with closing(sqlite3.connect(db_path)) as db:
                            self.assertIsNotNone(db.execute('SELECT 1 FROM videos WHERE photo_id=?', (data['内容ID'],)).fetchone())
                    else:
                        self.assertIn(data['内容ID'], [r['内容ID'] for r in rows(target)])
                    logged.append(data)
                with patch.object(m, 'OUTPUT', root / 'csv'), patch.object(m, 'STATE_DIR', root / 'runtime'), \
                     patch.object(m.logger, 'info', side_effect=log):
                    if name == 'douyin':
                        fetch = Mock(return_value={'status_code': 0, 'has_more': False, 'cursor': 20, 'aweme_list': [
                            {'aweme_id': 'post', 'create_time': 1650000000, 'desc': '新贴文', 'ip_label': '北京',
                             'author': {'nickname': '作者'}, 'video': {'duration': 1000},
                             'statistics': dict(digg_count=0, comment_count=0, collect_count=0, share_count=0)}]})
                        for _ in range(2):
                            m._collect_posts(['双减'], None, START, END, fetch=fetch)
                        fetch.assert_called_once()
                    elif name == 'kuaishou':
                        client = Mock()
                        client.search.return_value = {'result': 1, 'pcursor': 'no_more', 'feeds': [feed('post')]}
                        for _ in range(2):
                            m.export_search(client, ['双减'], target, START, END, comment_counts=False, state_path=db_path)
                        client.search.assert_called_once()
                        client.comments.assert_not_called()
                    else:
                        result = {'status': 'success', 'note_id': 'post', 'content': {'title': '新贴文', 'time': '2023-01-01'}}
                        m.write_notes_csv([result], target)
                        m.write_notes_csv([], target)
                        with patch.object(m, 'export_csv', side_effect=RuntimeError('fixture')):
                            with self.assertRaises(RuntimeError):
                                m.write_notes_csv([{**result, 'note_id': 'unsaved'}], target)
                    self.assertEqual(len(logged), 1)
                    self.assertEqual(logged[0]['内容ID'], 'post')

    def test_platform_pagination_ranking_resume_and_schema(self):
        for platform in ("douyin", "kuaishou", "xiaohongshu"):
            with self.subTest(platform=platform):
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), platform],
                                     capture_output=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)



"""Offline regression only. Run: python -m unittest discover -s tests -p test_search_csv.py"""
import csv
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from kuaishou import (
    ROOT, CST, FIELDS, BudgetExhausted, SearchClient, check_page, count, export_search,
    load_keywords, normalize, search_body, csv_row, is_link_or_junction,
)


def feed(pid="video1", when="2021-07-24T00:00:00", caption="标题,换行\n#双减", **photo):
    return {"photo": {"id": pid, "caption": caption, "timestamp": int(datetime.fromisoformat(when).replace(tzinfo=CST).timestamp() * 1000),
                      "duration": 15050, "viewCount": 0, "likeCount": 9, **photo},
            "author": {"name": "=1+1", "id": "author1"}, "tags": [{"name": "双减", "type": 1}],
            "comment": {"us_c": 1}}


class FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, keyword, cursor, session):
        self.calls.append((keyword, cursor, session))
        if not cursor:
            return {"result": 1, "pcursor": "p2", "searchSessionId": "test-session", "feeds": [feed(), feed("old", "2021-07-23T23:59:59")]}
        return {"result": 1, "pcursor": "no_more", "feeds": [feed(), feed("video2", "2026-07-24T23:59:59"), feed("new", "2026-07-25T00:00:00")]}

    def comment_count(self, pid):
        return 0

    def comments(self, pid, cursor="", page=1):
        return {"result": 1, "rootCommentsV2": [], "pcursorV2": "no_more",
                "commentCountV2": self.comment_count(pid)}


from test_kuaishou_rpc import make_client as make_ks_rpc


class SearchCSVTest(unittest.TestCase):
    def test_kuaishou_main_uses_rpc_port_not_legacy_cookies(self):
        import kuaishou as k
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Mock(used=0)
            def collect(_client, *args, **kwargs):
                self.assertIs(_client, client)
                report = k.sidecar_path(k.OUTPUT / '快手双减_覆盖报告.json', 'reports')
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('{"未完成评论篇数":0}', encoding='utf-8')
                return 0
            with patch.object(k, 'ROOT', root), patch.object(k, 'OUTPUT', root / 'output/csv'), \
                 patch.object(k, 'STATE_DIR', root / 'runtime/kuaishou'), \
                 patch.dict(os.environ, {'KS_COOKIE': 'ignored-legacy-fixture'}), \
                 patch.object(k, 'prepare_kuaishou_browser') as login, \
                 patch.object(k, 'SearchClient', return_value=client) as create, \
                 patch.object(k, 'export_search', side_effect=collect) as run:
                self.assertEqual(k.main(['--keyword', '双减', '--rpc-port', '9334']), 0)
                login.assert_called_once_with(9334)
                create.assert_called_once_with(9334, None, 60.0)
                run.assert_called_once()
                client.close.assert_called_once()

    def test_offline_pipeline(self):
        start, end = date(2021, 7, 24), date(2026, 7, 24)
        with tempfile.TemporaryDirectory(prefix="csv-test-") as tmp:
            tmp = Path(tmp)
            spec = tmp / "需求.md"
            spec.write_text("## 一、搜索关键词\n| 类别 | 搜索词 |\n|---|---|\n|政策|双减、鸡娃。|\n|家长|鸡娃、AI辅导。|\n**双减 + 课后服务**\n## 二、爬取的数据字段\n|快手|作者、点赞数|", encoding="utf-8")
            self.assertEqual(load_keywords(spec), ["双减", "鸡娃", "AI辅导", "双减 课后服务"])
            row = normalize(feed(), "双减", start, end)
            self.assertEqual(row["视频时长"], 15.05)
            self.assertEqual(row["播放量"], 0)
            self.assertIsNone(row["评论数"])  # us_c must never become a comment count
            self.assertIsNone(row["分享数"])
            self.assertIsNone(row["IP 属地"])
            self.assertIsNone(normalize(feed(timestamp=None), "双减", start, end))
            self.assertIsNone(normalize(feed(timestamp="invalid"), "双减", start, end))
            self.assertIsNone(count("1.2万"))
            self.assertIsNone(count(float("nan")))
            self.assertIsNone(count(-1))
            client, out = FakeClient(), tmp / "result.csv"
            self.assertEqual(export_search(client, ["双减", "鸡娃"], out, start, end), 2)
            self.assertIn(("双减", "p2", "test-session"), client.calls)
            self.assertIn(("鸡娃", "", ""), client.calls)
            self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))
            with out.open(encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                self.assertEqual(reader.fieldnames, [k for k in FIELDS if k not in ("收藏数", "分享数", "IP 属地")])
                self.assertEqual(FIELDS[:4], ["平台", "内容ID", "标题", "链接"])
                rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["发布时间"].startswith("2021-07-24"))
            self.assertTrue(rows[1]["发布时间"].startswith("2026-07-24"))
            self.assertEqual(rows[0]["视频标题或描述"], "标题,换行\n#双减")
            self.assertEqual(rows[0]["作者"], "'=1+1")
            self.assertEqual(rows[0]["评论数"], "0")
            self.assertNotIn("分享数", rows[0])
            self.assertNotIn("IP 属地", rows[0])
            self.assertEqual(rows[0]["视频时长"], "15.05")
            self.assertTrue(all(len(r) == len(FIELDS) - 3 for r in rows))
            self.assertEqual(list(csv_row({**row, "额外字段": "禁止导出"})), FIELDS)
            with self.assertRaises(FileExistsError):
                export_search(client, ["双减"], out, start, end)
            client.comment_count = lambda pid: (_ for _ in ()).throw(BudgetExhausted("test"))
            partial = tmp / "partial.csv"
            with self.assertRaises(BudgetExhausted):
                export_search(client, ["双减"], partial, start, end)
            with partial.open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(len(list(csv.DictReader(file))), 1)
        self.assertEqual(search_body("双减", "p2", "s"), {"keyword": "双减", "page": "search", "webPageArea": "", "pcursor": "p2", "searchSessionId": "s"})
        for bad in ({"result": 109}, {"result": 1}, {"result": 1, "feeds": {}, "pcursor": "no_more"}):
            with self.assertRaises(RuntimeError):
                check_page(bad)

    def test_budget_and_exact_body_for_rpc(self):
        client = make_ks_rpc()
        client.limit = 1
        with patch('kuaishou.time.sleep'):
            client.search('双减', 'saved', 'session-fixture')
            with self.assertRaises(BudgetExhausted):
                client.search('双减', 'next', 'session-fixture')
            self.assertEqual(client.used, 1)
            client.limit, client.used = None, 101
            client.search('双减', 'next', 'session-fixture')
        self.assertEqual(client.used, 102)
        self.assertEqual(client.page.evaluate.call_count, 2)
        self.assertEqual(client.page.evaluate.call_args.args[1], ['/rest/v/search/feed',
            search_body('双减', 'next', 'session-fixture')])

    def test_kuaishou_failure_preserves_retry_after_for_supervisor(self):
        import start_all
        for status, payload in ((429, {}), (200, {'result': 2})):
            client = make_ks_rpc(payload, status, retry='900')
            with patch('kuaishou.time.sleep'), self.assertRaises(RuntimeError) as raised:
                client.search('双减', '', '')
            self.assertIn("Retry-After='900'", str(raised.exception))
            self.assertEqual(start_all.retry_wait('kuaishou', 1, str(raised.exception), 300), 900)

    def test_kuaishou_rate_limit_reason_is_preserved_after_rpc(self):
        import start_all
        for message in ('操作太快了，请稍微休息一下', None, '请先登录', 'Cookie: private-fixture'):
            client = make_ks_rpc({'result': 2, 'error_msg': message})
            events = []
            invoke = client.page.evaluate.side_effect
            client.page.evaluate.side_effect = lambda *args: events.append('native') or invoke(*args)
            with patch('kuaishou.time.sleep', side_effect=lambda *args: events.append('wait')), self.assertRaises(RuntimeError) as raised:
                client.comments('test-id', 'saved-cursor', 2)
            reason = str(raised.exception)
            self.assertEqual(events, ['wait', 'native'])
            self.assertNotIn('private-fixture', reason)
            if message and message.startswith('操作太快'):
                self.assertIn('快手请求频率受限', reason)
                for initial, expected in ((300,900),(600,1800),(1200,3600)):
                    self.assertEqual(start_all.retry_wait('kuaishou',1,'采集未完成：'+reason,initial),expected)
                self.assertEqual(start_all.retry_wait('kuaishou',1,reason.replace("'未提供'", "'7200'"),300),7200)
            elif message == '请先登录':
                self.assertTrue(start_all.needs_manual_login(reason))
                self.assertIsNone(start_all.retry_wait('kuaishou',1,reason,300))
            elif message is None:
                self.assertEqual(start_all.retry_wait('kuaishou',1,reason,300),900)

    def test_main_no_arguments_collects_all_pages(self):
        """Run the real dispatch without arguments; browser RPC is replaced."""
        from unittest.mock import Mock
        import kuaishou as entry
        import kuaishou as search

        class ManyPages(FakeClient):
            used = 0
            close = Mock()

            def search(self, keyword, cursor, session):
                self.calls.append((keyword, cursor, session))
                self.used += 1
                page = int(cursor or 1)
                return {"result": 1, "pcursor": str(page + 1) if page < 22 else "no_more",
                        "feeds": [feed(keyword + str(page)), feed("unknown", timestamp=None)]}

        with tempfile.TemporaryDirectory(prefix="main-test-") as tmp:
            root = Path(tmp).resolve()
            (root / "runtime").mkdir()
            (root / "runtime/cookie.txt").write_text("test=synthetic", encoding="utf-8")
            client = ManyPages()
            with patch.object(search, "ROOT", root), patch.object(search, "OUTPUT", root / "output/csv"), \
                    patch.object(search, "STATE_DIR", root / "runtime"), patch.object(search, "load_keywords", return_value=["双减", "鸡娃"]), \
                    patch.object(search, "SearchClient", return_value=client) as constructor, \
                    patch.object(search, "prepare_kuaishou_browser"), \
                    patch.dict("os.environ", {"KS_COOKIE": ""}), patch("sys.argv", ["main.py"]), \
                    patch.object(search.logger, "info"), patch.object(search.logger, "warning") as warning:
                self.assertEqual(entry.main(), 0)
                self.assertEqual(entry.main(), 0)  # 已完成的检索不重扫，第二次启动零搜索请求。
            self.assertEqual(constructor.call_count, 2)
            constructor.assert_called_with(9224, None, 60.0)
            self.assertEqual(warning.call_count, 2)
            self.assertIn("分享数、IP 属地", warning.call_args.args[0])
            self.assertEqual(client.used, 44 * 7)
            self.assertEqual(sorted(p.name for p in (root / "output/csv").glob("*.csv")),
                             ["快手双减.csv", "快手双减_高赞评论.csv"])
            self.assertIn(("双减", "22", ""), client.calls)
            self.assertIn(("鸡娃", "22", ""), client.calls)
            with (root / "output/csv/快手双减.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 44 * 7)
            self.assertTrue(all(date(2021, 7, 24) <= datetime.fromisoformat(r["发布时间"]).date()
                                <= date(2026, 7, 24) for r in rows))
            self.assertTrue(all(len(r) == len(FIELDS) - 3 for r in rows))
            self.assertEqual(client.close.call_count, 2)

    def test_persistent_resume_and_incremental(self):
        from unittest.mock import Mock
        import sqlite3
        start, end = date(2021, 7, 24), date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            out, db = Path(tmp) / "one.csv", Path(tmp) / "state.sqlite3"
            first = FakeClient()
            search = first.search
            def stop_on_second_page(keyword, cursor, session):
                if cursor:
                    raise RuntimeError("simulated disconnect")
                return search(keyword, cursor, session)
            first.search = stop_on_second_page
            with self.assertRaises(RuntimeError):
                export_search(first, ["双减", "鸡娃"], out, start, end, state_path=db)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT cursor,session,page,done FROM progress WHERE keyword='双减'").fetchone(),
                                 ("p2", "test-session", 1, 0))
            second = FakeClient()
            second.comment_count = Mock(return_value=0)
            self.assertEqual(export_search(second, ["双减", "鸡娃"], out, start, end, state_path=db), 1)
            self.assertEqual(second.calls[0], ("双减", "p2", "test-session"))
            second.comment_count.assert_called_once_with("video2")
            third = FakeClient()
            base_search = third.search
            def with_new_video(keyword, cursor, session):
                data = base_search(keyword, cursor, session)
                data["feeds"].append(feed("new-id-same-caption"))
                return data
            third.search = with_new_video
            third.comment_count = Mock(return_value=0)
            self.assertEqual(export_search(third, ["双减", "鸡娃"], out, start, end, state_path=db), 0)
            self.assertEqual(third.calls, [])
            third.comment_count.assert_not_called()
            self.assertEqual(export_search(third, ["双减", "鸡娃"], out, start, end, state_path=db, restart=True), 1)
            self.assertEqual(third.calls[0], ("双减", "", ""))
            third.comment_count.assert_called_once_with("new-id-same-caption")
            with out.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, [k for k in FIELDS if k not in ("收藏数", "分享数", "IP 属地")])
                self.assertEqual(len(list(reader)), 3)  # Same text does NOT make different video IDs duplicates.
            before = out.read_bytes()
            with self.assertRaises(ValueError):
                export_search(FakeClient(), ["双减"], out, date(2022, 1, 1), end, state_path=db)
            self.assertEqual(out.read_bytes(), before)
            unknown_state = Path(tmp) / "no-index.sqlite3"
            with self.assertRaises(FileExistsError):
                export_search(FakeClient(), ["双减"], out, start, end, state_path=unknown_state)
            self.assertFalse(unknown_state.exists())
            self.assertEqual(out.read_bytes(), before)

    def test_interrupted_enrichment_and_csv_recovery(self):
        from unittest.mock import Mock, patch
        start, end = date(2021, 7, 24), date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            out, db = Path(tmp) / "one.csv", Path(tmp) / "state.sqlite3"
            first = FakeClient()
            first.search = Mock(return_value={"result": 1, "pcursor": "no_more", "feeds": [feed("a"), feed("b"), feed("c")]})
            first.comment_count = Mock(side_effect=[0, KeyboardInterrupt()])
            with self.assertRaises(KeyboardInterrupt):
                export_search(first, ["双减"], out, start, end, state_path=db)
            with out.open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual([r["评论数"] for r in csv.DictReader(f)], ["0", "", ""])
            # Simulate a failed CSV replacement: SQLite must still recover every committed record.
            out.write_bytes(b"partial CSV")
            out.with_name("one_高赞评论.csv").write_bytes(b"partial comments CSV")
            second = FakeClient()
            second.search = Mock(return_value={"result": 1, "pcursor": "no_more", "feeds": []})
            second.comment_count = Mock(return_value=7)
            with patch("kuaishou.os.replace", side_effect=PermissionError), patch("kuaishou.time.sleep") as local_wait:
                with self.assertRaisesRegex(RuntimeError, "CSV 被占用"):
                    export_search(second, ["双减"], out, start, end, state_path=db)
                self.assertGreaterEqual(local_wait.call_count, 2)  # finally 还会导出；均只等本地文件，不重试接口。
            second.search.assert_not_called()
            self.assertEqual(out.read_bytes(), b"partial CSV")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])
            self.assertEqual(export_search(second, ["双减"], out, start, end, state_path=db), 0)
            self.assertEqual([c.args[0] for c in second.comment_count.call_args_list], ["b", "c"])
            second.search.assert_not_called()  # 整页贴文已提交；评论中断不能导致旧搜索页重发。
            # Neither pending video is returned again, but both still get enriched from the durable index.
            with out.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["评论数"] for r in rows], ["0", "7", "7"])
            self.assertTrue(all(len(r) == len(FIELDS) - 3 for r in rows))
            self.assertEqual(out.read_bytes().count(b"\xef\xbb\xbf"), 1)

    def test_page_cap_restart_and_later_comment_enrichment(self):
        from unittest.mock import Mock
        start, end = date(2021, 7, 24), date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            out, db = Path(tmp) / "one.csv", Path(tmp) / "state.sqlite3"
            first = FakeClient()
            first.comment_count = Mock(side_effect=AssertionError("comment request disabled"))
            self.assertEqual(export_search(first, ["双减", "鸡娃"], out, start, end, pages=1,
                                           comment_counts=False, state_path=db), 1)
            second = FakeClient()
            second.comment_count = Mock(return_value=0)
            self.assertEqual(export_search(second, ["双减", "鸡娃"], out, start, end, pages=1, state_path=db), 1)
            self.assertEqual(second.calls, [("双减", "p2", "test-session"), ("鸡娃", "p2", "test-session")])
            self.assertEqual([c.args[0] for c in second.comment_count.call_args_list], ["video1", "video2"])
            # A forced restart preserves ID dedup and works even with an unfinished sweep.
            third = FakeClient()
            self.assertEqual(export_search(third, ["双减", "鸡娃"], out, start, end, pages=1, state_path=db), 0)
            fourth = FakeClient()
            fourth.comment_count = Mock(side_effect=AssertionError("already enriched"))
            self.assertEqual(export_search(fourth, ["双减", "鸡娃"], out, start, end, pages=1,
                                           state_path=db, restart=True), 0)
            self.assertEqual(fourth.calls, [("双减", "", ""), ("鸡娃", "", "")])
            fourth.comment_count.assert_not_called()
            self.assertEqual(export_search(FakeClient(), ["双减"], out, start, end, pages=1, state_path=db), 0)
            last = FakeClient()
            self.assertEqual(export_search(last, ["双减", "鸡娃"], out, start, end, state_path=db), 0)
            self.assertEqual(last.calls, [("鸡娃", "p2", "test-session")])  # Skip completed keyword on resume.
            with out.open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)

    def test_stalled_comments_keep_cursor_and_do_not_replay_search(self):
        from unittest.mock import Mock
        from kuaishou import CommentStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out, db = root / 'data.csv', root / 'search.sqlite3'
            client = Mock()
            client.search.return_value = {'result': 1, 'pcursor': 'no_more', 'feeds': [feed('bad'), feed('good')]}
            client.comments.side_effect = lambda pid, *_: {'result': 1, 'rootCommentsV2': [],
                'pcursorV2': 'unexpected-next' if pid == 'bad' else 'no_more', 'commentCountV2': 0}
            self.assertEqual(export_search(client, ['双减'], out, START, END, state_path=db), 2)
            with closing(CommentStore(out, '快手')) as store:
                self.assertFalse(store.state('bad')['done'])
                self.assertEqual(store.state('bad')['cursor'], '')
                self.assertTrue(store.state('bad')['status'].startswith('分页停滞'))
                self.assertTrue(store.state('good')['done'])
            client.reset_mock()
            self.assertEqual(export_search(client, ['双减'], out, START, END, state_path=db), 0)
            client.search.assert_not_called()
            client.comments.assert_called_once_with('bad', '', 1)  # 新一轮也补旧停滞任务，仍用原游标。
            client.reset_mock()
            self.assertEqual(export_search(client, ['双减'], out, START, END, state_path=db, retry_stalled=False), 0)
            client.comments.assert_not_called()

    def test_link_check_without_new_pathlib_api(self):
        import stat
        from types import SimpleNamespace
        from unittest.mock import Mock
        path = Mock(spec=["lstat"])
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(is_link_or_junction(path))  # Windows junction/reparse point
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFLNK)
        self.assertTrue(is_link_or_junction(path))
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0)
        self.assertFalse(is_link_or_junction(path))
        path.lstat.side_effect = FileNotFoundError
        self.assertFalse(is_link_or_junction(path))



"""Offline RPC contracts. Node fixtures are our own fake page, never site JS."""
import asyncio
import csv
import json
import shutil
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError, HTTPError
from unittest.mock import AsyncMock, Mock, patch

from xiaohongshu import SEARCH, FEED
from xiaohongshu import RiskControlError, TransientRiskError
from xiaohongshu import AsyncBrowserRPCClient, BRIDGE_JS, START_URL


class BrowserRPCTest(unittest.TestCase):
    def test_default_cli_selects_rpc(self):
        from xiaohongshu import main
        with patch('sys.argv', ['main.py']), \
             patch('xiaohongshu.cmd_all_notes', new_callable=AsyncMock, return_value=0) as collect:
            self.assertEqual(main(), 0)
            self.assertEqual(collect.call_args.args[0].transport, 'rpc')
            self.assertEqual(collect.call_args.args[0].rpc_port, 9223)

    def test_port_validation(self):
        for port in (0, 65536, '9223', True):
            with self.assertRaises(ValueError):
                AsyncBrowserRPCClient(port)

    def tab(self, payload):
        tab = Mock()
        tab.documents = deque()
        tab.evaluate.return_value = {'url': '/search_result'}
        tab.call.return_value = {'result': {'value': json.dumps(payload)}}
        return tab

    def test_rpc_calls_original_page_function_without_navigation_or_signer(self):
        async def check():
            client = AsyncBrowserRPCClient()
            payload = {'code': 0, 'data': {'items': [], 'has_more': False}}
            client.search_tab = self.tab({'ok': True, 'payload': payload})
            body = {'keyword': '双减', 'page': 1, 'search_id': 'test'}
            with patch.object(client, '_rate_limit', new_callable=AsyncMock) as pace:
                self.assertFalse(hasattr(client, '_build_headers'))
                response = await client.request('POST', SEARCH, body)
                self.assertEqual(response.json(), payload)
                pace.assert_awaited_once()
                client.search_tab.navigate.assert_not_called()
                method, params = client.search_tab.call.call_args.args
                self.assertEqual(method, 'Runtime.evaluate')
                self.assertTrue(params['awaitPromise'])
                self.assertIn('rpc.call', params['expression'])
                self.assertIn(json.dumps([SEARCH, body], ensure_ascii=True), params['expression'])
        asyncio.run(check())

    def test_comments_use_native_get_and_keep_pagination_envelope(self):
        async def check():
            client = AsyncBrowserRPCClient()
            payload = {'code': 0, 'data': {'comments': [], 'cursor': '', 'has_more': False}}
            client.search_tab = self.tab({'ok': True, 'payload': payload})
            path = '/api/sns/web/v2/comment/page?note_id=n&cursor=&xsec_token=test'
            with patch.object(client, '_rate_limit', new_callable=AsyncMock):
                response = await client.request('GET', path)
                self.assertEqual(response.json(), payload)
                self.assertIn(json.dumps([path, None], ensure_ascii=True),
                              client.search_tab.call.call_args.args[1]['expression'])
            for method, url in [('POST', path), ('GET', 'https://example.com' + path),
                                ('GET', '/api/sns/web/v2/comment/page?cursor=1')]:
                with self.assertRaises(ValueError):
                    await client.request(method, url)
        asyncio.run(check())

    def test_access_error_latches_and_never_retries(self):
        async def check():
            client = AsyncBrowserRPCClient()
            client.search_tab = self.tab({'ok': False, 'status': 461, 'code': 300013})
            with patch.object(client, '_rate_limit', new_callable=AsyncMock):
                with self.assertRaises(TransientRiskError):
                    await client.request('POST', FEED, {'source_note_id': 'n', 'xsec_token': 'test'})
                with self.assertRaisesRegex(RuntimeError, '本轮已停止'):
                    await client.request('POST', FEED, {'source_note_id': 'n', 'xsec_token': 'test'})
                self.assertEqual(client.search_tab.call.call_count, 1)
        asyncio.run(check())

    def test_incomplete_envelope_is_not_a_success_or_last_page(self):
        for payload in ({'items': []}, {'code': 0, 'data': {}},
                        {'code': 0, 'data': {'items': []}},
                        {'code': 0, 'data': {'items': [], 'has_more': 'false'}},
                        {'code': False, 'data': {'items': [], 'has_more': False}}):
            client = AsyncBrowserRPCClient()
            client.search_tab = self.tab({'ok': True, 'payload': payload})
            with self.assertRaises(RuntimeError):
                client._invoke(SEARCH, {'keyword': 'test', 'page': 1})

    def test_only_allowed_business_paths(self):
        async def check():
            client = AsyncBrowserRPCClient()
            with patch.object(client, '_start') as start:
                for method, path in [('GET', SEARCH), ('POST', '/api/sns/web/v1/note/like')]:
                    with self.assertRaises(ValueError):
                        await client.request(method, path, {})
                start.assert_not_called()
        asyncio.run(check())

    def test_local_attach_rejects_ambiguous_tabs_and_remote_socket(self):
        base = {'type': 'page', 'url': 'https://www.xiaohongshu.com/search_result',
                'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}
        for tabs in ([], [base, base], [{**base, 'webSocketDebuggerUrl': 'ws://example.com:9223/test'}]):
            client = AsyncBrowserRPCClient()
            with patch('xiaohongshu.build_opener'), \
                 patch('xiaohongshu.json.load', return_value=tabs), \
                 patch('xiaohongshu.CDPTab') as connect:
                with self.assertRaises(RuntimeError):
                    client._start()
                connect.assert_not_called()

    def test_attach_installs_bridge_without_importing_cookies(self):
        tab = self.tab({})
        tab.evaluate.side_effect = [{'url': '/search_result'}, None,
                                    {'url': '/search_result'}, True, {'ready': True, 'module': 'fake'}]
        metadata = [{'type': 'page', 'url': 'https://www.xiaohongshu.com/search_result',
                     'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
        client = AsyncBrowserRPCClient()
        with patch('xiaohongshu.build_opener'), \
             patch('xiaohongshu.json.load', return_value=metadata), \
             patch('xiaohongshu.CDPTab', return_value=tab):
            client._start()
            self.assertTrue(client._owns_bridge)
            tab.call.assert_not_called()  # No Network.setCookies / Page.navigate.
            self.assertEqual(tab.evaluate.call_args.args[0], BRIDGE_JS)

    def test_refused_port_launches_once_and_waits_for_page(self):
        client = AsyncBrowserRPCClient()
        tab = Mock()
        tab.evaluate.side_effect = [None, False, True, {'ready': True}]
        blank = [{'type': 'page', 'url': 'about:blank',
                  'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
        with patch.object(client, '_list_tabs', side_effect=[URLError(ConnectionRefusedError()), blank]) as listing, \
             patch.object(client, '_launch_headed') as launch, \
             patch.object(client, '_check_page'), patch('xiaohongshu.time.sleep') as sleep, \
             patch('xiaohongshu.CDPTab', return_value=tab):
            client._start()
            client._start()  # Subsequent calls must reuse the socket, not start another Chrome.
        launch.assert_called_once()
        self.assertEqual(listing.call_count, 2)
        tab.navigate.assert_called_once_with(START_URL)
        self.assertNotIn('search_result', START_URL)  # 冷启动也不重发已采关键词首页。
        sleep.assert_called_once_with(0.5)
        tab.call.assert_not_called()  # Startup never invokes the business RPC.
        self.assertTrue(client._owns_bridge)

    def test_new_run_rebuilds_only_stopped_idle_bridge_without_reload(self):
        metadata = [{'type': 'page', 'url': 'https://www.xiaohongshu.com/search_result',
                     'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
        client, tab = AsyncBrowserRPCClient(), Mock()
        tab.evaluate.side_effect = [{'version': 1, 'busy': False, 'stopped': True, 'uncertain': False},
                                    True, False, True, {'ready': True}]
        with patch.object(client, '_list_tabs', return_value=metadata), \
             patch.object(client, '_check_page'), patch('xiaohongshu.CDPTab', return_value=tab), \
             patch('xiaohongshu._collector_lease', SimpleNamespace(closed=False)), \
             patch('xiaohongshu.time.sleep'):
            client._start()
            client._start()
        tab.call.assert_not_called()  # 不刷新、不导航、不发送业务请求。
        self.assertIn('!window.__xhsCollectorRPC', tab.evaluate.call_args_list[-2].args[0])
        self.assertTrue(client._owns_bridge)
        for previous in ({'version': 1, 'busy': True, 'stopped': True},
                         {'version': 1, 'busy': False, 'stopped': False},
                         {'version': 1, 'busy': False, 'stopped': True, 'uncertain': False},
                         {'version': 2, 'busy': False, 'stopped': True}):
            client, tab = AsyncBrowserRPCClient(), Mock()
            tab.evaluate.return_value = previous
            with patch.object(client, '_list_tabs', return_value=metadata), \
                 patch.object(client, '_check_page'), patch('xiaohongshu.CDPTab', return_value=tab):
                with self.assertRaisesRegex(RuntimeError, '不接管、不刷新'):
                    client._start()
            tab.call.assert_not_called()
            self.assertFalse(client._owns_bridge)
        # 登录/验证必须先停，不能借恢复连接刷新掉验证页面。
        client, tab = AsyncBrowserRPCClient(), Mock()
        with patch.object(client, '_list_tabs', return_value=metadata), \
             patch.object(client, '_check_page', side_effect=RiskControlError('人工验证')), \
             patch('xiaohongshu.CDPTab', return_value=tab):
            with self.assertRaises(RiskControlError):
                client._start()
        tab.call.assert_not_called()
        tab.evaluate.assert_not_called()

    def test_cli_lock_recovers_idle_orphan_without_refresh_or_business_request(self):
        import xiaohongshu as x
        metadata = [{'type': 'page', 'url': 'https://www.xiaohongshu.com/search_result',
                     'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
        with tempfile.TemporaryDirectory() as directory:
            for previous, released, expected in [
                ({'version': 1, 'busy': False, 'stopped': False}, True, 0),
                ({'version': 1, 'busy': True, 'stopped': False}, True, 1),
                ({'version': 2, 'busy': False, 'stopped': False}, True, 1),
                ({'version': 1, 'busy': False, 'stopped': False}, False, 1),
            ]:
                client, tab = AsyncBrowserRPCClient(), Mock()
                tab.evaluate.side_effect = [previous, released, True, {'ready': True}]
                held = []
                def start():
                    held.append(x._collector_lease)
                    self.assertFalse(held[-1].closed)
                    client._start()
                    return 0
                with patch.object(x, 'STATE_DIR', Path(directory)), patch.object(x, 'main', side_effect=start), \
                     patch('sys.argv', ['xiaohongshu.py']), patch.object(x.signal, 'signal'), \
                     patch.object(client, '_list_tabs', return_value=metadata), \
                     patch.object(client, '_check_page'), patch.object(x, 'CDPTab', return_value=tab):
                    self.assertEqual(x.run_cli(), expected)
                self.assertIsNone(x._collector_lease)
                self.assertTrue(held[0].closed)
                tab.call.assert_not_called()  # 无Page.reload、Page.navigate或业务RPC。
                tab.navigate.assert_not_called()
                self.assertEqual(client._owns_bridge, expected == 0)
                if previous['version'] == 1 and not previous['busy']:
                    release = tab.evaluate.call_args_list[1].args[0]
                    self.assertIn('|| !true', release)
                    self.assertIn('(r.uncertain === undefined ? r.stopped : r.uncertain) !== false', release)
                client._close_browser()

    def test_uncertain_or_unclassified_legacy_bridge_cannot_be_reclaimed(self):
        metadata = [{'type': 'page', 'url': 'https://www.xiaohongshu.com/search_result',
                     'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
        for previous in ({'version': 1, 'busy': False, 'stopped': True, 'uncertain': True},
                         {'version': 1, 'busy': False, 'stopped': False, 'uncertain': True},
                         {'version': 1, 'busy': False, 'stopped': True}):
            client, tab = AsyncBrowserRPCClient(), Mock()
            tab.evaluate.side_effect = [previous, True, True, {'ready': True}]
            with self.subTest(previous=previous), patch.object(client, '_list_tabs', return_value=metadata), \
                 patch.object(client, '_check_page'), patch('xiaohongshu.CDPTab', return_value=tab), \
                 patch('xiaohongshu._collector_lease', SimpleNamespace(closed=False)):
                with self.assertRaisesRegex(RuntimeError, '不接管'):
                    client._start()
            self.assertFalse(client._owns_bridge)
            tab.navigate.assert_not_called()
            tab.call.assert_not_called()

    def test_timeout_or_invalid_endpoint_does_not_launch_browser(self):
        for error in (URLError(TimeoutError()), TimeoutError(), ValueError('invalid JSON'),
                      HTTPError('http://127.0.0.1:9223/json/list', 500, 'bad endpoint', {}, None)):
            client = AsyncBrowserRPCClient()
            with patch.object(client, '_list_tabs', side_effect=error), \
                 patch.object(client, '_launch_headed') as launch:
                with self.assertRaises((RuntimeError, TimeoutError, ValueError)):
                    client._start()
                launch.assert_not_called()

    def test_auto_opened_login_page_stops_before_rpc(self):
        async def check():
            client = AsyncBrowserRPCClient()
            tab = Mock()
            blank = [{'type': 'page', 'url': 'about:blank',
                      'webSocketDebuggerUrl': 'ws://127.0.0.1:9223/devtools/page/test'}]
            with patch.object(client, '_list_tabs', side_effect=[URLError(ConnectionRefusedError()), blank]), \
                 patch.object(client, '_launch_headed') as launch, \
                 patch.object(client, '_check_page', side_effect=RiskControlError('需要人工登录')), \
                 patch.object(client, '_invoke') as invoke, patch('xiaohongshu.CDPTab', return_value=tab):
                with self.assertRaises(RiskControlError):
                    await client.request('POST', SEARCH, {'keyword': 'test', 'page': 1})
                with self.assertRaisesRegex(RuntimeError, '本轮已停止'):
                    await client.request('POST', SEARCH, {'keyword': 'test', 'page': 1})
                launch.assert_called_once()
                invoke.assert_not_called()
        asyncio.run(check())

    def test_launcher_is_headed_and_reuses_profile_without_killing_it(self):
        client = AsyncBrowserRPCClient()
        with tempfile.TemporaryDirectory() as directory, patch('xiaohongshu.STATE_DIR', Path(directory)), \
             patch('xiaohongshu._find_browser_executable', return_value='chrome'), \
             patch('xiaohongshu.browser_proxy_args', return_value=[]), \
             patch('xiaohongshu.subprocess.Popen') as launch, \
             patch('xiaohongshu._wait_for_cdp') as ready:
            client._launch_headed()
            args = launch.call_args.args[0]
            self.assertFalse(any(a.startswith('--headless') for a in args))
            self.assertIn('--remote-debugging-port=9223', args)
            self.assertIn('--remote-debugging-address=127.0.0.1', args)
            self.assertIn(f'--user-data-dir={Path(directory) / "browser"}', args)
            self.assertEqual(args[-1], 'about:blank')
            ready.assert_called_once_with(9223, process=launch.return_value)
            client._close_browser()
            launch.return_value.terminate.assert_not_called()
            launch.return_value.kill.assert_not_called()

    def test_close_only_disconnects_owned_socket(self):
        client = AsyncBrowserRPCClient()
        tab = Mock()
        client.search_tab = tab
        client._close_browser()
        tab.close.assert_called_once()
        tab.call.assert_not_called()  # Unowned bridge and browser must not be touched.
        client.search_tab = tab
        client._owns_bridge = True
        client._close_browser()
        self.assertEqual(tab.call.call_args.args[0], 'Runtime.evaluate')
        self.assertIn('r.stopped = true', tab.call.call_args.args[1]['expression'])
        self.assertNotIn('Browser.close', str(tab.mock_calls))

    def test_rpc_resume_keeps_csv_index_and_old_cdp_cache(self):
        import httpx
        from xiaohongshu import cmd_all_notes
        from xiaohongshu import write_notes_csv

        async def post(path, body):
            self.assertEqual(path, SEARCH)
            return httpx.Response(200, json={'code': 0, 'data': {
                'has_more': False, 'items': [
                    {'id': n, 'xsec_token': 'test', 'note_card': {'type': 'normal'}}
                    for n in ('old', 'excluded', 'new', 'pending')
                ]}})

        async def collect(client, note_id, token, **kwargs):
            if note_id == 'pending':
                raise TransientRiskError('offline limit fixture')
            self.assertEqual(note_id, 'new')  # Saved/excluded IDs must not request detail.
            return {'status': 'success', 'content': {'title': 'new', 'time': '2025-03-01'}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / 'requirements.md'
            doc.write_text('fixture', encoding='utf-8')
            private = root / 'runtime/xiaohongshu'
            private.mkdir(parents=True)
            state = private / 'full_run_state.jsonl'
            state.write_text('{"note_id":"old"}\n{"skip_note_id":"excluded"}\n', encoding='utf-8')
            cdp = private / 'candidate_pages_cdp.json'
            cdp.write_text('{"preserved":true}', encoding='utf-8')
            csv_path = root / 'output/csv/小红书_双减_笔记.csv'
            csv_path.parent.mkdir(parents=True)
            with patch('xiaohongshu.OUTPUT', root / 'output/csv'):
                write_notes_csv([{'note_id': 'old', 'status': 'success', 'comments_complete': True,
                                  'content': {'title': 'old', 'time': '2025-03-01'},
                                  'propagation': dict(liked=0, collected=0, comment=0, shared=0)}], csv_path)
            client = Mock()
            client.post = AsyncMock(side_effect=post)
            client.close = AsyncMock()
            with patch('xiaohongshu.ROOT', root), patch('xiaohongshu.OUTPUT', root / 'output/csv'), \
                 patch('xiaohongshu.STATE_DIR', private), patch('xiaohongshu.load_requirement_keywords', return_value=['双减']), \
                 patch('xiaohongshu.AsyncBrowserRPCClient', return_value=client) as make, \
                 patch('xiaohongshu.async_collect_note', side_effect=collect), \
                 patch('xiaohongshu.async_collect_comments', new_callable=AsyncMock):
                result = asyncio.run(cmd_all_notes(SimpleNamespace(requirements=doc, transport='rpc', rpc_port=9224,
                                                                  no_year_queries=True)))
            self.assertEqual(result, 1)
            make.assert_called_once_with(port=9224)
            client.set_cookies.assert_not_called()
            client.close.assert_awaited_once()
            self.assertEqual(cdp.read_text(encoding='utf-8'), '{"preserved":true}')
            self.assertTrue((private / 'candidate_pages_rpc.json').exists())
            events = [json.loads(line) for line in state.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([e['note_id'] for e in events if 'note_id' in e], ['old', 'new'])
            self.assertEqual(events[-1]['retry_keyword'], '双减')
            self.assertFalse(any('keyword' in e for e in events))
            with csv_path.open(encoding='utf-8-sig', newline='') as f:
                self.assertEqual([r['笔记标题'] for r in csv.DictReader(f)], ['old', 'new'])

    def test_completed_search_and_old_details_never_refetch_for_pending_comments(self):
        import xiaohongshu as x
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'runtime'
            private.mkdir()
            out = root / 'csv/notes.csv'
            with patch.object(x, 'OUTPUT', root / 'csv'), patch.object(x, 'STATE_DIR', private):
                out = x.OUTPUT / '小红书_双减_笔记.csv'
                x.write_notes_csv([{'note_id': 'old', 'status': 'success', 'comments_complete': True,
                                   'content': {'title': 'old', 'time': '2023-01-01'}}], out)
                (private / 'full_run_state.jsonl').write_text(
                    '{"note_id":"old"}\n{"keyword":"双减"}\n{"run_complete":true}\n', encoding='utf-8')
                cache = private / 'candidate_pages_rpc.json'
                cache.write_text(json.dumps({'keyword': '双减', 'search_id': 'saved-search', 'pages': [
                    {'has_more': False, 'notes': [{'note_id': 'old', 'xsec_token': 'fixture'}]}]}), encoding='utf-8')
                before = cache.read_bytes()
                client = Mock(post=AsyncMock(), close=AsyncMock())
                args = SimpleNamespace(requirements=None, no_year_queries=True)
                with patch.object(x, 'load_requirement_keywords', return_value=['双减']), \
                     patch.object(x, 'AsyncBrowserRPCClient', return_value=client), \
                     patch.object(x, 'async_collect_note', new_callable=AsyncMock) as detail:
                    self.assertEqual(asyncio.run(x.cmd_all_notes(args)), 0)
                    client.post.assert_not_called()
                    detail.assert_not_called()  # 缺互动数也不能让普通增量重采旧详情。
                    with closing(x.CommentStore(out, '小红书')) as store:
                        with store.db:
                            store.db.execute("UPDATE posts SET done=0,cursor='saved-cursor' WHERE id='old'")
                    async def resume(_client, ident, token, store, *args, **kwargs):
                        self.assertEqual((ident, token, store.state(ident)['cursor']), ('old', 'fixture', 'saved-cursor'))
                        store.save_page(ident, [], '', True)
                    with patch.object(x, 'async_collect_comments', side_effect=resume) as comments:
                        self.assertEqual(asyncio.run(x.cmd_all_notes(args)), 0)
                    comments.assert_awaited_once()
                    client.post.assert_not_called()
                    detail.assert_not_called()
                    self.assertEqual(cache.read_bytes(), before)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for offline bridge fixtures')
    def test_bridge_native_chain_busy_timeout_and_error_guards(self):
        fixture = r"""
            const vm = require('node:vm');
            const assert = require('node:assert/strict');
            const source = require('node:fs').readFileSync(0, 'utf8');
            const search = '/api/sns/web/v1/search/notes';
            const feed = '/api/sns/web/v1/feed';
            function page(post, timeout = false) {
                let calls = 0;
                const client = {instance: {}, interceptors: {}, post(...args) {
                    assert.equal(this, client); calls++; return post(...args);
                }, get(...args) {
                    assert.equal(this, client); calls++; return post(...args);
                }};
                // Fake webpack factories; no downloaded code is executed.
                function factory() {
                    function processSend() {}
                    function handleResData() {}
                    const transformRequestConfig = 'extractData';
                }
                const require = () => client;
                require.m = {test: factory};
                const chunks = [];
                chunks.push = chunk => chunk[2](require);
                const context = vm.createContext({window: {webpackChunkxhs_pc_web: chunks, __INITIAL_STATE__: {}},
                    document: {readyState: 'complete'},
                    location: {origin: 'https://www.xiaohongshu.com', pathname: '/search_result'},
                    crypto: {randomUUID: () => 'offline'}, clearTimeout, URLSearchParams,
                    setTimeout: timeout ? fn => setTimeout(fn, 0) : setTimeout,
                    fetch: () => {throw Error('fetch forbidden');},
                    XMLHttpRequest: function() {throw Error('XHR forbidden');}});
                return {context, client, require, count: () => calls,
                        install: () => vm.runInContext(source, context),
                        rpc: () => context.window.__xhsCollectorRPC};
            }
            (async () => {
                const success = {code: 0, success: true, data: {items: [], has_more: false}};
                const p = page(async (path, body, options) => {
                    assert.equal(path, search); assert.equal(body.page, 1);
                    assert.equal(options.extractData, false); assert.equal(options.transform, false);
                    return success;
                });
                assert.equal(p.install().ready, true);
                assert.equal(p.install().ready, false); // No second collector on the same page.
                let response = await p.rpc().call(search, {keyword: 'test', page: 1});
                assert.equal(response.payload, success);
                await p.rpc().call('/api/sns/web/v1/note/like', {});
                await p.rpc().call(search, {keyword: '', page: 1});
                await p.rpc().call(feed, {source_note_id: 'n'});
                assert.equal(p.count(), 1);

                const commentPath = '/api/sns/web/v2/comment/page?note_id=n&xsec_token=test&cursor=next';
                const comments = page(async (path, options) => {
                    assert.equal(path, commentPath);
                    assert.equal(options.extractData, false);
                    assert.equal(options.transform, false);
                    return {code: 0, data: {comments: [{id: 'c'}], cursor: '', has_more: false}};
                });
                comments.install();
                response = await comments.rpc().call(commentPath, null);
                assert.equal(response.payload.data.comments[0].id, 'c');
                await comments.rpc().call('/api/sns/web/v2/comment/page?note_id=n', null);
                assert.equal(comments.count(), 1);

                let release;
                const busy = page(() => new Promise(r => {release = r;})); busy.install();
                const first = busy.rpc().call(search, {keyword: 'test', page: 1});
                assert.equal((await busy.rpc().call(search, {keyword: 'test', page: 2})).ok, false);
                assert.equal(busy.count(), 1); release(success); await first;

                for (const timeout of [false, true]) {
                    const failed = page(() => timeout ? new Promise(() => {}) : Promise.reject({
                        status: 461, code: 300013, message: 'secret-token', config: {cookie: 'secret-token'}
                    }), timeout);
                    failed.install();
                    response = await failed.rpc().call(feed, {source_note_id: 'n', xsec_token: 'test'});
                    assert.equal(response.ok, false);
                    assert.equal(response.reason, timeout ? 'timeout_no_retry' : 'native_request_failed');
                    assert.equal(JSON.stringify(response).includes('secret-token'), false);
                    assert.equal(failed.rpc().stopped, true);
                    assert.equal(failed.rpc().uncertain, true);
                    await failed.rpc().call(feed, {source_note_id: 'n', xsec_token: 'test'});
                    assert.equal(failed.count(), 1);
                }
                const bad = page(async () => ({code: 300013, success: false})); bad.install();
                await bad.rpc().call(search, {keyword: 'test', page: 1});
                assert.equal(bad.rpc().stopped, true);
                const missing = page(async () => success); missing.require.m = {};
                assert.equal(missing.install().ready, false); assert.equal(missing.count(), 0);
                const wrong = page(async () => success); wrong.context.location.origin = 'https://example.com';
                assert.equal(wrong.install().ready, false); assert.equal(wrong.count(), 0);
                process.stdout.write('OK');
            })().catch(e => {console.error(e); process.exitCode = 1;});
        """
        result = subprocess.run([shutil.which('node'), '-e', fixture],
            input=BRIDGE_JS, text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout, 'OK')



"""python -m unittest test_start_all -v：只启动本地假任务，不访问网站。"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

import start_all as launcher
import douyin as browser


class LauncherTest(unittest.TestCase):
    def setUp(self):
        # 故意失败的本地假任务不弹真实窗口、不修改测试者的控制台标题。
        notice = patch.object(launcher, "desktop_notice")
        notice.start()
        self.addCleanup(notice.stop)

    def test_full_commands_and_check_only(self):
        self.assertEqual(launcher.arguments("douyin", 9222, 9223), ["--browser-rpc", "9222"])
        self.assertEqual(launcher.arguments("kuaishou", 9222, 9223), ["--rpc-port", "9224"])
        self.assertEqual(launcher.arguments("kuaishou", 9222, 9223, ks_port=9334), ["--rpc-port", "9334"])
        self.assertEqual(launcher.arguments("xiaohongshu", 9222, 9223), ["--transport", "rpc", "--rpc-port", "9223"])
        with patch.object(launcher, "check_project") as check, patch.object(launcher, "supervise") as run:
            self.assertEqual(launcher.main(["--check"]), 0)
            self.assertEqual([c.args[0] for c in check.call_args_list], list(launcher.PROJECTS))
            run.assert_not_called()

    def test_run_logs_are_grouped_and_latest_does_not_overwrite_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'ROOT', Path(directory)), \
             patch.object(launcher, 'check_project'), patch.object(launcher.signal, 'signal'), \
             patch.object(launcher, 'supervise', return_value=0) as supervise:
            root = Path(directory)
            latest = root / 'logs/最新日志.txt'
            for stamp in ('20260102_030405', '20260102_040506'):
                with patch.object(launcher.time, 'strftime', return_value=stamp):
                    self.assertEqual(launcher.main(['--only', 'douyin']), 0)
                logdir = supervise.call_args.args[1]
                self.assertEqual(logdir, root / 'logs/runs' / (stamp + f'_{os.getpid()}'))
                self.assertTrue(logdir.is_dir())
                self.assertEqual(root / latest.read_text(encoding='utf-8').strip(), logdir)
                (logdir / 'douyin.log').write_text(stamp, encoding='utf-8')
            # 同一目录冲突时拒绝覆盖旧日志；离线检查也不生成空的运行目录。
            with patch.object(launcher.time, 'strftime', return_value='20260102_030405'):
                self.assertEqual(launcher.main(['--only', 'douyin']), 1)
            self.assertEqual(supervise.call_count, 2)
            self.assertEqual(launcher.main(['--check']), 0)
            self.assertEqual(len(list((root / 'logs/runs').iterdir())), 2)
            self.assertEqual(root / latest.read_text(encoding='utf-8').strip(), logdir)
            self.assertEqual({p.read_text(encoding='utf-8') for p in (root / 'logs/runs').glob('*/douyin.log')},
                             {'20260102_030405', '20260102_040506'})
            self.assertTrue((root / 'logs/collector.lock').is_file())
            self.assertFalse(latest.with_suffix('.tmp').exists())

    def test_independent_jobs_logs_and_failed_spawn(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher, "ROOT", Path(tmp)):
            root = Path(tmp)
            jobs = {name: [sys.executable, "-u", "-c", f"import time; print({name!r}); time.sleep(0.3); raise SystemExit({rc})"]
                    for name, rc in zip(launcher.PROJECTS, [1, 0, 2])}
            self.assertEqual(launcher.supervise(jobs, root, threading.Event()), 1)
            for name in jobs:
                self.assertIn(name, (root / (name + ".log")).read_text(encoding="utf-8"))
            # 一个平台可执行文件不存在，不妨碍另一个平台完成。
            self.assertEqual(launcher.supervise({"douyin": [str(root / "missing-python")],
                                                "kuaishou": jobs["kuaishou"]}, root, threading.Event()), 1)
            self.assertEqual(launcher.supervise({"xiaohongshu": jobs["xiaohongshu"]}, root, threading.Event()), 2)
            self.assertEqual(launcher.supervise({"kuaishou": jobs["kuaishou"]}, root, threading.Event()), 0)
            stopped = threading.Event()
            stopped.set()
            with patch.object(launcher.subprocess, "Popen") as spawn:
                self.assertEqual(launcher.supervise(jobs, root, stopped), 130)
                spawn.assert_not_called()

    def test_forward_logs_buffers_split_utf8_and_final_line(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new_callable=io.StringIO) as console:
            path = Path(directory) / 'data.log'
            pending = {}
            raw = '中文🙂'.encode('utf-8')
            with path.open('wb') as writer, path.open('rb') as reader:
                readers = {'douyin': reader}
                writer.write(raw[:1]); writer.flush()
                launcher.forward_logs(readers, pending)
                self.assertEqual(console.getvalue(), '')
                writer.write(raw[1:] + b'\r\n' + '最后一行'.encode('utf-8')); writer.flush()
                launcher.forward_logs(readers, pending)
                self.assertEqual(console.getvalue(), '[抖音] 中文🙂\n')
                launcher.forward_logs(readers, pending, final=True)
                launcher.forward_logs(readers, pending, final=True)
                self.assertEqual(console.getvalue(), '[抖音] 中文🙂\n[抖音] 最后一行\n')

    def test_console_streams_before_exit_and_keeps_stderr_and_final_line(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'ROOT', Path(directory)):
            root, displayed = Path(directory), []
            def display(text, **kwargs):
                displayed.append(text)
                if text == '[小红书] 测试采集数据':
                    (root / 'ack').touch()
            code = """import time, sys
from pathlib import Path
print('测试采集数据', flush=True)
print('测试错误', file=sys.stderr, flush=True)
end = time.monotonic() + 5
while not Path('ack').exists() and time.monotonic() < end:
    time.sleep(0.02)
print('末行', end='', flush=True)
raise SystemExit(0 if Path('ack').exists() else 7)
"""
            with patch.object(launcher, 'print', side_effect=display, create=True):
                self.assertEqual(launcher.supervise({'xiaohongshu': [sys.executable, '-u', '-c', code]}, root, threading.Event()), 0)
            for text in ('测试采集数据', '测试错误', '末行'):
                self.assertEqual(displayed.count('[小红书] ' + text), 1)
            self.assertEqual((root / 'xiaohongshu.log').read_text(encoding='utf-8'), '测试采集数据\n测试错误\n末行')

    def test_lock_and_release_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher, "ROOT", Path(tmp)):
            code = "import start_all as s; from pathlib import Path; s.ROOT=Path(" + repr(tmp) + "); s.workspace_lock().close()"
            with launcher.workspace_lock():
                child = subprocess.run([sys.executable, "-c", code], capture_output=True, env=launcher.ENV, timeout=10)
                self.assertNotEqual(child.returncode, 0)
            child = subprocess.run([sys.executable, "-c", code], capture_output=True, env=launcher.ENV, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr.decode("utf-8", errors="replace"))

    def test_worker_directory_arguments_and_graceful_finally(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "collectors"
            project.mkdir()
            (project / "helper.py").write_text("VALUE=42", encoding="utf-8")
            (project / "kuaishou.py").write_text("""
import signal, sys
from pathlib import Path
from helper import VALUE
assert VALUE == 42 and Path.cwd() == Path(__file__).resolve().parents[1]
assert sys.argv[1:] == ['--rpc-port', '9224']
try:
    signal.raise_signal(getattr(signal, 'SIGBREAK', signal.SIGTERM))
finally:
    Path('committed.txt').write_text('saved')
""", encoding="utf-8")
            code = (f"import start_all as s; from pathlib import Path; s.ROOT=Path({tmp!r}); "
                    "raise SystemExit(s.run_worker('kuaishou',9222,9223))")
            child = subprocess.run([sys.executable, "-c", code], capture_output=True, env=launcher.ENV, timeout=10)
            self.assertEqual(child.returncode, 130, child.stderr.decode("utf-8", errors="replace"))
            self.assertEqual((Path(tmp) / "committed.txt").read_text(), "saved")

    def test_stop_timeout_does_not_leave_collector_running(self):
        code = ("import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                "signal.signal(getattr(signal,'SIGBREAK',signal.SIGTERM),signal.SIG_IGN); "
                "print('ready',flush=True); time.sleep(20)")
        python = launcher.project_python()
        with subprocess.Popen([python, "-u", "-c", code], stdout=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), start_new_session=os.name != "nt") as child:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            launcher.stop_processes([child], grace=0.2)
            self.assertIsNotNone(child.poll())

    def test_douyin_browser_reuse_and_single_launch(self):
        with patch.object(browser, "cdp_ready", return_value=True), patch.object(browser.subprocess, "Popen") as spawn:
            browser.prepare_douyin_browser(9222)
            spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp, patch.object(browser, "STATE_DIR", Path(tmp)):
            executable = Path(tmp) / "browser.exe"
            executable.touch()
            with patch.dict(os.environ, {"CHROME_PATH": str(executable)}), \
                    patch.object(browser, "cdp_ready", side_effect=[False, True]), \
                    patch.object(browser, "getproxies", return_value={}), \
                    patch.object(browser, "proxy_bypass", return_value=False), \
                    patch.object(browser.subprocess, "Popen") as spawn:
                browser.prepare_douyin_browser(9222)
                spawn.assert_called_once()
                command = spawn.call_args.args[0]
                self.assertIn("--remote-debugging-address=127.0.0.1", command)
                self.assertIn("--user-data-dir=" + str(Path(tmp) / "browser"), command)

    def test_cdp_checks_never_treat_invalid_listener_as_absent(self):
        with patch.object(browser, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError(ConnectionRefusedError())
            self.assertFalse(browser.cdp_ready(9222))
            self.assertGreaterEqual(opener.return_value.open.call_args.kwargs["timeout"], 5)
            opener.return_value.open.side_effect = URLError(TimeoutError())
            with self.assertRaises(RuntimeError):
                browser.cdp_ready(9222)
            opener.return_value.open.side_effect = None
            for url, valid in [("ws://127.0.0.1:9222/devtools/browser/a", True),
                               ("ws://example.com:9222/a", False), ("ws://user:secret@127.0.0.1:9222/a", False)]:
                opener.return_value.open.return_value = io.BytesIO(json.dumps({"webSocketDebuggerUrl": url}).encode())
                if valid:
                    self.assertTrue(browser.cdp_ready(9222))
                else:
                    with self.assertRaises(RuntimeError):
                        browser.cdp_ready(9222)



class DouyinTest(unittest.TestCase):
    def test_browser_resume_sends_saved_cursor_once_without_navigation(self):
        from douyin import BrowserRPC
        rpc = BrowserRPC.__new__(BrowserRPC)
        rpc.page, rpc._pace = Mock(), Mock()
        body = {'status_code': 0, 'cursor': 3735, 'has_more': 1, 'comments': []}
        rpc.page.evaluate.return_value = {'http': 200, 'type': 'application/json', 'body': body}
        self.assertEqual(rpc._comments('video', 3725), body)
        self.assertEqual(rpc.page.evaluate.call_args.args[1]['query']['cursor'], '3725')
        rpc._search_page('双减', 460)
        self.assertEqual(rpc.page.evaluate.call_args.args[1]['query']['offset'], '460')
        rpc._detail('new-id')
        self.assertEqual(rpc.page.evaluate.call_count, 3)
        rpc.page.goto.assert_not_called()
        rpc.page.mouse.wheel.assert_not_called()
        for result in ({'error': 'AbortError'}, {'http': 429, 'type': 'application/json'},
                       {'http': 200, 'type': 'text/html'},
                       {'http': 200, 'type': 'application/json', 'body': {'status_code': 2483}},
                       {'http': 200, 'type': 'application/json', 'body': {'status_code': False}}):
            rpc.page.evaluate.reset_mock()
            rpc.page.evaluate.return_value = result
            with self.assertRaises(RuntimeError):
                rpc._comments('video', 3725)
            self.assertEqual(rpc.page.evaluate.call_count, 1)

    def test_browser_navigation_during_request_stops_without_replaying_saved_cursor(self):
        from douyin import BrowserRPC, collect_comments
        from playwright.sync_api import Error as PlaywrightError
        import start_all
        rpc = BrowserRPC.__new__(BrowserRPC)
        rpc.page, rpc._pace = Mock(), Mock()
        rpc.page.evaluate.side_effect = PlaywrightError(
            'Page.evaluate: Execution context was destroyed, most likely because of a navigation')
        with tempfile.TemporaryDirectory() as directory, \
             closing(CommentStore(Path(directory) / 'posts.csv', '抖音', memory=True)) as store:
            store.register('video')
            store.save_page('video', [comment(1)], '3725', False)
            with self.assertRaisesRegex(RuntimeError, '结果未知；已停止且不重发，原断点保留') as caught:
                collect_comments(rpc.fetch, None, store, 'video', max_pages=1)
            self.assertEqual((store.state('video')['cursor'], store.state('video')['pages']), ('3725', 1))
            self.assertFalse(store.state('video')['done'])
            self.assertIsNone(start_all.retry_wait('douyin', 1, str(caught.exception), 300))
        self.assertEqual(rpc.page.evaluate.call_count, 1)
        self.assertEqual(rpc.page.evaluate.call_args.args[1]['query']['cursor'], '3725')
        rpc.page.goto.assert_not_called()

    def test_browser_reuses_and_keeps_loaded_page(self):
        from douyin import BrowserRPC
        page = Mock(url='https://www.douyin.com/video/old')
        context = Mock(pages=[page])
        pw = Mock()
        pw.chromium.connect_over_cdp.return_value.contexts = [context]
        with patch('douyin.sync_playwright') as start:
            start.return_value.start.return_value = pw
            rpc = BrowserRPC()
            self.assertIs(rpc.page, page)
            rpc.close()
        context.new_page.assert_not_called()
        page.goto.assert_not_called()
        page.close.assert_not_called()
        pw.stop.assert_called_once()

    def test_full_cli_legacy_merge_and_interrupted_search_resume(self):
        import douyin as m
        from datetime import datetime
        calls = []
        def video(pid, day):
            return {"aweme_id": pid, "create_time": int(datetime.fromisoformat(day + "T12:00:00+08:00").timestamp()),
                    "desc": "@讨论,\n双减", "author": {"nickname": "作者"}, "ip_label": "北京",
                    "video": {"duration": 1000}, "statistics": dict(digg_count=0, comment_count=0, collect_count=0, share_count=0)}
        def fetch(kind, args, cookie):
            if kind == "comment_list":
                return {"status_code": 0, "comments": [], "cursor": 0, "has_more": 0, "total": 0}
            self.assertEqual(kind, "aweme_search")
            offset = int(args[args.index("--offset") + 1])
            calls.append(offset)
            return {"has_more": offset == 0, "cursor": 20 if offset == 0 else 30,
                    "data": [video("start", "2021-07-24"), video("excluded", "2021-07-23")] if offset == 0
                            else [video("end", "2026-07-24"), video("future", "2026-07-25")]}
        client = Mock(fetch=fetch)
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "OUTPUT", Path(directory) / "csv"), \
                patch.object(m, "DEFAULT_KEYWORDS", ["双减"]), patch.object(m, "prepare_douyin_browser"), \
                patch.object(m, "BrowserRPC", return_value=client), patch("sys.argv", ["douyin.py", "--no-year-queries"]):
            old = m.OUTPUT / "douyin_legacy.csv"
            legacy = dict(zip(m.COLUMNS, m._row(video("legacy", "2023-01-01"), "旧词")))
            m.atomic_csv(old, m.LEGACY_COLUMNS, [legacy])
            before = old.read_bytes()
            self.assertEqual(m.main(), 0)
            target = m.OUTPUT / "douyin.csv"
            self.assertEqual([r["内容ID"] for r in rows(target)], ["legacy", "start", "end"])
            self.assertEqual((m.OUTPUT.parent / "backups/douyin_legacy.csv.schema-v1.bak").read_bytes(), before)
            self.assertTrue((m.OUTPUT.parent / "backups/douyin_legacy.csv.bak").exists())
            self.assertEqual(calls, [0, 20])
            self.assertEqual(m.main(), 0)
            self.assertEqual(calls, [0, 20])
            self.assertEqual(client.close.call_count, 2)
            cursor = m.OUTPUT.parent / "state/douyin_progress.json"
            self.assertTrue(json.loads(cursor.read_text(encoding="utf-8"))["双减"]["done"])
            interrupted_calls = []
            def interrupted(kind, args, cookie):
                offset = int(args[args.index("--offset") + 1])
                interrupted_calls.append(offset)
                if offset:
                    raise RuntimeError("offline interrupted page")
                return {"has_more": True, "cursor": 20, "data": [video("new", "2023-01-01")]}
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                m._collect_posts(["继续"], None, START, END, fetch=interrupted, source="browser_rpc")
            self.assertEqual(interrupted_calls, [0, 20])
            self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["继续"]["offset"], 20)
            def resume(kind, args, cookie):
                self.assertEqual(args[args.index("--offset") + 1], "20")
                return {"has_more": False, "cursor": 30, "data": [video("new", "2023-01-01")]}
            m._collect_posts(["继续"], None, START, END, fetch=resume, source="browser_rpc")
            self.assertEqual(len(rows(target)), 4)
            self.assertTrue(json.loads(cursor.read_text(encoding="utf-8"))["继续"]["done"])


class SingleFileTest(unittest.TestCase):
    def test_output_subdirectories_preserve_comment_cursor_and_backups(self):
        import importlib
        for name, platform in (("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书")):
            m = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as tmp, patch.object(m, "OUTPUT", Path(tmp) / "csv"):
                root = Path(tmp)
                target = m.OUTPUT / "nested/posts.csv"
                post = m.canonical_post(platform, {"内容ID": "post", "发布时间": "2023-01-01"})
                m.atomic_csv(target, m.POST_FIELDS, [post])
                store = m.CommentStore(target, platform)
                try:
                    store.register("post")
                    store.save_page("post", [comment(1)], "next", False)
                    store.export()
                    store.report()
                finally:
                    store.close()
                store = m.CommentStore(target, platform)
                try:
                    self.assertEqual(store.state("post")["cursor"], "next")
                    self.assertEqual(store.state("post")["pages"], 1)
                    self.assertFalse(store.state("post")["done"])
                    self.assertEqual(len(store.top("post")), 1)
                finally:
                    store.close()
                self.assertTrue((root / "state/nested/posts.comments.sqlite3").is_file())
                self.assertTrue((root / "reports/nested/posts_覆盖报告.json").is_file())
                legacy = m.OUTPUT / "nested/legacy.csv"
                m.atomic_csv(legacy, ["old_id"], [{"old_id": "post"}])
                before = legacy.read_bytes()
                m.upgrade_csv(legacy, ["old_id"], m.POST_FIELDS, lambda row: post)
                self.assertEqual((root / "backups/nested/legacy.csv.schema-v1.bak").read_bytes(), before)
                self.assertTrue(all(p.suffix == ".csv" for p in m.OUTPUT.rglob("*") if p.is_file()))
                self.assertEqual({p.name for p in root.iterdir()}, {"csv", "state", "reports", "backups"})

    def test_standalone_copy_has_no_project_dependencies(self):
        import ast
        allowed = sys.stdlib_module_names | {"requests", "httpx", "websocket", "playwright"}
        for name in ("douyin", "kuaishou", "xiaohongshu"):
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "collectors" / (name + ".py")
                target.parent.mkdir()
                shutil.copy2(ROOT / "collectors" / target.name, target)
                tree = ast.parse(target.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertTrue({a.name.split('.')[0] for a in node.names} <= allowed)
                    elif isinstance(node, ast.ImportFrom):
                        self.assertIn(node.module.split('.')[0], allowed)
                env = {**launcher.ENV, "KS_COOKIE": "offline=fixture", "PYTHONDONTWRITEBYTECODE": "1"}
                child = subprocess.run([sys.executable, str(target), "--check"], cwd=tmp,
                                       env=env, text=True, encoding="utf-8", capture_output=True, timeout=20)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertFalse((Path(tmp) / "output").exists())
                self.assertFalse((Path(tmp) / "runtime").exists())
                probe = (f"import {name} as m; from pathlib import Path; "
                         f"assert m.ROOT == Path({tmp!r}); "
                         "assert m.OUTPUT == m.ROOT / 'output/csv'; "
                         f"assert m.STATE_DIR == m.ROOT / 'runtime/{name}'")
                child = subprocess.run([sys.executable, "-c", probe], cwd=target.parent,
                                       env=env, text=True, capture_output=True, timeout=20)
                self.assertEqual(child.returncode, 0, child.stderr)
                if name == "kuaishou":
                    self.assertNotIn('SIGNER_JS', target.read_text(encoding='utf-8'))
                    self.assertNotIn('requests.Session', target.read_text(encoding='utf-8'))

    def test_common_contracts_and_built_in_keywords_match(self):
        import ast
        import importlib
        modules = [importlib.import_module(n) for n in ("douyin", "kuaishou", "xiaohongshu")]
        names = {"exact_count", "timestamp", "canonical_post", "CommentStore", "upgrade_csv", "atomic_csv", "search_queries", "shard_queries", "sidecar_path",
                 "export_csv", "present", "merge_post", "dataset_header", "read_posts", "log_saved_row", "comment_jobs"}
        common = []
        for module in modules:
            self.assertEqual(len(module.DEFAULT_KEYWORDS), 134)
            self.assertEqual(len(module.search_queries(module.DEFAULT_KEYWORDS)), 938)
            self.assertEqual(module.OUTPUT, ROOT / "output/csv")
            self.assertEqual((module.START, module.END), (START, END))
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            common.append({n.name: ast.dump(n, include_attributes=False) for n in tree.body if getattr(n, 'name', None) in names})
        self.assertEqual(common[0], common[1])
        self.assertEqual(common[0], common[2])
        self.assertEqual(modules[0].DEFAULT_KEYWORDS, modules[1].DEFAULT_KEYWORDS)
        self.assertEqual(modules[1].DEFAULT_KEYWORDS, modules[2].DEFAULT_KEYWORDS)
        for module in modules:
            full = module.search_queries(module.DEFAULT_KEYWORDS)
            forward, reverse = module.shard_queries(full, "forward"), module.shard_queries(full, "reverse")
            self.assertEqual((len(forward), len(reverse)), (469, 469))
            self.assertEqual(forward + list(reversed(reverse)), full)
            self.assertFalse(set(forward) & set(reverse))
            with self.assertRaisesRegex(ValueError, "词表已变化"):
                module.shard_queries(full[:10], "reverse")
        import start_all
        start_all.validate_scope()  # 还须与用户原需求文档逐项一致，而非仅三个脚本互相一致。

    def test_csv_snapshot_skips_identical_replace_and_retries_local_sharing_only(self):
        import importlib
        for name in ("douyin", "kuaishou", "xiaohongshu"):
            module = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "snapshot.csv"
                module.atomic_csv(path, ["id"], [{"id": "old"}])
                original = path.read_bytes()
                with patch.object(module.os, "replace", side_effect=AssertionError("identical snapshot was replaced")):
                    module.atomic_csv(path, ["id"], [{"id": "old"}])
                self.assertEqual(path.read_bytes(), original)
                real_replace = os.replace
                attempts = []

                def transient(source, target):
                    attempts.append(1)
                    if len(attempts) <= 6:
                        raise PermissionError(13, "temporary sharing violation")
                    return real_replace(source, target)

                with patch.object(module.os, "replace", side_effect=transient), \
                     patch.object(module.time, "sleep") as wait, patch.object(module.logger, "warning") as warning:
                    module.atomic_csv(path, ["id"], [{"id": "new"}])
                    self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 2, 4, 8, 16, 60])
                    warning.assert_called_once()
                self.assertEqual(len(attempts), 7)
                changed = path.read_bytes()
                self.assertNotEqual(changed, original)
                with patch.object(module.os, "replace", side_effect=PermissionError(13, "persistent")) as replace, \
                     patch.object(module.time, "sleep") as wait, patch.object(module.logger, "warning") as warning:
                    with self.assertRaisesRegex(RuntimeError, "CSV 被占用.*系统错误码13"):
                        module.atomic_csv(path, ["id"], [{"id": "newer"}])
                    self.assertEqual(replace.call_count, 36)
                    self.assertEqual([args.args[0] for args in wait.call_args_list], [1, 2, 4, 8, 16] + [60] * 30)
                    warning.assert_called_once()
                self.assertEqual(path.read_bytes(), changed)
                self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_platform_locks_block_standalone_and_total_duplicates(self):
        import importlib
        for name in ("douyin", "kuaishou", "xiaohongshu"):
            module = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as tmp, patch.object(module, "STATE_DIR", Path(tmp)):
                command = [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT / 'collectors')!r}); import {name} as s; from pathlib import Path; s.STATE_DIR=Path({tmp!r}); s.collector_lock().close()"]
                with module.collector_lock():
                    child = subprocess.run(command, cwd=ROOT, env=launcher.ENV, capture_output=True, timeout=15)
                    self.assertNotEqual(child.returncode, 0)
                child = subprocess.run(command, cwd=ROOT, env=launcher.ENV, capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("douyin", "kuaishou", "xiaohongshu"):
        check_platform(sys.argv[1])
    else:
        unittest.main()
