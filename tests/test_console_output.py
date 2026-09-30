"""终端排版离线验证；日志原文、采集器与CSV不改动。"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start_all as app


def line(kind, payload=None, level="INFO"):
    message = kind if payload is None else kind + " | " + json.dumps(payload, ensure_ascii=False)
    return "2026-09-28 16:05:12,123 | douyin | " + level + " | " + message


class ConsoleOutputTest(unittest.TestCase):
    def test_comments_are_aligned_wrapped_and_not_truncated(self):
        text = "这是一条中文与English混排的评论🙂。" * 10 + "\n第二段完整保留。"
        data = {"内容ID": "6989895709264727326", "评论ID": "comment-1", "评论者昵称": "测试昵称",
                "评论内容": text, "评论点赞数": 0, "评论时间": "2023-01-01 12:30:00",
                "可参与高赞排序": True, "采集时间": "2026-09-28 16:05:12",
                "Cookie": "private-fixture", "xsec_token": "private-fixture"}
        for width in (40, 64, 96):
            with self.subTest(width=width):
                result = app.render_console_line("douyin", line("评论候选已保存", data), width)
                self.assertIn("抖音", result)
                self.assertIn("16:05:12", result)
                self.assertIn("候选，非最终TOP20", result)
                self.assertNotIn("private-fixture", result)
                self.assertNotIn('{"', result)
                self.assertTrue(all(app.console_width(s) <= width for s in result.splitlines()))
                labels = [s.split(" : ", 1)[0] for s in result.splitlines() if " : " in s]
                self.assertEqual(len({app.console_width(s) for s in labels}), 1)
                self.assertIn("点赞数   : 0", result)
                # 去掉续行缩进后，所有正文字符仍在，包括原来的两个段落。
                content = result.split("评论内容 : ", 1)[1].split("│  采集时间", 1)[0]
                restored = "".join(s if index == 0 else s[14:] for index, s in enumerate(content.splitlines()))
                self.assertEqual(restored, text.replace("\n", ""))

    def test_posts_show_metrics_missing_values_and_distinct_body(self):
        data = {"内容ID": "post-1", "标题": "贴文标题", "作者": "作者名", "发布时间": "2024-01-01",
                "内容": "完整正文", "点赞数": 0, "评论数": 17, "收藏数": None, "分享数": "",
                "搜索词": "双减", "采集时间": "2026-09-28 16:05:12"}
        rendered = app.render_console_line("xiaohongshu", line("贴文已保存", data), 90)
        self.assertIn("小红书 · 贴文已保存", rendered)
        self.assertIn("赞 0   |   评 17   |   藏 —   |   转 —", rendered)
        self.assertIn("正文     : 完整正文", rendered)
        self.assertIn("搜索词   : 双减", rendered)
        data['内容'] = data['标题']
        rendered = app.render_console_line("xiaohongshu", line("贴文已保存", data), 90)
        self.assertEqual(rendered.count("贴文标题"), 1)

    def test_progress_warnings_csv_and_bad_json_remain_readable(self):
        progress = line("评论页已提交", {"内容ID": "post-1", "累计页数": 12, "返回条数": 10,
                                        "新增候选": 8, "已完成": False})
        rendered = app.render_console_line("kuaishou", progress, 64)
        for text in ("进度", "内容ID post-1", "累计 12 页", "新增 8 条", "待续采"):
            self.assertIn(text, rendered)
        self.assertTrue(all(app.console_width(s) <= 64 for s in rendered.splitlines()))
        warning = app.render_console_line("xiaohongshu", line("需要人工登录", level="WARNING"))
        self.assertIn("警告", warning)
        self.assertIn("需要人工登录", warning)
        saved = app.render_console_line("douyin", line("高赞评论CSV已同步：D:\\private\\output\\csv\\douyin_高赞评论.csv，共20条（未完成篇为当前候选排名）"), 80)
        self.assertIn("CSV", saved)
        self.assertIn("douyin_高赞评论.csv", saved)
        self.assertNotIn("D:\\private", saved)
        bad = app.render_console_line("douyin", line("评论候选已保存 | {broken"))
        self.assertIn("记录格式异常", bad)
        self.assertNotIn("{broken", bad)
        short = app.render_console_line("douyin", progress, 24)
        self.assertTrue(all(app.console_width(s) <= 24 for s in short.splitlines()))

    def test_controls_are_removed_without_changing_log_or_interleaving_cards(self):
        originals = {}
        for name in ("douyin", "kuaishou"):
            originals[name] = (line("评论候选已保存", {"内容ID": "post", "评论ID": "1",
                "评论者昵称": "昵称\x1b[31m\u202e", "评论内容": "正文\r\n后续\t内容"}) + "\n").encode('utf-8')
        readers = {name: io.BytesIO(raw) for name, raw in originals.items()}
        with patch.object(app, "print", create=True) as display:
            app.forward_logs(readers, {}, final=True)
        self.assertEqual(display.call_count, 2)  # 每张卡片一个print，不能三平台逐行穿插。
        for call in display.call_args_list:
            block = call.args[0]
            self.assertEqual(block.count("┌"), 1)
            self.assertEqual(block.count("└"), 1)
            self.assertNotIn("\x1b", block)
            self.assertNotIn("\u202e", block)
            self.assertNotIn("\r", block)
            self.assertIn("后续    内容", block)
        self.assertEqual({name: reader.getvalue() for name, reader in readers.items()}, originals)
        self.assertEqual(app.console_width("中文ab"), 6)
        self.assertEqual(app.console_width("e\u0301"), 1)


if __name__ == "__main__":
    unittest.main()
