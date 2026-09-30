"""离线：字段投影、旧ID补写、详情匹配、补采中断后续跑；不请求平台。"""
import asyncio
import csv
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class FieldCompletenessTest(unittest.TestCase):
    def test_zero_partial_columns_restore_and_invalid_csv(self):
        for module, platform in (("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书")):
            m = importlib.import_module(module)
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "posts.csv"
                data = [m.canonical_post(platform, {"内容ID": str(i), "发布时间": "2023-01-01",
                                                    "点赞数": 0, "采集时间": "   "}) for i in (1, 2)]
                m.export_csv(p, m.POST_FIELDS, data)
                self.assertNotIn("采集时间", rows(p)[0])
                self.assertEqual(rows(p)[0]["点赞数"], "0")
                report = json.loads((Path(tmp) / "reports/posts_字段报告.json").read_text(encoding="utf-8"))
                self.assertIn("收藏数", report["全空未导出字段"])
                restored = m.read_posts(p, m.POST_FIELDS, platform)
                restored[0] = m.merge_post(restored[0], {"内容ID": "1", "收藏数": 0, "标题": "=公式"})
                m.export_csv(p, m.POST_FIELDS, restored)
                output = rows(p)
                self.assertEqual([r["收藏数"] for r in output], ["0", ""])
                self.assertEqual(output[0]["标题"], "'=公式")
                self.assertTrue(all(any(m.present(r[k]) for r in output) for k in output[0]))
                with self.assertRaises(ValueError):
                    m.merge_post(restored[0], {"内容ID": "wrong", "标题": "错误详情"})
                good = p.read_bytes()
                p.write_text('平台,内容ID,发布时间\n' + platform + ',1\n', encoding="utf-8-sig")
                with self.assertRaises(ValueError):
                    m.read_posts(p, m.POST_FIELDS, platform)
                p.write_bytes(good)
                self.assertEqual(len(m.read_posts(p, m.POST_FIELDS, platform)), 2)

    def test_kuaishou_repairs_existing_ids_without_advancing_search(self):
        import kuaishou as m
        from test_collectors import feed, FakeClient
        with tempfile.TemporaryDirectory() as tmp:
            p, db = Path(tmp) / "posts.csv", Path(tmp) / "search.sqlite3"
            state = m.SearchState(db, p, m.START, m.END)
            try:
                state.put("video1", m.canonical_post("快手", {"内容ID": "video1", "发布时间": "2021-07-24", "评论数": 9}), True)
                state.begin(["双减"], False)
                state.checkpoint("双减", "original-cursor", "original-session", 2, [], False)
                state.sync_csv()
            finally:
                state.close()
            client = FakeClient()
            client.search = lambda *a: {"result": 1, "pcursor": "no_more", "feeds": [feed(collectCount=0), feed("new")]}
            self.assertEqual(m.repair_fields(client, ["双减"], p, db, m.START, m.END, 1), 1)
            output = rows(p)
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0]["收藏数"], "0")
            self.assertEqual(output[0]["作者ID"], "author1")
            self.assertEqual(output[0]["评论数"], "9")
            self.assertEqual(output[0]["搜索词"], "双减")
            self.assertTrue(output[0]["采集时间"])
            self.assertEqual(output[0]["视频链接"], output[0]["链接"])
            state = m.SearchState(db, p, m.START, m.END)
            try:
                self.assertEqual(state.progress("双减")[:3], ("original-cursor", "original-session", 2))
            finally:
                state.close()

    def test_douyin_detail_repair_persists_before_failure_and_resumes(self):
        import douyin as m
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "OUTPUT", Path(tmp) / "csv"):
            p = m.OUTPUT / "douyin.csv"
            m.export_csv(p, m.COLUMNS, [m.canonical_post("抖音", {"内容ID": i, "发布时间": "2023-01-01"}) for i in ("a", "b")])
            calls = []
            def fetch(kind, args, cookie):
                ident = args[1]
                calls.append(ident)
                if ident == "b" and calls == ["a", "b"]:
                    raise RuntimeError("offline interruption")
                return {"status_code": 0, "aweme_detail": {"aweme_id": ident, "create_time": 1672502400,
                        "desc": "完整内容", "author": {"nickname": "作者"}, "ip_label": "上海",
                        "statistics": {"digg_count": 0, "collect_count": 8, "comment_count": 0, "share_count": 0},
                        "video": {"duration": 5000}}}
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                m.repair_fields(fetch, 3)
            self.assertTrue(rows(p)[0]["采集时间"])
            self.assertEqual(rows(p)[1]["采集时间"], "")
            self.assertEqual(m.repair_fields(fetch, 3), 1)
            self.assertEqual(calls, ["a", "b", "b"])
            self.assertEqual(rows(p)[1]["收藏数"], "8")

    def test_repair_core_fields_even_when_collection_time_is_already_present(self):
        import douyin as d
        import xiaohongshu as x
        from unittest.mock import Mock
        complete = {"发布时间": "2023-01-01", "采集时间": "2026-01-01", "作者": "作者",
                    "点赞数": 0, "评论数": 0, "收藏数": 0, "分享数": 0, "视频时长(秒)": 5}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(d, "OUTPUT", root / "dy"):
                path = d.OUTPUT / "douyin.csv"
                d.export_csv(path, d.COLUMNS, [d.canonical_post("抖音", {**complete, "内容ID": "complete"}),
                    d.canonical_post("抖音", {**complete, "内容ID": "missing", "收藏数": None})])
                fetch = Mock(return_value={"status_code": 0, "aweme_detail": {"aweme_id": "missing",
                    "create_time": 1672502400, "desc": "正文", "author": {"nickname": "作者"},
                    "statistics": {"digg_count": 0, "comment_count": 0, "collect_count": 7, "share_count": 0},
                    "video": {"duration": 5000}}})
                self.assertEqual(d.repair_fields(fetch, 1), 1)
                fetch.assert_called_once_with("aweme_detail", ["--aweme-id", "missing"], None)
                self.assertEqual(rows(path)[1]["收藏数"], "7")
                self.assertEqual(d.repair_fields(fetch, 1), 0)  # 可选IP/话题缺失不触发再次请求。
            with patch.object(x, "OUTPUT", root / "xhs"), patch.object(x, "STATE_DIR", root / "runtime"):
                x.STATE_DIR.mkdir()
                checkpoint = x.STATE_DIR / "full_run_state.jsonl"
                checkpoint.write_text('{"keyword":"双减"}\n', encoding="utf-8")
                before = checkpoint.read_bytes()
                (x.STATE_DIR / "candidate_pages_rpc.json").write_text(json.dumps({"keyword": "双减", "pages": [
                    {"notes": [{"note_id": ident, "xsec_token": "fixture"} for ident in ("complete", "missing")]}]}), encoding="utf-8")
                path = x.OUTPUT / "小红书_双减_笔记.csv"
                x.export_csv(path, x.COLUMNS, [x.canonical_post("小红书", {**complete, "内容ID": "complete"}),
                    x.canonical_post("小红书", {**complete, "内容ID": "missing", "收藏数": None})])
                result = {"note_id": "missing", "status": "success", "content": {
                    "title": "标题", "desc": "正文", "time": "2023-01-01", "user": {"nickname": "作者"}},
                    "propagation": {"liked": 0, "comment": 0, "collected": 7, "shared": 0}}
                with patch.object(x, "AsyncBrowserRPCClient", return_value=Mock(close=AsyncMock())), \
                     patch.object(x, "async_collect_note", new_callable=AsyncMock, return_value=result) as detail:
                    self.assertEqual(asyncio.run(x.repair_fields(1, 9223)), 1)
                    self.assertEqual(asyncio.run(x.repair_fields(1, 9223)), 0)
                    self.assertEqual(detail.call_count, 1)
                    self.assertEqual(detail.call_args.args[1], "missing")
                    self.assertEqual(detail.call_args.kwargs["comment_limit"], 0)
                self.assertEqual(rows(path)[1]["收藏数"], "7")
                self.assertEqual(checkpoint.read_bytes(), before)

    def test_xhs_cache_keywords_duplicate_update_and_detail_id_guard(self):
        import xiaohongshu as m
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "STATE_DIR", Path(tmp) / "runtime"):
            m.STATE_DIR.mkdir()
            (m.STATE_DIR / "candidate_pages_rpc.json").write_text(json.dumps({"keyword": "真实检索词", "pages": [
                {"notes": [{"note_id": "a", "xsec_token": "synthetic"}]}]}), encoding="utf-8")
            p = Path(tmp) / "posts.csv"
            m.export_csv(p, m.COLUMNS, [m.canonical_post("小红书", {"内容ID": "a", "发布时间": "2023-01-01",
                                                                  "点赞数": 9, "IP属地": "未提供"})])
            self.assertEqual(m.write_notes_csv([], p), 0)
            self.assertEqual(rows(p)[0]["搜索词"], "真实检索词")
            self.assertNotIn("IP属地", rows(p)[0])
            result = {"note_id": "a", "status": "success", "content": {"title": "详情标题", "time": "2023-01-01",
                      "ip_location": "广东"}, "propagation": {"collected": 0}}
            self.assertEqual(m.write_notes_csv([result], p), 0)
            self.assertEqual(len(rows(p)), 1)
            self.assertEqual(rows(p)[0]["收藏数"], "0")
            self.assertEqual(rows(p)[0]["点赞数"], "9")
            self.assertEqual(rows(p)[0]["IP属地"], "广东")
            self.assertEqual(rows(p)[0]["搜索词"], "真实检索词")
            payload = {"__raw_payload__": {"data": {"items": [{"id": "wrong", "note_card": {}}]}}}
            with patch.object(m, "async_fetch_note", new_callable=AsyncMock, return_value=payload):
                with self.assertRaisesRegex(RuntimeError, "ID"):
                    asyncio.run(m.async_fetch_note_detail(None, {"note_id": "a", "xsec_token": "synthetic"}))


if __name__ == "__main__":
    unittest.main()
