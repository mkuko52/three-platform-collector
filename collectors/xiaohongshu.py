"""小红书单文件采集器（已登录浏览器RPC）；直接运行本文件，CSV写入项目根目录的output/csv目录。"""
from __future__ import annotations
import argparse
import hashlib
import sys
import signal
import logging
# ponytail: 为三个单文件独立运行，公共CSV/状态逻辑各自内置；修改时需同步三份。
import csv
import json
import math
import os
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

CST = timezone(timedelta(hours=8))
START, END = date(2021, 7, 24), date(2026, 7, 24)
POST_FIELDS = ["平台", "内容ID", "标题", "链接", "内容", "话题标签", "作者", "发布时间",
               "点赞数", "评论数", "收藏数", "分享数", "阶段", "搜索词", "采集时间"]
COMMENT_FIELDS = ["平台", "内容ID", "链接", "排名", "评论ID", "评论内容", "评论点赞数",
                  "评论时间", "评论者昵称", "评论者ID", "IP属地", "采集时间", "采集状态", "排序口径"]
STAGES = [("政策落地期", "2021-07-24", "2021-12-31"),
          ("机构转型退出期", "2022-01-01", "2022-12-31"),
          ("隐形变异治理期", "2023-01-01", "2023-12-31"),
          ("AI辅导与非学科扩容期", "2024-01-01", "2026-07-24")]
URLS = {"抖音": "https://www.douyin.com/video/", "快手": "https://www.kuaishou.com/short-video/",
        "小红书": "https://www.xiaohongshu.com/explore/"}
RANK_SCOPE = "时间范围内可见一级评论按点赞降序；不含楼中楼；非平台全局排名"


def exact_count(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = Decimal(str(value))
        return int(n) if n.is_finite() and n >= 0 and n == n.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value):
    """北京时间；无法确认的时间留空，不使用更新时间代替发布时间。"""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)
        else:
            n = float(value)
            dt = datetime.fromtimestamp(n / 1000 if abs(n) >= 100_000_000_000 else n, CST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def stage(published):
    day = timestamp(published)[:10]
    return next((name for name, lower, upper in STAGES if lower <= day <= upper), "")


def search_queries(words, start=START, end=END, yearly=True):
    """年份补充词不等于服务端日期筛选；仍必须依据真实发布时间过滤。"""
    words = list(dict.fromkeys(words))
    # 每个词先轮询各年，再补无年份检索，避免只在最新年份耗尽运行时间。
    extra = [f"{word} {year}" for word in words for year in range(start.year, end.year + 1)] if yearly else []
    return list(dict.fromkeys([*extra, *words]))


def shard_queries(queries, shard="full"):
    if shard == "full":
        return list(queries)
    if len(queries) != 938:
        raise ValueError("分片词表已变化，拒绝不完整搜索")
    if shard == "forward":
        return list(queries[:469])
    if shard == "reverse":
        return list(reversed(queries[469:]))
    raise ValueError("未知搜索分片")


def post_columns(legacy):
    return list(dict.fromkeys([*POST_FIELDS, *legacy]))


def canonical_post(platform, row):
    row = dict(row)
    ident = str(row.get("内容ID") or row.get("视频ID") or row.get("笔记ID") or "")
    if not ident:
        raise ValueError("内容缺少可靠 ID；不能按标题猜测关联关系")
    row.update({"平台": platform, "内容ID": ident,
                "标题": row.get("标题", row.get("笔记标题", row.get("视频标题或描述", ""))),
                "链接": URLS[platform] + ident,
                "内容": row.get("内容", row.get("正文", row.get("视频标题或描述", ""))),
                "发布时间": timestamp(row.get("发布时间")),
                "搜索词": row.get("搜索词", row.get("搜索关键词", "")),
                "采集时间": timestamp(row.get("采集时间"))})
    row["阶段"] = stage(row["发布时间"])
    if platform in ("抖音", "快手"):
        row["视频ID"], row["视频链接"] = ident, row["链接"]
    for key in ("点赞数", "评论数", "收藏数", "分享数"):
        row[key] = exact_count(row.get(key))
    return row


def csv_cell(value):
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def atomic_csv(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: csv_cell(v) for k, v in row.items()})
            file.flush()
            os.fsync(file.fileno())
        # 只暂停本站文件同步；外部编辑器可能有未保存内容，绝不杀进程或强关句柄。
        delays = (1, 2, 4, 8, 16) + (60,) * 30
        for attempt in range(len(delays) + 1):
            try:
                # 已同步的快照不反复替换：Windows 上读者短暂打开目标文件也会挡住 replace。
                if path.exists() and path.stat().st_size == os.path.getsize(name) and path.read_bytes() == Path(name).read_bytes():
                    return
                os.replace(name, path)
                return
            except PermissionError as exc:
                if attempt == len(delays):
                    code = getattr(exc, "winerror", None) or exc.errno or "未知"
                    raise RuntimeError(f"CSV 被占用或写入权限不足（系统错误码{code}；本地等待{sum(delays)}秒仍失败），未能更新：{path}；已采候选仍保存在状态库") from None
                if attempt == 5:
                    logger.warning("CSV 仍被本地程序占用，暂停本站请求等待约30分钟；释放后自动续采，不关闭外部程序：%s", path)
                time.sleep(delays[attempt])  # 只等本地文件释放，不重发网站请求。
    finally:
        Path(name).unlink(missing_ok=True)


def present(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def log_saved_row(platform, row, is_comment=False):
    """只打印已提交的业务字段；不输出原响应、Cookie、签名或候选令牌。"""
    fields = (("内容ID", "评论ID", "评论者昵称", "评论内容", "评论点赞数", "评论时间", "可参与高赞排序", "采集时间")
              if is_comment else ("内容ID", "标题", "作者", "发布时间", "内容", "点赞数", "评论数", "收藏数", "分享数", "搜索词", "采集时间"))
    data = {"平台": platform, **{key: row.get(key) for key in fields}}
    # JSON转义换行/控制字符，一条记录一行；缺失值仍为空，不填假0。
    logger.info("%s已保存 | %s", "评论候选" if is_comment else "贴文", json.dumps(data, ensure_ascii=False))


def merge_post(old, new):
    if old and old["内容ID"] != new["内容ID"]:
        raise ValueError("详情ID与已有记录不一致，拒绝补写")
    merged = dict(old)
    merged.update({k: v for k, v in new.items() if present(v)})
    if present(old.get("搜索词")):
        merged["搜索词"] = old["搜索词"]
    return merged


def dataset_header(header, fields):
    # 允许本程序省略全空列后的有序子集；未知列、重复列及缺少关联键仍拒绝。
    return bool(header) and "内容ID" in header and "平台" in header and header == [k for k in fields if k in header]


def read_posts(path, fields, platform):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not dataset_header(reader.fieldnames, fields):
            raise ValueError(f"CSV 字段不兼容：{path}")
        rows, seen = [], set()
        for row in reader:
            if (None in row or any(v is None for v in row.values()) or not row.get("内容ID")
                    or row["内容ID"] in seen or row.get("平台") != platform
                    or not timestamp(row.get("发布时间"))):
                raise ValueError(f"CSV 记录损坏、重复或平台不一致：{path}")
            seen.add(row["内容ID"])
            rows.append(canonical_post(platform, row))
    return rows


def export_csv(path, fields, rows):
    """只省略整列无真实值的列；部分缺失保留，0不是缺失。完整字段仍在代码/状态库中。"""
    path, rows = Path(path), [dict(row) for row in rows]
    placeholders = {"IP属地": {"未提供", "缺失（平台未提供）"}, "IP 属地": {"未提供"},
                    "视频链接": {"未提供", "不适用"}, "话题标签": {"无话题标签"}}
    for row in rows:
        for key, values in placeholders.items():
            if row.get(key) in values:
                row[key] = ""
    counts = {key: sum(present(row.get(key)) for row in rows) for key in fields}
    selected = [key for key in fields if counts[key]] if rows else list(fields)
    atomic_csv(path, selected, rows)
    report = {"文件": path.name, "记录数": len(rows), "完整字段": list(fields), "导出字段": selected,
              "全空未导出字段": [k for k in fields if k not in selected],
              "字段非空条数": counts, "字段缺失条数": {k: len(rows) - v for k, v in counts.items()},
              "说明": "省略全空列不代表字段已补齐；部分缺失仍为空。未知数值不填0，旧采集时间不按文件时间猜测。"}
    report_path = sidecar_path(path.with_name(path.stem + "_字段报告.json"), "reports")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)


def sidecar_path(path, folder):
    """配套文件分目录存放；自定义CSV子目录按原层级镜像，避免同名文件串库。"""
    path = Path(path)
    try:
        relative = path.relative_to(OUTPUT)
    except ValueError:
        return path.parent / folder / path.name
    return OUTPUT.parent / folder / relative


def upgrade_csv(path, legacy_fields, fields, convert):
    """仅识别已知旧表头；原文件备份成功且整表验证后才替换。"""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames == list(fields) or dataset_header(reader.fieldnames, fields):
            return
        if reader.fieldnames != list(legacy_fields):
            raise ValueError(f"CSV 表头不兼容，拒绝覆盖：{path}")
        rows = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"旧 CSV 有残缺行，拒绝迁移：{path}")
            rows.append(convert(row))
    ids = [r["内容ID"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError(f"旧 CSV ID 重复，拒绝迁移：{path}")
    backup = sidecar_path(path.with_suffix(path.suffix + ".schema-v1.bak"), "backups")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists() and backup.read_bytes() != path.read_bytes():
        raise ValueError(f"备份已存在且不同，拒绝覆盖：{backup}")
    if not backup.exists():
        shutil.copy2(path, backup)
    atomic_csv(path, fields, rows)


def comment_jobs(store, post_ids, page_limit=None):
    """历史贴文和新贴文共用独立评论队列；轮流一页，不为深页作品饿死其它旧ID。"""
    from collections import deque
    states = [store.state(ident) for ident in dict.fromkeys(post_ids)]
    states = sorted((s for s in states if not s["done"]),
                    key=lambda s: (s["status"].startswith("评论暂不可用"),
                                   s["status"].startswith("分页停滞"), s["pages"]))
    pending = deque((s["id"], 0) for s in states)
    logger.info("评论补采队列：%s篇未完成（含旧贴文）；逐篇轮流采1页，不重采详情", len(pending))
    while pending:
        ident, used = pending.popleft()
        before = store.state(ident)["pages"]
        yield ident
        current = store.state(ident)
        delta = current["pages"] - before
        used += delta
        if not current["done"] and delta > 0 and (page_limit is None or used < page_limit):
            store.mark(ident, "轮转补采，待续页")
            pending.append((ident, used))
        # 没有提交新页（如分页停滞）不在本轮反复重发；仍保留未完成状态。


class CommentStore:
    """逐页提交候选和游标；导出每篇最多20条，不把失败当成零评论。"""
    def __init__(self, posts_path, platform, start=START, end=END, memory=False):
        self.posts_path, self.platform = Path(posts_path), platform
        self.memory = memory
        self.start, self.end = date.fromisoformat(str(start)).isoformat(), date.fromisoformat(str(end)).isoformat()
        if platform not in URLS or self.start > self.end:
            raise ValueError("未知平台或无效日期范围")
        self.output = self.posts_path.with_name(self.posts_path.stem + "_高赞评论.csv")
        self.posts_path.parent.mkdir(parents=True, exist_ok=True)
        db_path = sidecar_path(self.posts_path.with_suffix(".comments.sqlite3"), "state")
        if not memory:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(":memory:" if memory else str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS config (value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, url TEXT NOT NULL,
                cursor TEXT NOT NULL DEFAULT '', pages INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '待补采', total INTEGER);
            CREATE TABLE IF NOT EXISTS comments (post_id TEXT NOT NULL, id TEXT NOT NULL,
                likes INTEGER, eligible INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(post_id,id));
            CREATE TABLE IF NOT EXISTS pages (post_id TEXT NOT NULL, cursor TEXT NOT NULL,
                PRIMARY KEY(post_id,cursor));
        ''')
        config = json.dumps([platform, self.start, self.end])
        saved = self.db.execute("SELECT value FROM config").fetchone()
        if saved is not None and saved[0] != config:
            self.close()
            raise ValueError("评论状态库的平台/日期范围不一致，请使用新的输出路径")
        if saved is None:
            with self.db:
                self.db.execute("INSERT INTO config VALUES (?)", (config,))

    def register(self, post_id, url=None):
        if post_id is None or isinstance(post_id, bool) or not str(post_id).strip():
            raise ValueError("评论必须关联内容ID")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO posts(id,url) VALUES (?,?)",
                            (str(post_id), url or URLS[self.platform] + str(post_id)))

    def state(self, post_id):
        return dict(self.db.execute("SELECT * FROM posts WHERE id=?", (str(post_id),)).fetchone())

    def mark(self, post_id, status):
        with self.db:
            self.db.execute("UPDATE posts SET status=? WHERE id=? AND done=0", (status, str(post_id)))

    def save_page(self, post_id, rows, cursor, done, total=None):
        post_id, cursor = str(post_id), str(cursor)
        current = self.state(post_id)
        prepared, retained, excluded = [], set(), 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("评论ID"):
                raise RuntimeError("评论条目缺少评论ID，不能可靠去重")
            row = dict(row)
            row["评论ID"] = str(row["评论ID"])
            row["评论时间"] = timestamp(row.get("评论时间"))
            row["评论点赞数"] = exact_count(row.get("评论点赞数"))
            row.setdefault("采集时间", datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
            in_range = self.start <= row["评论时间"][:10] <= self.end
            if in_range:
                retained.add(row["评论ID"])
            else:
                excluded += 1
                retained.discard(row["评论ID"])
                # 仅留去重/日期标记，避免全页越界被误判为空页；不保存正文、作者或互动数。
                row = {"评论ID": row["评论ID"], "评论时间": row["评论时间"], "评论点赞数": None}
            eligible = in_range and row["评论点赞数"] is not None
            prepared.append((post_id, row["评论ID"], row["评论点赞数"], eligible, json.dumps(row, ensure_ascii=False)))
        fresh = {r[1] for r in prepared if not self.db.execute(
            "SELECT 1 FROM comments WHERE post_id=? AND id=?", (post_id, r[1])).fetchone()}
        if not done:
            if not rows or cursor == current["cursor"] or self.db.execute(
                    "SELECT 1 FROM pages WHERE post_id=? AND cursor=?", (post_id, cursor)).fetchone():
                raise RuntimeError("评论空页或游标循环，不能标记已采完")
            if not fresh:
                raise RuntimeError("评论页全部重复，不能标记已采完")
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?)", prepared)
            self.db.execute("INSERT OR IGNORE INTO pages VALUES (?,?)", (post_id, current["cursor"]))
            self.db.execute("UPDATE posts SET cursor=?, pages=pages+1, done=?, status=?, total=COALESCE(?,total) WHERE id=?",
                            (cursor, done, "已遍历可见一级评论" if done else "未完成", exact_count(total), post_id))
        if not self.memory:
            for item in {r[1]: r for r in prepared}.values():
                if item[1] in fresh and item[1] in retained:
                    log_saved_row(self.platform, {**json.loads(item[4]), "内容ID": post_id,
                                                  "可参与高赞排序": bool(item[3])}, is_comment=True)
            logger.info("评论页已提交 | %s", json.dumps({"内容ID": post_id, "累计页数": current["pages"] + 1,
                "返回条数": len(prepared), "新增候选": len(fresh & retained), "日期排除": excluded,
                "已完成": bool(done)}, ensure_ascii=False))
            self.export()  # SQLite先提交；CSV失败不回退已保存的游标，下次先恢复导出。

    def top(self, post_id):
        state = self.state(post_id)
        rows = self.db.execute("SELECT payload FROM comments WHERE post_id=? AND eligible=1 ORDER BY likes DESC,id ASC LIMIT 20",
                               (str(post_id),))
        return [{**json.loads(row[0]), "平台": self.platform, "内容ID": str(post_id), "链接": state["url"],
                 "排名": rank, "采集状态": state["status"], "排序口径": RANK_SCOPE}
                for rank, row in enumerate(rows, 1)]

    def export(self):
        # ponytail: 每页原子重写当前高赞CSV；文件大小成为瓶颈后再做按内容ID增量快照。
        rows = [row for post in self.db.execute("SELECT id FROM posts ORDER BY rowid") for row in self.top(post[0])]
        export_csv(self.output, COMMENT_FIELDS, rows)
        logger.info("高赞评论CSV已同步：%s，共%s条（未完成篇为当前候选排名）", self.output, len(rows))

    def report(self):
        years = {str(y): 0 for y in range(int(self.start[:4]), int(self.end[:4]) + 1)}
        stages = {s[0]: 0 for s in STAGES}
        if self.posts_path.exists():
            with self.posts_path.open(encoding="utf-8-sig", newline="") as file:
                for row in csv.DictReader(file):
                    year = row.get("发布时间", "")[:4]
                    if year in years:
                        years[year] += 1
                    name = stage(row.get("发布时间"))
                    if name in stages:
                        stages[name] += 1
        comments = []
        for post in self.db.execute("SELECT * FROM posts ORDER BY rowid"):
            n, eligible = self.db.execute("SELECT COUNT(*),COALESCE(SUM(eligible),0) FROM comments WHERE post_id=?", (post["id"],)).fetchone()
            comments.append({"内容ID": post["id"], "状态": post["status"], "页数": post["pages"],
                             "候选数": n, "可排序数": eligible, "导出数": min(20, eligible)})
        report = {"平台": self.platform, "开始日期": self.start, "结束日期": self.end,
                  "按年篇数": years, "按阶段篇数": stages, "评论采集": comments,
                  "未完成评论篇数": self.db.execute("SELECT COUNT(*) FROM posts WHERE done=0").fetchone()[0],
                  "说明": "数量为已采样本，不证明平台历史完整；0表示未采到，不表示没有。年份词不是日期过滤。",
                  "评论口径": RANK_SCOPE}
        path = sidecar_path(self.posts_path.with_name(self.posts_path.stem + "_覆盖报告.json"), "reports")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def close(self):
        self.db.close()


ROOT = Path(__file__).resolve().parents[1]  # collectors/ 的上一级是项目根目录
OUTPUT = ROOT / "output/csv"
STATE_DIR = ROOT / "runtime/xiaohongshu"
DEFAULT_KEYWORDS = ('双减', '双减政策', '义务教育双减', '减轻学生作业负担', '减轻校外培训负担', '作业管理', '五项管理', '校外培训治理', '课后服务', '校内课后服务', '课后延时服务', '延时托管', '三点半课堂', '三点半难题', '430课后服务', '430课堂', '课后看护', '校内托管', '午托', '晚托', '寒暑假校内托管', '课后服务收费', '普惠性课后服务', '课后托管', '托管班', '校外托管', '学生托管', '小饭桌', '作业托管', '假期托管', '寒暑假托管', '暑托班', '晚辅', '作业辅导班', '课外培训', '校外培训', '学科培训', '学科类培训', '非学科类培训', '补习班', '辅导班', '培训班', '培训机构', '教培', '教培行业', '培训学校', 'K12培训', '一对一辅导', '一对一家教', '小班课', '线上培训', '网课', '在线教育', '隐形变异培训', '无证办学', '办学资质', '无资质培训', '预收费', '收费监管', '资金监管', '培训广告', '广告治理', '黑白名单', '教培机构注销', '机构跑路', '退费难', '营转非', '非营利机构', '机构转型', '行政处罚', '吊销办学许可', '家长', '学生', '教师', '老师', '班主任', '校长', '教培从业者', '教培老师', '机构老板', '家长委员会', '焦虑', '教育焦虑', '鸡娃', '内卷', '减负', '减负不减负', '躺平', '佛系', '分流', '学区房', '教育公平', '负担', '压力', '睡眠不足', '近视', '心理健康', '抑郁', '厌学', '牛娃', '牛蛙', '普娃', '渣娃', '海淀妈妈', '顺义妈妈', '鸡血家长', '佛系养娃', '掐尖', '点招', '密考', '暗考', '抢跑', '起跑线', '剧场效应', 'AI辅导', 'AI家教', '人工智能辅导', '智能学习', '学习机', '作业帮', '猿辅导转型', '非学科类', '艺术培训', '体育培训', '编程教育', '科学素养', '研学旅行', '营地教育', '素质教育', '教师弹性上下班', '教师课后服务津贴', '教师负担', '双减 课后服务', '校外培训 退费难')
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("xiaohongshu")

def collector_lock():
    """同一平台只运行一份；OS在进程退出时自动释放，不删除正在使用的锁文件。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = (STATE_DIR / "collector.lock").open("a+b")
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
        raise RuntimeError("该平台已有采集任务运行，请先停止旧任务") from None


_collector_lease = None  # 仅本进程实际持有平台OS锁时，允许回收页面里的空闲旧连接。


class TestBudgetExhausted(RuntimeError):
    """本轮有界采集测试达到网络动作数上限，原断点待续。"""


def run_cli():
    global _collector_lease
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        if any(flag in sys.argv[1:] for flag in ("--help", "-h", "--check", "--list-keywords")):
            return main()
        with collector_lock() as lease:
            _collector_lease = lease
            try:
                logger.info("数据允许范围（北京时间）：%s 00:00:00 至 %s 23:59:59，含首尾日；按发布时间/评论时间筛选，不按采集时间", START, END)
                return main()
            finally:
                _collector_lease = None
    except KeyboardInterrupt:
        return 130
    except TestBudgetExhausted:
        print("有界测试达到请求上限，原断点保留；未完成不等于采集成功", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"采集未完成：{exc}", file=sys.stderr)
        return 1


import asyncio
import random
import re
import subprocess
import time
import urllib.parse
from typing import Optional
from collections import deque
from urllib.error import URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import ProxyHandler, build_opener, getproxies, proxy_bypass
import httpx
from websocket import create_connection, WebSocketTimeoutException

START_DATE, END_DATE = str(START), str(END)
SEARCH = SEARCH_NOTES_PATH = "/api/sns/web/v1/search/notes"
FEED = FEED_PATH = "/api/sns/web/v1/feed"
COMMENT = COMMENT_PATH = "/api/sns/web/v2/comment/page"

BRIDGE_JS = '/* Install in the already-loaded site page. No fetch/XHR, signer or cookie export. */\n(() => {\n    "use strict";\n    if (location.origin !== "https://www.xiaohongshu.com")\n        return {ready: false, reason: "wrong_origin"};\n    if (document.readyState !== "complete" || !window.__INITIAL_STATE__)\n        return {ready: false, reason: "page_not_ready"};\n    if (window.__xhsCollectorRPC)\n        return {ready: false, reason: "bridge_in_use_or_stopped"};\n    const chunks = window.webpackChunkxhs_pc_web;\n    if (!Array.isArray(chunks)) return {ready: false, reason: "webpack_not_loaded"};\n    let require;\n    chunks.push([["xhs-local-rpc-" + crypto.randomUUID()], {}, r => { require = r; }]);\n    if (!require?.m) return {ready: false, reason: "runtime_not_available"};\n\n    // Fingerprint the native HTTP module observed in vendor-dynamic.f1969d7f.js.\n    // Do not run arbitrary factories, create a new HTTP client or copy its signer.\n    const ids = Object.keys(require.m).filter(id => {\n        const source = Function.prototype.toString.call(require.m[id]);\n        return ["function processSend(", "function handleResData(", "transformRequestConfig", "extractData"]\n            .every(marker => source.includes(marker));\n    });\n    if (ids.length !== 1) return {ready: false, reason: "native_http_module_not_unique"};\n    const client = require(ids[0]);\n    if (typeof client.post !== "function" || !client.instance || !client.interceptors)\n        return {ready: false, reason: "native_http_contract_changed"};\n\n    const SEARCH = "/api/sns/web/v1/search/notes";\n    const FEED = "/api/sns/web/v1/feed";\n    const COMMENT = "/api/sns/web/v2/comment/page";\n    const bridge = {\n        version: 1, stopped: false, busy: false, uncertain: false,\n        async call(path, body) {\n            if (this.stopped || this.busy) return {ok: false, reason: "bridge_stopped_or_busy"};\n            if (location.origin !== "https://www.xiaohongshu.com" ||\n                location.pathname.startsWith("/website-login/")) {\n                this.stopped = true;\n                return {ok: false, reason: "page_not_available"};\n            }\n            const isComment = typeof path === "string" && path.startsWith(COMMENT + "?") && body == null;\n            if (isComment) {\n                const query = new URLSearchParams(path.slice(COMMENT.length + 1));\n                if (!query.get("note_id") || !query.get("xsec_token") || typeof client.get !== "function")\n                    return {ok: false, reason: "invalid_comment"};\n            } else if (![SEARCH, FEED].includes(path) || !body || Array.isArray(body) || typeof body !== "object")\n                return {ok: false, reason: "invalid_request"};\n            if (path === SEARCH && (typeof body.keyword !== "string" || !body.keyword.trim() ||\n                !Number.isInteger(body.page) || body.page < 1))\n                return {ok: false, reason: "invalid_search"};\n            if (path === FEED && (typeof body.source_note_id !== "string" || !body.source_note_id ||\n                typeof body.xsec_token !== "string" || !body.xsec_token))\n                return {ok: false, reason: "invalid_note"};\n\n            this.busy = true;\n            let timer;\n            try {\n                const payload = await Promise.race([\n                    // Preserve native signing/session/interceptors. Keep the full snake_case envelope.\n                    isComment ? client.get(path, {extractData: false, transform: false}) :\n                        client.post(path, body, {extractData: false, transform: false}),\n                    new Promise((_, reject) => {\n                        timer = setTimeout(() => reject({rpcTimeout: true}), 35000);\n                    })\n                ]);\n                if (!payload || payload.code !== 0 || payload.success === false)\n                    this.stopped = true;\n                return {ok: true, payload};\n            } catch (error) {\n                // Never return stack/config/headers/URLs: native errors may contain credentials.\n                this.stopped = this.uncertain = true;\n                const status = error?.status ?? error?.response?.status;\n                const code = error?.code ?? error?.data?.code;\n                return {ok: false, reason: error?.rpcTimeout ? "timeout_no_retry" : "native_request_failed",\n                        status: Number.isInteger(status) ? status : null,\n                        code: Number.isInteger(code) ? code : null};\n            } finally {\n                clearTimeout(timer);\n                this.busy = false;\n            }\n        }\n    };\n    window.__xhsCollectorRPC = bridge;\n    return {ready: true, module: ids[0], version: bridge.version};\n})()\n'
START_URL = "https://www.xiaohongshu.com/explore"  # 冷启动只初始化会话，不自动重搜“双减”首页。
ACW_INDICATORS = ("acw.scu", "self.__ac_", "acw_sc__v2", "acw_sc__")
_RISK_CONTROL_TEXT_SIGNATURES = ("请通过验证", "请求太频繁，请稍后再试", "请求太频繁了", "请求太频繁", "安全验证", "人机验证", "滑动验证")
class RiskControlError(RuntimeError):
    """触发风控验证（请通过验证 / 请求太频繁），必须立即停止整个程序。"""
    pass


class TransientRiskError(RuntimeError):
    """临时服务错误或原因未明的访问限制：停止本轮，保留断点待人工重试。"""
    pass


def check_response_risk_control(text: str) -> str:
    """检查响应文本是否包含风控关键词。

    返回匹配到的关键词（空字符串表示未命中）。
    """
    if not text:
        return ""
    for keyword in _RISK_CONTROL_TEXT_SIGNATURES:
        if keyword in text:
            return keyword
    return ""
def _find_browser_executable():
    """优先环境变量,再本机已装,必须支持 --remote-debugging-port"""
    candidates = [
        os.environ.get("CHROME_PATH"),
        os.environ.get("EDGE_PATH"),
        # 普通 Chrome 优先：支持 --remote-debugging-port 命令行 flag
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        # CloakBrowser 不支持 --remote-debugging-port 有头模式，仅作兜底
        r"D:\develop_software\CloakBrowser\chrome.exe",
        shutil.which("chrome"),
        shutil.which("msedge"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "未找到 Chrome 或 Edge，请先安装，或设置 CHROME_PATH/EDGE_PATH 环境变量"
    )


def _http_json(url, timeout=10):
    """简易 HTTP GET → JSON"""
    with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ensure_browser_alive(process):
    if process is None:
        return
    if process.poll() is not None:
        raise RuntimeError(f"浏览器已退出，返回码 {process.returncode}")


def _wait_for_cdp(port, process=None, timeout=20):
    """等 CDP 调试 HTTP 端口可用"""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if process is not None:
            try:
                _ensure_browser_alive(process)
            except RuntimeError:
                raise
        try:
            return _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"CDP 端口 {port} 未就绪 ({timeout}s): {last_error}")
def browser_proxy_args():
    """Chrome doesn't consume HTTP(S)_PROXY like Python clients; use the same configured route."""
    if proxy_bypass("www.xiaohongshu.com"):
        return ["--no-proxy-server"]
    proxies = getproxies()
    proxy = proxies.get("https") or proxies.get("http") or proxies.get("all")
    if not proxy:
        return []
    parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
    if parsed.username or parsed.password:
        raise RuntimeError("CDP 代理含认证信息，不能安全传入 Chrome 命令行；请配置不含认证的本地代理")
    if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
        raise RuntimeError("CDP 代理配置格式不受支持")
    return [f"--proxy-server={parsed.scheme}://{parsed.netloc}"]
import base64
class CDPTab:
    """One serial CDP connection: preserve network events while awaiting commands."""
    def __init__(self, url):
        self.ws = create_connection(url, timeout=1, suppress_origin=True)
        self.sequence = 0
        self.requests = {}
        self.responses = deque()
        self.documents = deque()
        self.navigation = {}
        self.document_id = None
        self.call("Network.enable", {"maxTotalBufferSize": 20000000, "maxResourceBufferSize": 5000000,
                                     "maxPostDataSize": 65536})
        self.call("Page.enable")
        self.call("Runtime.enable")

    def receive(self):
        message = json.loads(self.ws.recv())
        params = message.get("params", {})
        request_id = params.get("requestId")
        method = message.get("method")
        if method == "Network.requestWillBeSent":
            request = params["request"]
            if params.get("type") == "Document":
                self.document_id = request_id
                self.navigation = {"stage": "document_requested", "host": urlparse(request["url"]).hostname}
            if urlparse(request["url"]).path in (SEARCH, FEED):
                self.requests[request_id] = {"request": request}
        elif method == "Network.responseReceived":
            if request_id == self.document_id:
                self.navigation.update(stage="document_response", status=params["response"]["status"])
            if request_id in self.requests:
                self.requests[request_id]["response"] = params["response"]
            if params["response"]["status"] >= 400 and (
                params.get("type") == "Document" or "/api/sns/" in params["response"].get("url", "")
            ):
                self.documents.append(params["response"])
        elif method == "Network.loadingFailed" and request_id == self.document_id:
            self.navigation.update(stage="document_failed", error=params.get("errorText", "unknown"))
        elif method == "Network.loadingFinished" and request_id in self.requests:
            entry = self.requests.pop(request_id)
            if "response" in entry:
                self.responses.append((request_id, entry))
        elif method == "Network.loadingFailed" and request_id in self.requests:
            self.requests.pop(request_id)
            raise RuntimeError("浏览器业务请求加载失败，保留断点后重试")
        return message

    def call(self, method, params=None, timeout=15):
        self.sequence += 1
        call_id = self.sequence
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.receive()
            except WebSocketTimeoutException:
                continue
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} 失败 (code={message['error'].get('code')})")
                return message.get("result", {})
        raise TimeoutError(f"CDP {method} 超时；页面网络状态={json.dumps(self.navigation, ensure_ascii=False)}")

    def evaluate(self, expression):
        # Vue reactive proxies serialize as empty objects through CDP's native by-value copier.
        # Materialize them in the page first, then decode the JSON string locally.
        result = self.call("Runtime.evaluate", {
            "expression": f"JSON.stringify(({expression}))", "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError("浏览器页面状态读取失败")
        value = result.get("result", {}).get("value")
        return json.loads(value) if isinstance(value, str) else None

    def take_response(self, path, matches):
        for request_id, entry in list(self.responses):
            request = entry["request"]
            if urlparse(request["url"]).path != path:
                continue
            try:
                body = json.loads(request.get("postData") or "{}")
            except ValueError:
                continue
            if not matches(body):
                continue
            self.responses.remove((request_id, entry))
            raw = self.call("Network.getResponseBody", {"requestId": request_id})
            content = raw.get("body", "")
            if raw.get("base64Encoded"):
                content = base64.b64decode(content)
            # CDP returns the decoded entity body, not compressed wire bytes.
            headers = {k: v for k, v in entry["response"].get("headers", {}).items()
                       if k.lower() not in ("content-encoding", "content-length")}
            return httpx.Response(int(entry["response"]["status"]), headers=headers, content=content)
        return None

    def navigate(self, url):
        self.responses.clear()
        self.documents.clear()
        self.requests.clear()
        self.document_id = None
        self.navigation = {"stage": "navigation_sent", "host": urlparse(url).hostname}
        logger.info(f"CDP 导航开始：{urlparse(url).hostname}（最多等待45秒）")
        self.call("Page.bringToFront")
        result = self.call("Page.navigate", {"url": url}, timeout=45)
        if result.get("errorText"):
            raise RuntimeError(f"浏览器页面导航失败：{result['errorText']}")

    def scroll(self):
        self.call("Page.bringToFront")
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 900, "y": 600, "deltaX": 0, "deltaY": 10000,
        })

    def close(self):
        self.ws.close()

class AsyncXHSClient:
    """RPC所需的限速、响应检查、CDP生命周期；没有HTTP备用通道。"""
    def __init__(self):
        self._request_delay = (60.0, 75.0)
        self._last_request_time = 0.0
        self._rate_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._actions_since_rest = 0
        self.test_request_limit = None
        self.test_requests_used = 0
        self.process = self.search_tab = None

    async def _rate_limit(self, is_heavy: bool = False):
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time

            delay = random.uniform(*self._request_delay)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_request_time = time.monotonic()

    def _check_acw(self, resp: httpx.Response) -> bool:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" not in ctype:
            return False
        text = (resp.text or "").lower()
        return any(ind.lower() in text for ind in ACW_INDICATORS)

    def _check_access_response(self, resp: httpx.Response, method: str, path: str):
        """Log bounded metadata, not bodies/tokens; stop instead of retrying access errors."""
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        if isinstance(payload, dict):
            # Never classify user-written note content as a server risk message.
            message = "" if code == 0 and payload.get("success") is not False else str(
                payload.get("msg") or payload.get("message") or ""
            )
        else:
            message = resp.text or ""
        hit = check_response_risk_control(message)
        redirect = resp.status_code in (301, 302, 303, 307, 308) and any(
            word in resp.headers.get("location", "").lower() for word in ("acw", "challenge", "login")
        )
        reason = ""
        error = TransientRiskError
        if code in (-100, -101, -104) or resp.status_code == 401:
            reason = "登录或权限异常，请先人工确认登录状态"
        elif resp.status_code == 429:
            reason = "服务器限流，请遵守 Retry-After，停止本轮"
        elif hit or redirect or self._check_acw(resp):
            reason = f"疑似访问验证或频率限制（{hit or 'challenge/login'}）"
            error = RiskControlError
        elif resp.status_code in (403, 406, 461):
            reason = "访问被拒绝，具体原因待排查"
        elif resp.status_code >= 500 or "请稍后再试" in message:
            reason = "临时服务错误或疑似限制，不能仅凭此认定风控"
        if reason or resp.status_code != 200 or (code is not None and code != 0):
            # Do not include a full URL/query, arbitrary response message, or credentials.
            safe_code = code if isinstance(code, (int, float)) else "unknown"
            ctype = resp.headers.get("content-type", "unknown").split(";", 1)[0][:60]
            retry_after = resp.headers.get("retry-after", "未提供")[:80]
            summary = (f"{method} {path.split('?', 1)[0]} HTTP {resp.status_code} "
                       f"code={safe_code} Content-Type={ctype!r} Retry-After={retry_after!r}")
            logger.warning(f"{summary}：{reason or '接口返回异常'}")
            if reason:
                raise error(f"{summary}：{reason}；已停止，不自动重试或重新登录")

    async def get(self, path: str, is_heavy: bool = False) -> httpx.Response:
        return await self.request("GET", path, is_heavy=is_heavy)

    async def post(self, path: str, body: dict, is_heavy: bool = False) -> httpx.Response:
        return await self.request("POST", path, body, is_heavy=is_heavy)

    async def _run(self, function, *args):
        # Don't close the socket concurrently with a still-running CDP worker on Ctrl+C.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _action(self, function, *args):
        # 预算在页面原生调用前扣除；失败也计数，不重发。
        if self.test_request_limit is not None and self.test_requests_used >= self.test_request_limit:
            raise TestBudgetExhausted()
        if self._actions_since_rest >= 30:
            logger.info("浏览器已执行30次操作，休息5分钟；可 Ctrl+C 停止，下次按断点续采")
            await asyncio.sleep(300)
            self._actions_since_rest = 0
        await self._rate_limit()
        self._actions_since_rest += 1
        self.test_requests_used += 1
        return await self._run(function, *args)

    def _check_page(self, tab):
        while tab.documents:
            metadata = tab.documents.popleft()
            self._check_access_response(httpx.Response(int(metadata["status"]),
                                        headers=metadata.get("headers", {})), "GET", "/browser-page")
        status = tab.evaluate(r"""(() => {
            const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length);
            // Redirect query is available before the error page's text has rendered.
            const code = new URLSearchParams(location.search).get('error_code');
            return {url: location.pathname,
                errorCode: location.pathname.startsWith('/website-login/') ?
                    (/^\d{6}$/.test(code || '') ? code : document.body?.innerText?.match(/\b\d{6}\b/)?.[0]) : null,
                login: visible('.login-container'),
                verify: visible('#aliyunCaptcha-window-popup, .geetest_panel, .captcha-container')};
        })()""") or {}
        if status.get("url", "").startswith("/website-login/"):
            code = status.get("errorCode") or "unknown"
            reason = {"300013": "访问频繁，请稍后再试", "300017": "访问链接异常"}.get(code, "原因未明")
            raise RiskControlError(f"浏览器访问被限制（code={code}，{reason}），已停止；不自动重试或重新登录")
        if status.get("login") or status.get("verify") or any(
            part in status.get("url", "").lower() for part in ("/login", "/captcha", "/verify")
        ):
            raise RiskControlError("浏览器需要人工登录或验证；已停止，请在专用浏览器中人工处理后重新运行")

    async def close(self):
        await self._run(self._close_browser)

class AsyncBrowserRPCClient(AsyncXHSClient):
    def __init__(self, port=9223):
        super().__init__()
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("RPC 调试端口必须在 1–65535 之间")
        self.port = port
        self._stopped = False
        self._owns_bridge = False

    def _list_tabs(self):
        # Never send local CDP discovery through an environment/system proxy.
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{self.port}/json/list", timeout=5
        ) as response:
            tabs = json.load(response)
        if not isinstance(tabs, list) or not all(isinstance(t, dict) for t in tabs):
            raise RuntimeError("本机端口返回的不是 CDP 标签页列表，不启动第二个浏览器")
        return tabs

    def _launch_headed(self):
        profile = STATE_DIR / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        logger.info(f"RPC 端口 {self.port} 未启动，自动打开有头浏览器，复用原登录状态")
        self.process = subprocess.Popen([
            _find_browser_executable(), f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
            *browser_proxy_args(), "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,900", "about:blank",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        _wait_for_cdp(self.port, process=self.process)

    def _start(self):
        if self.search_tab is not None:
            return
        launched = False
        try:
            tabs = self._list_tabs()
        except URLError as exc:
            # Only a refused connection proves no listener. Timeout/bad HTTP must not spawn Chrome.
            if not isinstance(exc.reason, ConnectionRefusedError):
                raise RuntimeError(f"RPC 端口 {self.port} 连接异常；未自动启动浏览器，请检查端口") from None
            self._launch_headed()
            launched = True
            tabs = self._list_tabs()
        site_tabs = [t for t in tabs if t.get("type") == "page" and
                     urlparse(t.get("url", "")).scheme == "https" and
                     urlparse(t.get("url", "")).netloc == "www.xiaohongshu.com"]
        bootstrap = launched and not site_tabs
        tabs = ([t for t in tabs if t.get("type") == "page" and t.get("url") == "about:blank"]
                if bootstrap else site_tabs)
        if len(tabs) != 1:
            raise RuntimeError("RPC 浏览器必须只有一个小红书标签页；请人工登录并打开搜索页后再运行")
        ws_url = tabs[0].get("webSocketDebuggerUrl", "")
        ws = urlparse(ws_url)
        if (ws.scheme != "ws" or ws.hostname not in ("127.0.0.1", "localhost", "::1") or
                ws.port != self.port or ws.username or ws.password):
            raise RuntimeError("拒绝非本机 RPC 调试连接")
        self.search_tab = CDPTab(ws_url)
        if bootstrap:
            self.search_tab.navigate(START_URL)
        self._check_page(self.search_tab)
        previous = self.search_tab.evaluate("""(() => {
            const r = window.__xhsCollectorRPC;
            return r ? {version:r.version, busy:r.busy, stopped:r.stopped, uncertain:r.uncertain} : null;
        })()""")
        if previous is not None:
            # stopped=false只代表JS对象未锁止，不代表旧Python还在运行；OS锁才是任务互斥依据。
            exclusive = _collector_lease is not None and not _collector_lease.closed
            if (not isinstance(previous, dict) or previous.get("version") != 1
                    or previous.get("busy") is not False or type(previous.get("stopped")) is not bool
                    or previous.get("uncertain", previous.get("stopped")) is not False or not exclusive):
                raise RuntimeError("页面RPC正在执行、结果不确定、版本未知或未持有采集锁，不接管、不刷新；"
                                   "请先停止旧任务；结果不确定时须人工检查并刷新原页面后再运行")
            # Promise.race超时不取消底层请求；旧版stopped且无uncertain也不能证明安全。
            released = self.search_tab.evaluate("""(() => {
                const r = window.__xhsCollectorRPC;
                if (!r || r.version !== 1 || r.busy !== false || typeof r.stopped !== 'boolean' ||
                    (r.uncertain === undefined ? r.stopped : r.uncertain) !== false || !EXCLUSIVE) return false;
                delete window.__xhsCollectorRPC; return true;
            })()""".replace("EXCLUSIVE", "true" if exclusive else "false"))
            if released is not True:
                raise RuntimeError("上轮RPC连接状态已变化，不接管、不重试")
            logger.info("释放已确认空闲的旧RPC对象；复用原页面，不刷新、不重新搜索")
        deadline = time.monotonic() + 30
        while True:
            self._check_page(self.search_tab)
            if self.search_tab.evaluate("document.readyState === 'complete' && !!window.__INITIAL_STATE__ && !window.__xhsCollectorRPC"):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("浏览器页面尚未就绪；请人工检查登录/页面后重新运行，未发起 RPC 采集")
            time.sleep(0.5)
        installed = self.search_tab.evaluate(BRIDGE_JS)
        if not isinstance(installed, dict) or installed.get("ready") is not True:
            raise RuntimeError("页面原生 HTTP 客户端未就绪或版本变化；不回退 fetch/HTTP，请检查页面")
        self._owns_bridge = True
        logger.info("浏览器 RPC 已连接：原生请求链路，单并发，间隔%s–%s秒，每30次请求休息5分钟",
                    *self._request_delay)

    def _invoke(self, path, body):
        is_comment = urlparse(path).path == COMMENT
        method = "GET" if is_comment else "POST"
        self._check_page(self.search_tab)
        args = json.dumps([path, body], ensure_ascii=True, allow_nan=False)
        expression = f"""(async () => {{
            const rpc = window.__xhsCollectorRPC;
            if (!rpc || rpc.version !== 1) return JSON.stringify({{ok:false, reason:'bridge_missing'}});
            return JSON.stringify(await rpc.call(...{args}));
        }})()"""
        result = self.search_tab.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        }, timeout=45)
        # Also inspect native HTTP failures and login/verification redirects caught by CDP.
        self._check_page(self.search_tab)
        if "exceptionDetails" in result:
            raise RuntimeError("RPC 页面执行失败，结果不确定；停止且不重发")
        try:
            reply = json.loads(result.get("result", {}).get("value", ""))
        except (ValueError, TypeError):
            raise RuntimeError("RPC 返回格式异常；停止且不重发") from None
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            status = reply.get("status") if isinstance(reply, dict) else None
            code = reply.get("code") if isinstance(reply, dict) else None
            status = status if type(status) is int and 100 <= status <= 599 else None
            code = code if type(code) is int else None
            if status is not None:
                self._check_access_response(httpx.Response(status, json={"code": code}), method, urlparse(path).path)
            reason = reply.get("reason") if isinstance(reply, dict) else "invalid_reply"
            reason = reason if reason in ("timeout_no_retry", "native_request_failed", "bridge_missing",
                "bridge_stopped_or_busy", "page_not_available", "invalid_comment", "invalid_request") else "unknown"
            raise RuntimeError(f"页面原生请求失败或超时（{reason}, HTTP={status}, code={code}）；结果可能不确定，不自动重发")
        payload = reply.get("payload")
        if not isinstance(payload, dict) or "code" not in payload:
            raise RuntimeError("原生客户端未返回完整业务响应，停止以免误判末页")
        # A synthetic response adapts the native client's resolved envelope to the existing collector.
        response = httpx.Response(200, json=payload)
        self._check_access_response(response, method, urlparse(path).path)
        if type(payload.get("code")) is not int or payload["code"] != 0 or payload.get("success") is False:
            raise RuntimeError("RPC 业务响应失败；停止并保留断点")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("comments" if is_comment else "items"), list):
            raise RuntimeError("RPC 响应缺少笔记/评论列表；不能当作空结果或末页")
        if (path == SEARCH or is_comment) and type(data.get("has_more")) is not bool:
            raise RuntimeError("RPC 搜索缺少明确的 has_more，不能判定已翻完")
        return response

    async def request(self, method, path, body=None, is_heavy=False):
        parsed = urlparse(path)
        comment = (method == "GET" and not parsed.scheme and not parsed.netloc and parsed.path == COMMENT
                   and body is None and bool(parse_qs(parsed.query).get("note_id")))
        if not comment and (method != "POST" or path not in (SEARCH, FEED) or not isinstance(body, dict)):
            raise ValueError("RPC 只允许笔记搜索/详情POST及一级评论GET")
        # JSON serialization is also the trust boundary: no NaN, code strings or custom objects.
        json.dumps(body, allow_nan=False)
        async with self._operation_lock:
            if self._stopped:
                raise RuntimeError("RPC 本轮已停止，不允许自动重试")
            try:
                await self._run(self._start)
                return await self._action(self._invoke, path, body)
            except BaseException:
                self._stopped = True
                raise

    def _close_browser(self):
        # Attached browser belongs to the user: close only our CDP socket, never Browser.close.
        try:
            if self.search_tab is not None:
                if self._owns_bridge:
                    try:
                        self.search_tab.call("Runtime.evaluate", {"expression":
                            "(() => { const r = window.__xhsCollectorRPC; if (!r) return; "
                            "if (!r.busy && !r.stopped && r.uncertain === false) delete window.__xhsCollectorRPC; "
                            "else r.stopped = true; })()"}, timeout=3)
                    except Exception:
                        pass  # Never attempt a second business request while cleaning up.
                self.search_tab.close()
        finally:
            self.search_tab = None
            self._owns_bridge = False
            self._stopped = True

_IP_LOCATION_KEYS = ("ip_location", "ipLoc", "ipLocation", "ip_address", "ipAddress")
def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _format_time(ts_ms):
    value = timestamp(ts_ms)
    return value.replace(" ", "T") + "+08:00" if value else None


def _first_text(*values):
    for v in values:
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
            return v
    return ""


def _extract_ip_location(note: dict, item: dict) -> str:
    for k in _IP_LOCATION_KEYS:
        v = note.get(k) or item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def extract_note(item: dict) -> dict:
    # Browser SSR uses camelCase while the API uses snake_case.
    note = item.get("note_card") or item.get("noteCard") or item.get("note") or item
    user = (note.get("user") or note.get("user_info") or note.get("userInfo")
            or item.get("user") or item.get("user_info") or item.get("userInfo") or {})
    interact = (note.get("interact_info") or note.get("interactInfo")
                or item.get("interact_info") or item.get("interactInfo") or {})
    note_id = _first_text(note.get("note_id"), note.get("noteId"), item.get("id"),
                          item.get("note_id"), item.get("noteId"))
    xsec_token = _first_text(item.get("xsec_token"), item.get("xsecToken"),
                             note.get("xsec_token"), note.get("xsecToken"))
    image_urls = _extract_image_urls(note)
    video_url = _extract_video_url(note)
    return {
        "note_id": note_id,
        "xsec_token": xsec_token,
        "title": _first_text(note.get("display_title"), note.get("displayTitle"), note.get("title"), note.get("desc")),
        "desc": _first_text(note.get("desc")),
        "tags": [tag["name"] for tag in (note.get("tag_list") or note.get("tagList") or [])
                 if isinstance(tag, dict) and tag.get("name")],
        "author": _first_text(user.get("nickname"), user.get("nick_name")),
        "author_user_id": _first_text(user.get("user_id"), user.get("userId"), user.get("id")),
        "author_xhs_id": _first_text(user.get("red_id"), user.get("redId")),
        "liked_count": _first_text(interact.get("liked_count"), interact.get("likedCount"), interact.get("like_count"), interact.get("likeCount")),
        "collected_count": _first_text(interact.get("collected_count"), interact.get("collectedCount"), interact.get("save_count"), interact.get("saveCount")),
        "comment_count": _first_text(interact.get("comment_count"), interact.get("commentCount")),
        "share_count": _first_text(interact.get("share_count"), interact.get("shareCount"), interact.get("shared_count"), interact.get("sharedCount")),
        "type": _first_text(note.get("type"), item.get("model_type"), "未知"),
        "publish_time": _format_time(_first_text(note.get("time"), note.get("create_time"),
                                                  note.get("createTime"), item.get("time"),
                                                  item.get("create_time"), item.get("createTime"))) or "",
        "ip_location": _extract_ip_location(note, item),
        "image_urls": image_urls,
        "video_url": video_url,
        "video_duration": _extract_video_duration(note),
    }


def _extract_image_urls(note: dict) -> list[str]:
    """从 note_card.image_list 提取所有图片 URL（按清晰度优先级：url_pre > url_default > url）"""
    image_list = note.get("image_list") or note.get("imageList") or []
    urls = []
    for img in image_list:
        if not isinstance(img, dict):
            continue
        url = _first_text(img.get("url_pre"), img.get("urlPre"), img.get("url_default"), img.get("urlDefault"), img.get("url"))
        if url:
            urls.append(url)
    return urls


def _extract_video_url(note: dict) -> str:
    """从 note_card.video.media.stream 取最佳视频 URL

    新版 feed 可用 EF4/EF5/EF6/EF7 等动态流键；每档优先默认流，再选高分辨率。
    """
    video = note.get("video") or note.get("videoInfo") or {}
    if not isinstance(video, dict):
        return ""
    media = video.get("media") or video.get("media_v2") or video.get("mediaInfo") or {}
    if not isinstance(media, dict):
        return ""
    stream = media.get("stream") or media.get("streamInfo") or {}
    if not isinstance(stream, dict):
        return ""

    candidates: list[str] = []
    # 优先沿用旧版 codec 排序；新版 EF* 键依照响应顺序逐一寻找可用流。
    for codec in dict.fromkeys(("h266", "h265", "h264", "av1", *stream)):
        sv = stream.get(codec)
        if isinstance(sv, dict):
            sv = [sv]
        if isinstance(sv, list) and sv:
            # 默认流优先，其次按分辨率降序（1080p > 720p）。
            sorted_sv = sorted(
                (x for x in sv if isinstance(x, dict)),
                key=lambda x: (bool(x.get("default_stream")), (x.get("width") or 0) * (x.get("height") or 0)),
                reverse=True,
            )
            for item in sorted_sv:
                if not isinstance(item, dict):
                    continue
                url = _first_text(item.get("master_url"), item.get("masterUrl"),
                                  item.get("url"), item.get("url_default"), item.get("urlDefault"))
                if url:
                    candidates.append(str(url))
                    break
                backup_urls = item.get("backup_urls") or item.get("backupUrls") or []
                if isinstance(backup_urls, list) and backup_urls:
                    candidates.append(str(backup_urls[0]))
                    break

    if not candidates:
        return ""
    # 第一个就是最佳（旧版 codec 优先级 / 新版 EF* 可用流）。
    return candidates[0]


def _extract_video_duration(note: dict) -> str:
    """从 note_card.video 取视频时长（秒，统一转字符串）

    路径优先级：video.media.video.duration > video.capa.duration > 视频流第一条 duration(ms/1000)
    """
    video = note.get("video") or note.get("videoInfo") or {}
    if not isinstance(video, dict):
        return ""
    media = video.get("media") or video.get("mediaInfo") or {}
    if isinstance(media, dict):
        # 新版结构：实际 duration 在 media.video.duration（已是秒）
        mv = media.get("video") or media.get("videoInfo")
        if isinstance(mv, dict):
            dur = mv.get("duration")
            if isinstance(dur, (int, float)) and dur:
                return str(int(dur))
            if isinstance(dur, str) and dur:
                return dur
        # 老结构兼容：media.duration
        dur = media.get("duration")
        if isinstance(dur, (int, float)) and dur:
            return str(int(dur))
        if isinstance(dur, str) and dur:
            return dur
        # 兜底：从 stream 第一条 duration（毫秒）换算
        stream = media.get("stream") or media.get("streamInfo") or {}
        if isinstance(stream, dict):
            for codec in ("h266", "h265", "h264", "av1"):
                sv = stream.get(codec)
                if isinstance(sv, dict):
                    sv = [sv]
                if isinstance(sv, list) and sv:
                    first = sv[0]
                    if isinstance(first, dict):
                        dms = first.get("duration")
                        if isinstance(dms, (int, float)) and dms:
                            return str(int(dms // 1000))
    # capa 备用：capa.duration（秒）
    capa = video.get("capa")
    if isinstance(capa, dict):
        dur = capa.get("duration")
        if isinstance(dur, (int, float)) and dur:
            return str(int(dur))
        if isinstance(dur, str) and dur:
            return dur
    # 顶层 duration（旧版兼容）
    dur = video.get("duration")
    if isinstance(dur, (int, float)) and dur:
        return str(int(dur))
    if isinstance(dur, str) and dur:
        return dur
    return ""


def _build_video_detail(detail: dict) -> dict | None:
    """把 extract_note 返回的 video_url 包装成 {url, ...}，无视频返回 None"""
    url = detail.get("video_url") or ""
    if not url:
        return None
    return {
        "url": url,
        "duration": detail.get("video_duration", "") or "",
        "type": detail.get("type", ""),
    }


def extract_note_detail(payload: dict) -> dict:
    data = payload.get("data") or {}
    items = data.get("items") or []
    if not items:
        return {}
    return extract_note(items[0])


async def async_fetch_note_detail(client: AsyncXHSClient, note: dict) -> dict:
    """获取笔记完整详情并合并（对齐 exe fetch_note_detail）"""
    note_id = note["note_id"]
    xsec_token = note.get("xsec_token", "")
    resp = await async_fetch_note(client, note_id, xsec_token)
    if "error" in resp:
        merged = dict(note)
        merged.update(resp)
        return merged
    detail = extract_note_detail(resp.get("__raw_payload__", {}))
    if str(detail.get("note_id")) != str(note_id):
        raise RuntimeError("小红书详情ID与候选ID不一致，拒绝补写")
    merged = dict(note)
    merged.update({k: v for k, v in detail.items() if present(v)})
    return merged


async def async_fetch_note(client: AsyncXHSClient, note_id: str,
                           xsec_token: str = "", xsec_source: str = "pc_search") -> dict:
    """异步获取单条笔记原始 feed 响应"""
    body = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": "1"},
        "xsec_token": xsec_token or "",
        "xsec_source": xsec_source or "pc_search",
    }
    resp = await client.request("POST", FEED_PATH, body, is_heavy=True)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}
    try:
        payload = resp.json()
    except Exception:
        return {"error": "非 JSON 响应"}
    if payload.get("code") != 0:
        logger.info(f"feed 接口返回 code={payload.get('code')}: {payload.get('msg', '')}")
        return {"error": payload.get("msg"), "code": payload.get("code"), "raw": payload}
    items = payload.get("data", {}).get("items", [])
    if not items:
        return {"error": "no items"}
    payload["__items"] = items
    return {"__raw_payload__": payload}


async def async_fetch_comments(client: AsyncXHSClient, note_id: str,
                               cursor: str = "", xsec_token: str = "",
                               rate_limiter=None) -> dict:
    """异步获取一级评论"""
    if rate_limiter is not None:
        await rate_limiter.acquire()
    params = {
        "note_id": note_id,
        "cursor": cursor,
        "top_comment_id": "",
        "num": 10,
        "image_formats": "jpg,webp,avif",
        "xsec_token": xsec_token or "",
    }
    query = urllib.parse.urlencode(params)
    resp = await client.request("GET", COMMENT_PATH + "?" + query)
    if resp.status_code != 200:
        return {"code": -1, "msg": f"HTTP {resp.status_code}"}
    try:
        return resp.json()
    except Exception:
        return {"code": -2, "msg": "非 JSON 响应"}


async def iter_search_notes(client: AsyncXHSClient, keyword: str,
                            max_notes: int | None = None, max_pages: int | None = None,
                            cache_path: Path | None = None):
    """Replay durable candidate pages first, then request the next uncached page."""
    import uuid
    cache = {"keyword": keyword, "search_id": uuid.uuid4().hex, "pages": []}
    if cache_path is not None and cache_path.exists():
        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        if stored.get("keyword") == keyword:
            cache = stored
    seen = set()
    page = 1
    yielded = 0
    while max_pages is None or page <= max_pages:
        if page <= len(cache["pages"]):
            data = cache["pages"][page - 1]
            logger.info(f"搜索 {keyword}：复用缓存第 {page} 页，不重复请求搜索接口")
        else:
            logger.info(f"搜索 {keyword}：开始请求第 {page} 页（当前累计 {yielded} 个候选）")
            body = {
                "keyword": keyword, "page": page, "page_size": 20,
                "search_id": cache["search_id"], "sort": "general", "note_type": 0,
                "ext_flags": [], "geo": "", "image_formats": ["jpg", "webp", "avif"],
            }
            resp = await client.post(SEARCH_NOTES_PATH, body)
            if resp.status_code != 200:
                raise RuntimeError(f"搜索笔记 HTTP {resp.status_code}")
            payload = resp.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"搜索笔记失败: {payload.get('code')} {payload.get('msg', '')}")
            raw = payload.get("data") or {}
            if not isinstance(raw.get("has_more"), bool):
                raise RuntimeError(f"搜索 {keyword} 第 {page} 页缺少明确的 has_more，不能判定已翻完")
            notes = []
            for item in raw.get("items") or []:
                if not isinstance(item, dict) or not isinstance(item.get("note_card"), dict):
                    continue
                note = extract_note(item)
                if not note.get("note_id"):
                    continue
                if not note.get("xsec_token"):
                    raise RuntimeError(f"搜索 {keyword} 第 {page} 页笔记 {note['note_id']} 缺少 xsec_token，待重试")
                notes.append(note if cache_path is None else {
                    key: note.get(key, "") for key in ("note_id", "xsec_token", "publish_time")
                })
            data = {"has_more": raw["has_more"], "notes": notes}
            if data["has_more"] and not any(n["note_id"] not in seen for n in notes):
                raise RuntimeError(f"搜索 {keyword} 第 {page} 页没有新笔记却声明还有下一页")
            cache["pages"].append(data)
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                # ponytail: rewrite one keyword's pages; use per-page files if cache size becomes material.
                temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                temporary.replace(cache_path)  # Whole page is durable before yielding any candidate.
            logger.info(f"搜索 {keyword}：第 {page} 页返回 {len(notes)} 个候选，has_more={data['has_more']}")
        before = len(seen)
        for note in data["notes"]:
            note_id = note["note_id"]
            if note_id in seen:
                continue
            seen.add(note_id)
            yield note
            yielded += 1
            if max_notes is not None and yielded >= max_notes:
                return
        logger.info(f"搜索 {keyword} 第 {page} 页：新增 {len(seen) - before} 个候选，累计 {yielded} 个")
        if data["has_more"] is False:
            break
        if len(seen) == before:
            raise RuntimeError(f"搜索 {keyword} 第 {page} 页没有新笔记却声明还有下一页")
        page += 1


async def async_search_notes(client: AsyncXHSClient, keyword: str,
                             max_notes: int = 10, max_pages: int = 3) -> list[dict]:
    """Collect a bounded list of search results for interactive/trial use."""
    return [note async for note in iter_search_notes(client, keyword, max_notes=max_notes, max_pages=max_pages)]


async def async_collect_comments(client, note_id, xsec_token, store, max_pages=None, rate_limiter=None):
    """按实际游标遍历，不以不足10条、取满20条或置顶顺序作为结束条件。"""
    store.register(note_id)
    state, pages = store.state(note_id), 0
    try:
        while not state["done"] and (max_pages is None or pages < max_pages):
            payload = await async_fetch_comments(client, note_id, state["cursor"], xsec_token, rate_limiter)
            data = payload.get("data") or {}
            comments, more = data.get("comments"), data.get("has_more")
            if payload.get("code") != 0 or not isinstance(comments, list) or type(more) is not bool:
                raise RuntimeError("小红书评论响应异常或缺少明确的 has_more，不能当作无评论")
            cursor = data.get("cursor")
            if more and (not isinstance(cursor, str) or not cursor):
                raise RuntimeError("小红书评论缺少下一页 cursor")
            rows = []
            for comment in comments:
                c = _extract_l1(comment)
                user = c.get("user") or {}
                rows.append({"评论ID": c["comment_id"], "评论内容": c.get("content"),
                             "评论点赞数": c["liked"], "评论时间": c["time"],
                             "评论者昵称": user.get("nickname"), "评论者ID": user.get("user_id"),
                             "IP属地": c.get("ip_location")})
            store.save_page(note_id, rows, cursor or "", not more)
            state, pages = store.state(note_id), pages + 1
        if not state["done"]:
            store.mark(note_id, "页数上限，待续采")
    except BaseException:
        store.mark(note_id, "采集失败/中断，待续采")
        raise


def _extract_l1(c: dict) -> dict:
    user = c.get("user_info", {}) or c.get("user", {}) or {}
    liked_bool = c.get("liked")
    result = dict(c)  # 保留 API 所有原始字段
    result.pop("user_info", None)
    result.pop("user", None)
    result["comment_id"] = c.get("id") or c.get("comment_id")
    result["user"] = {
        **user,
        "xhs_id": user.get("red_id") or user.get("user_id", ""),
    }
    result["commenter_nickname"] = user.get("nickname", "")
    result["time"] = _format_time(c.get("create_time"))
    result["liked_by_viewer"] = liked_bool
    result["liked"] = exact_count(c.get("like_count"))   # 未知点赞数不能伪装为0
    result["sub_comment_count"] = _as_int(c.get("sub_comment_count"))
    result["level2"] = []
    return result
async def async_collect_note(client: AsyncXHSClient, note_id: str,
                             xsec_token: str = "", comment_limit: int | None = 20,
                             date_filter: tuple[str | None, str | None] | None = None,
                             known_comment_count: int | None = None,
                             comment_rate_limiter=None,
                             include_author_red_id: bool = False) -> dict:
    """详情 + 可见一级评论遍历。comment_limit 是导出条数，不是候选截断数。"""
    if comment_limit not in (None, 0, 20):
        raise ValueError("comment_limit 只支持0（跳过）或20（高赞），不限制评论候选分页")
    detail = await async_fetch_note_detail(client, {"note_id": note_id, "xsec_token": xsec_token})
    if "error" in detail:
        return {"note_id": note_id, "status": "unavailable" if detail.get("code") == 300031 else "failed",
                "error": detail["error"]}
    result = {
        "note_id": note_id, "status": "success",
        "content": {
            "user": {"nickname": detail.get("author", ""), "user_id": detail.get("author_user_id", ""),
                     "xhs_id": detail.get("author_xhs_id", "")},
            "title": detail.get("title", ""), "desc": detail.get("desc", ""), "tags": detail.get("tags", []),
            "time": detail.get("publish_time"), "ip_location": detail.get("ip_location"),
            "images": detail.get("image_urls") or [], "video": _build_video_detail(detail),
            "type": detail.get("type", ""),
        },
        "propagation": {k: exact_count(detail.get(field)) for k, field in (
            ("liked", "liked_count"), ("collected", "collected_count"),
            ("comment", "comment_count"), ("shared", "share_count"))},
        "comments": {"level1": []},
    }
    lower, upper = date_filter or (START_DATE, END_DATE)
    published = timestamp(detail.get("publish_time"))[:10]
    if not START_DATE <= published <= END_DATE or (lower and published < lower) or (upper and published > upper):
        result.update(status="skipped", skip_reason=f"publish_time={published} 不在允许日期范围内")
        return result
    if include_author_red_id:
        raise ValueError("单文件RPC不请求作者主页；请使用笔记已有作者字段")
    if comment_limit == 0:
        return result
    # 子命令使用内存候选库；批量入口在写好贴文后使用持久库独立续采评论。
    lower, upper = date_filter or (str(START), str(END))
    store = CommentStore(OUTPUT / "xhs_notes.csv", "小红书",
                         lower or START, upper or END, memory=True)
    try:
        await async_collect_comments(client, note_id, xsec_token, store, rate_limiter=comment_rate_limiter)
        result["comment_rows"] = [json.loads(r[0]) for r in store.db.execute(
            "SELECT payload FROM comments WHERE post_id=?", (note_id,))]
        result["comments"]["level1"] = [{"comment_id": r["评论ID"], "content": r["评论内容"],
            "liked": r["评论点赞数"], "time": r["评论时间"], "ip_location": r.get("IP属地"),
            "user": {"nickname": r.get("评论者昵称"), "user_id": r.get("评论者ID")}, "level2": []}
            for r in store.top(note_id)[:comment_limit or 20]]
        result["comments_complete"] = True
    finally:
        store.close()
    return result

LEGACY_COLUMNS = ['笔记标题', '正文', '话题标签', '作者', '发布时间', '点赞数', '收藏数', '评论数', '分享数', '笔记类型', 'IP属地', '视频链接']
COLUMNS = post_columns(LEGACY_COLUMNS)
def prepare_notes_csv(path, note_ids=None):
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as file:
        header = next(csv.reader(file), None)
        count = sum(1 for _ in csv.reader(file))
    if dataset_header(header, COLUMNS):
        read_posts(path, COLUMNS, "小红书")
        return
    if note_ids is None or len(note_ids) != count or len(set(note_ids)) != count:
        raise ValueError("旧笔记CSV没有ID，需要原 full_run_state.jsonl 的有序ID且行数一致；否则请使用新文件名")
    ids = iter(note_ids)
    upgrade_csv(path, LEGACY_COLUMNS, COLUMNS,
                lambda r: canonical_post("小红书", {**r, "笔记ID": next(ids)}))


def cached_note_keywords():
    """只按缓存中明确的笔记ID恢复搜索词，不用标题/文件名猜测。"""
    words = {}
    for path in sorted(STATE_DIR.glob("candidate_pages*.json")):
        cache = json.loads(path.read_text(encoding="utf-8"))
        keyword = cache.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        for page in cache.get("pages", []):
            for note in page.get("notes", []):
                if note.get("note_id"):
                    words.setdefault(str(note["note_id"]), keyword)
    return words


def write_notes_csv(results, output_path, overwrite=False):
    """统一表头、按ID去重，同时维护关联评论CSV；旧版无ID表必须先显式迁移。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        prepare_notes_csv(output_path)
    records = {} if overwrite else {r["内容ID"]: r for r in read_posts(output_path, COLUMNS, "小红书")}
    keywords = cached_note_keywords()
    for ident, row in records.items():
        if not present(row.get("搜索词")):
            row["搜索词"] = keywords.get(ident, "")
    accepted, added = [], 0
    for result in results:
        if result.get("status") != "success":
            continue
        content, p = result.get("content") or {}, result.get("propagation") or {}
        kind = content.get("type") or ""
        row = canonical_post("小红书", {
            "笔记ID": result.get("note_id"), "笔记标题": content.get("title", ""), "正文": content.get("desc", ""),
            "话题标签": "、".join(content.get("tags") or []), "作者": (content.get("user") or {}).get("nickname", ""),
            "发布时间": content.get("time"), "点赞数": p.get("liked"), "收藏数": p.get("collected"),
            "评论数": p.get("comment"), "分享数": p.get("shared"),
            "笔记类型": {"normal": "图文", "video": "视频"}.get(kind, kind),
            "IP属地": content.get("publish_ip_location") or content.get("ip_location"),
            "视频链接": (content.get("video") or {}).get("url") if kind == "video" else None,
            "搜索词": result.get("keyword", ""), "采集时间": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        })
        if not START_DATE <= row["发布时间"][:10] <= END_DATE:
            continue  # 写入端也校验；不能仅依赖调用者传入的success状态。
        ident = row["内容ID"]
        added += ident not in records
        records[ident] = merge_post(records.get(ident, {}), row)
        accepted.append(result)
    # ponytail: 整表原子重写以支持补字段和列恢复；超大数据量再迁入独立贴文库。
    export_csv(output_path, COLUMNS, records.values())
    for ident in dict.fromkeys(result["note_id"] for result in accepted):
        log_saved_row("小红书", records[ident])
    store = CommentStore(output_path, "小红书")
    try:
        if overwrite:
            with store.db:
                for table in ("comments", "pages", "posts"):
                    store.db.execute(f"DELETE FROM {table}")
        for note_id in records:
            store.register(note_id)
        for result in accepted:
            if result.get("comments_complete") and not store.state(result["note_id"])["done"]:
                store.save_page(result["note_id"], result.get("comment_rows") or [], "", True)
        store.export()
        store.report()
    finally:
        store.close()
    return added
def load_requirement_keywords(path: Path) -> list[str]:
    """Read every distinct keyword in the first markdown search table."""
    if path is None:
        return list(DEFAULT_KEYWORDS)
    text = path.read_text(encoding="utf-8").split("## 二、", 1)[0]
    words = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] in ("类别", "---") or cells[0].startswith("-"):
            continue
        words.extend(word.strip(" 。；，") for word in cells[1].split("、"))
    words.extend(("双减 课后服务", "校外培训 退费难"))
    return list(dict.fromkeys(word for word in words if word))
async def cmd_all_notes(args):
    """按词翻完可访问页，贴文先保存，再独立续采高赞评论。"""
    path = args.requirements
    if path is not None and not path.is_file():
        logger.error(f"找不到需求文档: {path}")
        return 1
    keywords = shard_queries(search_queries(load_requirement_keywords(path),
                                            yearly=not getattr(args, "no_year_queries", False)),
                             getattr(args, "shard", "full"))
    if not keywords:
        logger.error(f"需求文档没有搜索关键词: {path}")
        return 1
    output = OUTPUT / "小红书_双减_笔记.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    state = STATE_DIR / "full_run_state.jsonl"
    candidate_cache = STATE_DIR / "candidate_pages_rpc.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    if candidate_cache.exists():
        cached_keyword = json.loads(candidate_cache.read_text(encoding="utf-8")).get("keyword")
        if cached_keyword not in keywords:
            logger.error("候选缓存属于当前计划以外的关键词；请先完成或备份旧缓存，不自动覆盖")
            return 1
        keywords = [cached_keyword, *(w for w in keywords if w != cached_keyword)]
    client = AsyncBrowserRPCClient(port=getattr(args, "rpc_port", 9223))
    lower_delay = getattr(args, "min_delay", 60.0)
    client._request_delay = (lower_delay, lower_delay + 15.0)
    client.test_request_limit = getattr(args, "test_requests", None)
    logger.info("采集模式：RPC（单并发；访问限制即停止）")
    seen = set()
    excluded = set()
    completed = set()
    if state.exists():
        ordered_ids = []
        for line in state.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if "note_id" in entry:
                if entry["note_id"] not in seen:
                    ordered_ids.append(entry["note_id"])
                seen.add(entry["note_id"])
            if "skip_note_id" in entry:
                excluded.add(entry["skip_note_id"])
            if "retry_keyword" in entry:
                completed.discard(entry["retry_keyword"])
            if "keyword" in entry:
                completed.add(entry["keyword"])
        if output.exists():
            prepare_notes_csv(output, ordered_ids)
            with output.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
                rows_written = len(rows)
                csv_ids = [r.get("内容ID") for r in rows]
                if (not all(csv_ids) or len(set(csv_ids)) != rows_written or not seen <= set(csv_ids)
                        or any(None in r or any(v is None for v in r.values()) for r in rows)):
                    logger.error("断点与CSV的内容ID不一致或CSV损坏，拒绝猜测关联关系")
                    await client.close()
                    return 1
                # CSV先fsync，断点随后追加；崩溃窗口内的已写ID可由新表可靠恢复。
                missing = [ident for ident in csv_ids if ident not in seen]
                if missing:
                    with state.open("a", encoding="utf-8") as file:
                        for ident in missing:
                            file.write(json.dumps({"note_id": ident}, ensure_ascii=False) + "\n")
                        file.flush()
                        os.fsync(file.fileno())
                    seen.update(missing)
                    logger.info(f"从已落盘CSV恢复 {len(missing)} 条未提交ID断点")
        else:
            rows_written = 0
        if rows_written != len(seen):
            logger.error("断点记录与 CSV 行数不一致，为防止丢失或重复数据已停止，请检查文件")
            await client.close()
            return 1
        logger.info(f"从断点续采：已有 {rows_written} 条笔记，完成 {len(completed)} 个关键词")
    else:
        if output.exists():
            logger.error("CSV 已存在但去重断点缺失，无法安全增量采集；请恢复 full_run_state.jsonl")
            await client.close()
            return 1
        state.write_text("", encoding="utf-8")
    write_notes_csv([], output)
    comments = CommentStore(output, "小红书")
    attempted_comments = set()
    cache_files, saved_candidates = {}, {}
    for cached_path in sorted(STATE_DIR.glob("candidate_pages*.json"), key=lambda p: p.stat().st_mtime):
        cached = json.loads(cached_path.read_text(encoding="utf-8"))
        if cached_path.name.startswith("candidate_pages_rpc") and cached.get("keyword") in keywords:
            cache_files[cached["keyword"]] = cached_path
        for page in cached.get("pages", []):
            for note in page.get("notes", []):
                if note.get("note_id") in seen and note.get("xsec_token"):
                    saved_candidates[note["note_id"]] = note
    # 每个旧ID都有评论任务；缺令牌也必须明确报告，不能当成完成或静默略过。
    missing_tokens = [r[0] for r in comments.db.execute("SELECT id FROM posts WHERE done=0 ORDER BY rowid")
                      if r[0] not in saved_candidates]
    for ident in missing_tokens:
        comments.mark(ident, "缺少候选令牌，待搜索重新发现")
    if missing_tokens:
        logger.warning("旧贴文有%s篇评论缺少缓存令牌，已列为待补；不伪造令牌、不重置已完成搜索", len(missing_tokens))
    comments.report()
    saved = len(seen)
    logger.info(f"开始全量采集 {len(keywords)} 个不重复关键词，输出: {output}")

    def record(entry):
        with state.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())

    async def collect_note_comments(notes):
        pending = {note["note_id"]: note for note in notes if note["note_id"] not in attempted_comments}
        attempted_comments.update(pending)
        for note_id in comment_jobs(comments, pending, getattr(args, "comment_pages", None)):
            await async_collect_comments(client, note_id, pending[note_id]["xsec_token"], comments, max_pages=1)

    async def collect_one(note):
        nonlocal saved, accepted
        result = await async_collect_note(
            client, note["note_id"], note["xsec_token"], comment_limit=0,
            date_filter=(START_DATE, END_DATE), include_author_red_id=False,
        )
        note_id = note["note_id"]
        if result.get("status") == "failed":
            raise RuntimeError(f"笔记 {note_id} 详情失败: {result.get('error', '未知原因')}")
        if result.get("status") not in ("success", "skipped", "unavailable"):
            raise RuntimeError(f"笔记 {note_id} 状态异常: {result.get('status')}")
        published = ((result.get("content") or {}).get("time") or "")[:10]
        if result.get("status") == "unavailable":
            logger.warning(f"笔记 {note_id} 已不可访问，记录后不自动重采")
            excluded.add(note_id)
            record({"skip_note_id": note_id, "reason": "unavailable"})
        if published and not START_DATE <= published <= END_DATE:
            excluded.add(note_id)
            record({"skip_note_id": note_id})
        if result.get("status") != "success" or not START_DATE <= published <= END_DATE:
            return
        if result.get("note_id", note_id) != note_id:
            raise RuntimeError("笔记详情ID与候选ID不一致，拒绝写入错误关联")
        result["note_id"] = note_id
        result["keyword"] = keyword
        write_notes_csv([result], output)
        is_new = note_id not in seen
        if is_new:
            record({"note_id": note_id})
        seen.add(note_id)
        saved += int(is_new)
        accepted += int(is_new)
        logger.info("累计已存贴文 %s 条", saved)
        await collect_note_comments([note])

    try:
        await collect_note_comments(saved_candidates.values())
        for index, keyword in enumerate(keywords, 1):
            if keyword in completed:
                continue
            # 保留每个词的候选缓存；不删已获取令牌、不覆盖其他词的分页状态。
            candidate_cache = cache_files.get(keyword)
            if candidate_cache is None:
                legacy_cache = STATE_DIR / "candidate_pages_rpc.json"
                candidate_cache = legacy_cache if not legacy_cache.exists() else STATE_DIR / (
                    "candidate_pages_rpc_" + hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:16] + ".json")
                cache_files[keyword] = candidate_cache
            accepted = 0
            scanned = 0
            logger.info(f"开始关键词 {index}/{len(keywords)}：{keyword}，当前已写 {saved} 条")
            try:
                async for note in iter_search_notes(client, keyword, cache_path=candidate_cache):
                    scanned += 1
                    if scanned % 10 == 0:
                        logger.info(f"搜索 {keyword}：已处理 {scanned} 个候选，已写 {saved} 条")
                    note_id = note["note_id"]
                    if note_id in seen:
                        await collect_note_comments([note])
                        continue  # 已存详情不再请求；缺字段只通过 --repair-fields 显式补采。
                    if note_id in excluded:
                        continue
                    date = (note.get("publish_time") or "")[:10]
                    if date and not START_DATE <= date <= END_DATE:
                        excluded.add(note_id)
                        record({"skip_note_id": note_id})
                        continue
                    await collect_one(note)
            except TestBudgetExhausted:
                raise
            except Exception as exc:
                # 风控或单条/分页失败时保留断点，不把该关键词误标为已翻完。
                record({"retry_keyword": keyword, "error": f"{type(exc).__name__}: {exc}"[:500]})
                logger.error(f"关键词 {keyword} 未完整采集（{index}/{len(keywords)}），已记录待下次重试：{exc}")
                return 1
            else:
                # 保留本地候选页；下一次只跳过已处理ID，不重发已缓存搜索页。
                record({"keyword": keyword})
            logger.info(f"进度 {index}/{len(keywords)}：{keyword} 新增 {accepted} 条，累计 {saved} 条")
    except TestBudgetExhausted:
        logger.info("有界测试达到请求上限；不标记搜索完成，原游标和待补评论保留")
        return 2
    finally:
        try:
            comments.export()
            comments.report()
        finally:
            comments.close()
            await client.close()
    record({"run_complete": True})
    logger.info(f"本轮关键词可访问页已翻完：去重后 {saved} 条；CSV: {output}")
    report = json.loads(sidecar_path(output.with_name(output.stem + "_覆盖报告.json"), "reports").read_text(encoding="utf-8"))
    pending = report["未完成评论篇数"]
    if pending:
        logger.warning(f"还有 {pending} 篇评论未完成（可能缺少有效缓存令牌）；不自动重搜已完成关键词，详见覆盖报告")
    return 2 if pending else 0

async def repair_fields(limit, port):
    output = OUTPUT / "小红书_双减_笔记.csv"
    if not output.exists():
        raise ValueError("尚无贴文CSV，请先正常采集")
    prepare_notes_csv(output)
    candidates = {}
    # 最新缓存覆盖旧令牌；不打印或另存令牌，不用笔记标题猜ID。
    for path in sorted(STATE_DIR.glob("candidate_pages*.json"), key=lambda p: p.stat().st_mtime):
        cache = json.loads(path.read_text(encoding="utf-8"))
        for page in cache.get("pages", []):
            for note in page.get("notes", []):
                if note.get("note_id") and note.get("xsec_token"):
                    candidates[str(note["note_id"])] = {**note, "keyword": cache.get("keyword", "")}
    write_notes_csv([], output)
    client, repaired = AsyncBrowserRPCClient(port=port), 0
    try:
        for row in read_posts(output, COLUMNS, "小红书"):
            if (all(present(row.get(k)) for k in ("采集时间", "作者", "点赞数", "评论数", "收藏数", "分享数"))
                    or row["内容ID"] not in candidates):
                continue
            note = candidates[row["内容ID"]]
            result = await async_collect_note(client, row["内容ID"], note["xsec_token"], comment_limit=0,
                                              date_filter=(START_DATE, END_DATE))
            if result.get("status") != "success":
                raise RuntimeError("旧笔记详情不可用；停止补采，不伪造字段")
            result["keyword"] = note["keyword"]
            write_notes_csv([result], output)
            repaired += 1
            if repaired >= limit:
                break
    finally:
        await client.close()
    logger.info("旧笔记详情补采 %s 条；没有有效候选令牌的记录需后续搜索重新发现", repaired)
    return repaired


def check_environment():
    print(f"小红书：RPC依赖导入成功；CSV目录 {OUTPUT}；登录有效性待运行时确认。")


def main():
    parser = argparse.ArgumentParser(description="小红书全量RPC采集，CSV写根目录output/csv")
    parser.add_argument("--requirements", type=Path, default=None, help="可选Markdown词表；默认文件内DEFAULT_KEYWORDS")
    parser.add_argument("--no-year-queries", action="store_true")
    parser.add_argument("--shard", choices=("full", "forward", "reverse"), default="full")
    parser.add_argument("--comment-pages", type=int)
    parser.add_argument("--repair-fields", type=int, metavar="N", help="用缓存令牌最多补采N条旧笔记详情，不推进搜索/评论断点")
    parser.add_argument("--rpc-port", type=int, default=9223)
    parser.add_argument("--min-delay", type=float, default=60.0, help="最低操作间隔秒，默认60；超高速档可降至10，随机增加0–15秒；低于60未经长期验证")
    parser.add_argument("--test-requests", type=int, help="有界采集测试请求上限；到限时原断点待续并退出2")
    parser.add_argument("--transport", choices=("rpc",), default="rpc", help="单文件只保留当前全量RPC通道")
    parser.add_argument("--check", action="store_true", help="仅检查本地环境，不打开浏览器/不采集")
    args = parser.parse_args()
    if args.comment_pages is not None and args.comment_pages < 1 or not 1 <= args.rpc_port <= 65535:
        parser.error("评论页数必须大于0，RPC端口须为1–65535")
    if not math.isfinite(args.min_delay) or not 10 <= args.min_delay <= 3600:
        parser.error("最低操作间隔必须为10–3600秒的有限数")
    if args.repair_fields is not None and args.repair_fields < 1:
        parser.error("repair-fields 必须大于0")
    if args.test_requests is not None and (args.test_requests < 1 or args.repair_fields is not None):
        parser.error("test-requests 必须大于0且不能同时使用 repair-fields")
    if args.shard != "full" and (args.requirements or args.no_year_queries or args.repair_fields is not None
                                  or args.comment_pages is not None or args.test_requests is not None):
        parser.error("分片只支持原134词、完整日期和无限制分页；不能重置断点或截断请求")
    if args.check:
        check_environment()
        return 0
    if args.repair_fields is not None:
        asyncio.run(repair_fields(args.repair_fields, args.rpc_port))
        return 0
    return asyncio.run(cmd_all_notes(args))


if __name__ == "__main__":
    raise SystemExit(run_cli())
