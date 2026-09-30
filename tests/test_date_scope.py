"""日期范围回归：只用离线响应，不修改正式数据/断点。"""
import asyncio
import csv
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))

START_TS = int(datetime.fromisoformat("2021-07-24T00:00:00+08:00").timestamp())
END_TS = int(datetime.fromisoformat("2026-07-24T23:59:59+08:00").timestamp())
IN_RANGE = {"start": "2021-07-23T16:00:00Z", "end": "2026-07-24T15:59:59.999999Z",
            "seconds": START_TS, "milliseconds": END_TS * 1000 + 999}
OUTSIDE = {"before": START_TS - 1, "after": (END_TS + 1) * 1000,
           "invalid": "2024-02-30", "unknown": None, "boolean": True}


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


class DateScopeTest(unittest.TestCase):
    def test_comments_filter_content_but_preserve_pagination_and_resume(self):
        for name, platform in (("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书")):
            m = importlib.import_module(name)
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "posts.csv"
                excluded = [{"评论ID": ident, "评论时间": value, "评论点赞数": 99999,
                             "评论内容": "excluded-body", "评论者昵称": "excluded-author"}
                            for ident, value in OUTSIDE.items()]
                with closing(m.CommentStore(target, platform)) as store, patch.object(m, "log_saved_row") as log:
                    store.register("post")
                    store.save_page("post", excluded, "saved-next", False)
                    self.assertEqual(store.state("post")["cursor"], "saved-next")
                    self.assertFalse(store.state("post")["done"])
                    self.assertEqual(read_rows(store.output), [])
                    log.assert_not_called()
                    for (payload,) in store.db.execute("SELECT payload FROM comments"):
                        self.assertEqual(set(json.loads(payload)), {"评论ID", "评论时间", "评论点赞数"})
                        self.assertNotIn("excluded-", payload)
                    with self.assertRaisesRegex(RuntimeError, "全部重复"):
                        store.save_page("post", excluded, "loop", False)
                    self.assertEqual(store.state("post")["pages"], 1)
                # 关库重开仍从原游标继续，不能把全页日期排除当成已翻完。
                with closing(m.CommentStore(target, platform)) as store, patch.object(m, "log_saved_row") as log:
                    self.assertEqual(store.state("post")["cursor"], "saved-next")
                    valid = [{"评论ID": ident, "评论时间": value, "评论点赞数": 0, "评论内容": "范围内"}
                             for ident, value in IN_RANGE.items()]
                    valid.append({**valid[0], "评论ID": "unknown-likes", "评论点赞数": None})
                    store.save_page("post", valid, "", True)
                    self.assertEqual(store.state("post")["pages"], 2)
                    self.assertTrue(store.state("post")["done"])
                    self.assertEqual(log.call_count, len(valid))  # 范围内但点赞未知仍是有效候选。
                    self.assertEqual({r["评论ID"] for r in read_rows(store.output)}, set(IN_RANGE))
                    self.assertTrue(all("2021-07-24" <= r["评论时间"][:10] <= "2026-07-24"
                                        for r in read_rows(store.output)))

    def test_posts_only_save_known_dates_including_both_boundary_days(self):
        import douyin as d
        import kuaishou as k
        import xiaohongshu as x
        vectors = {"start": START_TS, "end": END_TS, "before": START_TS - 1,
                   "after": END_TS + 1, "unknown": None}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(d, "OUTPUT", root / "dy"), patch.object(d, "log_saved_row") as log:
                videos = [{"aweme_id": ident, "create_time": value, "desc": "离线样本", "ip_label": "北京",
                           "author": {"nickname": "测试"}, "video": {"duration": 1000},
                           "statistics": dict(digg_count=0, comment_count=0, collect_count=0, share_count=0)}
                          for ident, value in vectors.items()]
                fetch = Mock(return_value={"has_more": False, "cursor": 20, "aweme_list": videos})
                path, count = d._collect_posts(["双减"], None, d.START, d.END, fetch=fetch)
                self.assertEqual(count, 2)
                self.assertEqual({r["内容ID"] for r in read_rows(path)}, {"start", "end"})
                self.assertEqual(log.call_count, 2)
                d._collect_posts(["双减"], None, d.START, d.END, fetch=fetch)
                fetch.assert_called_once()  # 日期限制不重置已完成搜索。
            self.assertEqual({ident for ident, value in vectors.items() if k.normalize(
                {"photo": {"id": ident, "timestamp": value}}, "双减", k.START, k.END)}, {"start", "end"})
            with patch.object(x, "STATE_DIR", root / "xhs-runtime"), patch.object(x, "log_saved_row") as log:
                path = root / "xhs.csv"
                results = [{"status": "success", "note_id": ident, "content": {"title": ident, "time": value}}
                           for ident, value in {**IN_RANGE, **OUTSIDE}.items()]
                self.assertEqual(x.write_notes_csv(results, path), len(IN_RANGE))
                self.assertEqual({r["内容ID"] for r in read_rows(path)}, set(IN_RANGE))
                self.assertEqual(log.call_count, len(IN_RANGE))
                with closing(x.CommentStore(path, "小红书")) as store:
                    self.assertEqual({r[0] for r in store.db.execute("SELECT id FROM posts")}, set(IN_RANGE))

    def test_xhs_default_detail_filter_stops_before_comment_request(self):
        import xiaohongshu as x
        for value in OUTSIDE.values():
            with self.subTest(time=value), patch.object(x, "async_fetch_note_detail", new=AsyncMock(
                    return_value={"note_id": "post", "publish_time": value})), \
                 patch.object(x, "async_collect_comments", new_callable=AsyncMock) as comments:
                result = asyncio.run(x.async_collect_note(None, "post"))
                self.assertEqual(result["status"], "skipped")
                comments.assert_not_called()

    def test_kuaishou_cli_cannot_widen_scope(self):
        import kuaishou as k
        for args in (["--start-date", "2021-07-23"], ["--end-date", "2026-07-25"],
                     ["--start-date", "2026-07-24", "--end-date", "2021-07-24"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), \
                 patch.object(k, "check_environment") as check, patch.object(k, "SearchClient") as client:
                with self.assertRaises(SystemExit) as raised:
                    k.main([*args, "--check"])
                self.assertEqual(raised.exception.code, 2)
                check.assert_not_called()
                client.assert_not_called()

    def test_progress_distinguishes_returned_rows_from_date_filtered_rows(self):
        import start_all
        line = "2026-09-28 16:00:00 | douyin | INFO | 评论页已提交 | " + json.dumps({
            "内容ID": "post", "累计页数": 1, "返回条数": 10, "新增候选": 0, "日期排除": 10, "已完成": False})
        rendered = start_all.render_console_line("douyin", line, width=110)
        self.assertIn("日期排除 10 条", rendered)
        self.assertIn("新增 0 条", rendered)
        self.assertIn("待续采", rendered)


if __name__ == "__main__":
    unittest.main()
