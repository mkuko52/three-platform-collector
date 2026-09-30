"""抖音单文件采集器（浏览器模式）；直接运行本文件，CSV写入项目根目录的output/csv目录。"""
from __future__ import annotations
import argparse
import sys
import signal
import logging
# ponytail: 为三个单文件独立运行，公共CSV/状态逻辑各自内置；修改时需同步三份。
import csv
import json
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
STATE_DIR = ROOT / "runtime/douyin"
DEFAULT_KEYWORDS = ('双减', '双减政策', '义务教育双减', '减轻学生作业负担', '减轻校外培训负担', '作业管理', '五项管理', '校外培训治理', '课后服务', '校内课后服务', '课后延时服务', '延时托管', '三点半课堂', '三点半难题', '430课后服务', '430课堂', '课后看护', '校内托管', '午托', '晚托', '寒暑假校内托管', '课后服务收费', '普惠性课后服务', '课后托管', '托管班', '校外托管', '学生托管', '小饭桌', '作业托管', '假期托管', '寒暑假托管', '暑托班', '晚辅', '作业辅导班', '课外培训', '校外培训', '学科培训', '学科类培训', '非学科类培训', '补习班', '辅导班', '培训班', '培训机构', '教培', '教培行业', '培训学校', 'K12培训', '一对一辅导', '一对一家教', '小班课', '线上培训', '网课', '在线教育', '隐形变异培训', '无证办学', '办学资质', '无资质培训', '预收费', '收费监管', '资金监管', '培训广告', '广告治理', '黑白名单', '教培机构注销', '机构跑路', '退费难', '营转非', '非营利机构', '机构转型', '行政处罚', '吊销办学许可', '家长', '学生', '教师', '老师', '班主任', '校长', '教培从业者', '教培老师', '机构老板', '家长委员会', '焦虑', '教育焦虑', '鸡娃', '内卷', '减负', '减负不减负', '躺平', '佛系', '分流', '学区房', '教育公平', '负担', '压力', '睡眠不足', '近视', '心理健康', '抑郁', '厌学', '牛娃', '牛蛙', '普娃', '渣娃', '海淀妈妈', '顺义妈妈', '鸡血家长', '佛系养娃', '掐尖', '点招', '密考', '暗考', '抢跑', '起跑线', '剧场效应', 'AI辅导', 'AI家教', '人工智能辅导', '智能学习', '学习机', '作业帮', '猿辅导转型', '非学科类', '艺术培训', '体育培训', '编程教育', '科学素养', '研学旅行', '营地教育', '素质教育', '教师弹性上下班', '教师课后服务津贴', '教师负担', '双减 课后服务', '校外培训 退费难')
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("douyin")

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


class TestBudgetExhausted(RuntimeError):
    """本次有界测试达到请求上限；原状态保持待续。"""


def run_cli():
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        if any(flag in sys.argv[1:] for flag in ("--help", "-h", "--check", "--list-keywords")):
            return main()
        with collector_lock():
            logger.info("数据允许范围（北京时间）：%s 00:00:00 至 %s 23:59:59，含首尾日；按发布时间/评论时间筛选，不按采集时间", START, END)
            return main()
    except KeyboardInterrupt:
        return 130
    except TestBudgetExhausted:
        print("有界测试达到请求上限，原断点保留；未完成不等于采集成功", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"采集未完成：{exc}", file=sys.stderr)
        return 1


import re
import time
import random
import subprocess
from urllib.parse import parse_qs, quote, urlsplit, urlparse
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener, getproxies, proxy_bypass
from playwright.sync_api import Error as PlaywrightError, sync_playwright

LEGACY_COLUMNS = ('搜索词', '视频ID', '视频链接', '视频标题或描述', '话题标签', '作者', '发布时间', '点赞数', '评论数', '收藏数', '分享数', '视频时长(秒)', 'IP属地')
COLUMNS = post_columns(LEGACY_COLUMNS)
INTERVAL = 2.0
def cdp_ready(port):
    """仅明确拒绝连接才表示可以启动浏览器；坏端口/超时不能当成未启动。"""
    try:
        # Windows本机拒绝连接可能约2秒才返回；不能提前超时而阻止自动启动。
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
            data = json.load(response)
        ws = urlparse(data.get("webSocketDebuggerUrl", "")) if isinstance(data, dict) else urlparse("")
        if (ws.scheme != "ws" or ws.hostname not in ("localhost", "127.0.0.1", "::1")
                or ws.port != port or ws.username or ws.password):
            raise ValueError("not local CDP")
        return True
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False
        raise RuntimeError(f"本机 {port} 连接异常，请检查端口；不另起浏览器") from None
    except (ValueError, TypeError, OSError):
        raise RuntimeError(f"本机 {port} 不是正常的 CDP 端口；不另起浏览器") from None


def prepare_douyin_browser(port):
    if cdp_ready(port):
        return
    candidates = [os.environ.get("CHROME_PATH"), os.environ.get("EDGE_PATH"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")]
    browser = next((p for p in candidates if p and Path(p).is_file()), None)
    if not browser:
        raise RuntimeError("未找到 Chrome/Edge，请设置 CHROME_PATH 或先手动启动抖音 CDP 浏览器")
    # Chrome 不读取 HTTP(S)_PROXY；与 Python 保持同一已配置出口，不自动换代理。
    proxy_args = []
    proxies = getproxies()
    proxy = proxies.get("https") or proxies.get("http") or proxies.get("all")
    if proxy_bypass("www.douyin.com"):
        proxy_args = ["--no-proxy-server"]
    elif proxy:
        parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
        if parsed.username or parsed.password or parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
            raise RuntimeError("代理格式不支持或含凭据，不能安全传给 Chrome；请先人工准备浏览器")
        proxy_args = [f"--proxy-server={parsed.scheme}://{parsed.netloc}"]
    profile = STATE_DIR / "browser"
    profile.mkdir(parents=True, exist_ok=True)
    print(f"打开抖音专用浏览器，端口 {port}；登录/验证需要人工完成。", flush=True)
    browser_process = subprocess.Popen([
        browser, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *proxy_args, "about:blank",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), start_new_session=os.name != "nt")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if cdp_ready(port):
            return
        if browser_process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError("抖音浏览器未就绪；请检查原 profile 是否被别的 Chrome 占用，不自动换登录态")
class BrowserRPC:
    """复用已登录页面的原生请求链路，直接传保存的游标；不导航作品或回滚旧页。"""
    REQUEST_JS = r"""async ({path, query}) => {
        if (location.origin !== 'https://www.douyin.com') return {error:'wrong_origin'};
        const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length);
        if (/验证码|人机验证/.test(document.title) || visible('#captcha_container, .captcha_verify_container'))
            return {error:'manual_verification'};
        const version = navigator.userAgent.match(/Chrome\/([\d.]+)/)?.[1];
        const params = {device_platform:'webapp',aid:'6383',channel:'channel_pc_web',
            update_version_code:'170400',pc_client_type:'1',pc_libra_divert:'Windows',
            support_h265:'1',support_dash:'1',version_code:'170400',version_name:'17.4.0',
            cookie_enabled:String(navigator.cookieEnabled),screen_width:String(screen.width),
            screen_height:String(screen.height),browser_language:navigator.language,
            browser_platform:navigator.platform,browser_name:'Chrome',browser_version:version,
            browser_online:String(navigator.onLine),engine_name:'Blink',engine_version:version,
            os_name:'Windows',os_version:'10',cpu_core_num:String(navigator.hardwareConcurrency),
            device_memory:String(navigator.deviceMemory || 8),platform:'PC',downlink:'10',
            effective_type:'4g',round_trip_time:'150',...query};
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 25000);
        try {
            const response = await window.fetch(path + '?' + new URLSearchParams(params),
                {credentials:'include', signal:controller.signal});
            const type = response.headers.get('content-type') || '';
            const text = await response.text();
            let body; try {body = JSON.parse(text)} catch {}
            return {http:response.status, type, body};
        } catch (error) {return {error:error.name}}
        finally {clearTimeout(timer)}
    }"""

    def __init__(self, port=9222, interval=30):
        self.playwright = sync_playwright().start()
        self.page = None
        self.interval, self.last_action = max(interval, 10.0), 0.0
        self._actions_since_rest = 0
        self.test_request_limit = None
        self.test_requests_used = 0
        try:
            browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            self.page = next((p for p in context.pages if urlsplit(p.url).netloc == "www.douyin.com"), None)
            if self.page is None:
                self.page = context.new_page()
                # 仅冷启动初始化站点会话；不打开旧作品页、搜索首页或重放旧评论页。
                self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=25000)
                self.page.wait_for_load_state("load", timeout=25000)
            logger.info("增量RPC已连接：复用站点会话，直接使用已保存cursor/offset，不滚回首页；"
                        "单并发，随机间隔%s–%s秒，每30次请求休息5分钟", self.interval, self.interval + 15)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.playwright.stop()  # 留下已加载页面；重启复用，不关标签页或浏览器。

    def _pace(self):
        if self._actions_since_rest >= 30:
            logger.info("已执行30次业务请求，休息5分钟；可Ctrl+C停止，保留原断点")
            time.sleep(300)
            self._actions_since_rest = 0
        delay = random.uniform(self.interval, self.interval + 15)
        time.sleep(max(0, self.last_action + delay - time.monotonic()))
        self.last_action = time.monotonic()
        self._actions_since_rest += 1

    def _request(self, path, query):
        if path not in ("/aweme/v1/web/search/item/", "/aweme/v1/web/aweme/detail/", "/aweme/v1/web/comment/list/"):
            raise ValueError("接口不在只读采集范围")
        limit = getattr(self, "test_request_limit", None)
        if limit is not None and self.test_requests_used >= limit:
            raise TestBudgetExhausted()
        self._pace()
        if limit is not None:
            self.test_requests_used += 1  # 即便发送失败也计入预算；不在测试里自动重试。
        try:
            result = self.page.evaluate(self.REQUEST_JS, {"path": path, "query": query})
        except PlaywrightError as exc:
            if "Execution context was destroyed" in str(exc):
                # 请求可能已经发出；跳转后结果未知，不能在新页面盲重发或推进游标。
                raise RuntimeError("抖音页面在请求中跳转/刷新，结果未知；已停止且不重发，原断点保留。"
                                   "请确认专用浏览器页面稳定后重新运行 .bat 续采") from None
            raise
        if not isinstance(result, dict) or result.get("http") != 200 or "application/json" not in result.get("type", ""):
            raise RuntimeError("浏览器原生请求失败/超时或需人工验证；停止，不刷新、不重发")
        body = result.get("body")
        if not isinstance(body, dict) or type(body.get("status_code")) is not int or body["status_code"] != 0:
            code = body.get("status_code") if isinstance(body, dict) else None
            raise RuntimeError(f"浏览器接口 status_code={code}；请检查登录/验证，不自动重试")
        reason = (body.get("search_nil_info") or {}).get("search_nil_type")
        if reason in ("verify_check", "web_need_login"):
            raise RuntimeError("NEED_LOGIN/人机验证，请人工处理后从原断点续采")
        return body

    def _search_page(self, keyword, offset):
        return self._request("/aweme/v1/web/search/item/", {
            "keyword": keyword, "offset": str(offset), "count": "20" if offset == 0 else "10",
            "search_channel": "aweme_video", "search_source": "tab_search", "query_correct_type": "1",
            "is_filter_search": "0", "sort_type": "0", "publish_time": "0", "filter_duration": "0",
            "source": "normal_search", "search_id": ""})

    def _detail(self, video_id):
        return self._request("/aweme/v1/web/aweme/detail/", {"aweme_id": video_id})

    def _comments(self, video_id, cursor):
        return self._request("/aweme/v1/web/comment/list/", {"aweme_id": video_id, "cursor": str(cursor),
            "count": "10", "pc_img_format": "webp", "item_type": "0", "insert_ids": "",
            "whale_cut_token": "", "cut_version": "1", "rcFT": ""})

    def fetch(self, project, args, _cookie_file):
        if project == "aweme_search":
            return self._search_page(args[args.index("--keyword") + 1], int(args[args.index("--offset") + 1]))
        if project == "aweme_detail":
            return self._detail(args[args.index("--aweme-id") + 1])
        if project == "comment_list":
            return self._comments(args[args.index("--aweme-id") + 1], int(args[args.index("--cursor") + 1]))
        raise ValueError(f"未知接口：{project}")
def _cell(value):
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _row(video, keyword):
    desc = video.get("desc") or ""
    tags = [t.get("hashtag_name") for t in video.get("text_extra") or []
            if isinstance(t, dict) and t.get("hashtag_name")]
    tags += [t.get("cha_name") for t in video.get("cha_list") or []
             if isinstance(t, dict) and t.get("cha_name")]
    if not tags:
        tags = re.findall(r"#([^\s#，。！？,;；]+)", desc)
    stats = video.get("statistics") or {}
    duration = (video.get("video") or {}).get("duration")
    published = datetime.fromtimestamp(int(video["create_time"]), CST).strftime("%Y-%m-%d %H:%M:%S")
    row = dict(zip(LEGACY_COLUMNS, (keyword, video["aweme_id"],
        f'https://www.douyin.com/video/{video["aweme_id"]}', desc,
        "、".join(dict.fromkeys(tags)), (video.get("author") or {}).get("nickname"), published,
        stats.get("digg_count"), stats.get("comment_count"), stats.get("collect_count"),
        stats.get("share_count"), duration / 1000 if duration is not None else None,
        video.get("ip_label") or video.get("ip_attribution"))))
    row["采集时间"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    row = canonical_post("抖音", row)
    return [_cell(row.get(k)) for k in COLUMNS]


class CommentUnavailable(RuntimeError):
    """单篇成功响应但评论/分页数据缺失；保留缺口，不能当作零评论完成。"""


def collect_comments(fetch, cookie_file, store, video_id, max_pages=None):
    state = store.state(video_id)
    pages = 0
    try:
        while not state["done"] and (max_pages is None or pages < max_pages):
            body = fetch("comment_list", ["--aweme-id", video_id, "--cursor", state["cursor"] or "0"], cookie_file)
            if not isinstance(body, dict) or type(body.get("status_code")) is not int or body["status_code"] != 0:
                raise RuntimeError(f"抖音评论接口未成功（内容ID {video_id}），保留原游标，停止补采")
            comments = body.get("comments")
            more = body.get("has_more")
            # 实测正常零评论响应为null；仅明确total=0且已到末页时才视为合法空列表。
            if ("comments" in body and comments is None and type(more) in (int, bool) and more == 0
                    and exact_count(body.get("total")) == 0):
                comments = []
            # 实测权限/已删除作品：status_code=0、comments=null、has_more=0，total/cursor缺失。
            # 不补请求旧详情、不猜删除原因；隔离这一篇，但连续出现仍熔断，防止全站降级时继续请求。
            if ("comments" in body and comments is None and type(more) in (int, bool) and more == 0
                    and body.get("total") is None and body.get("cursor") is None):
                raise CommentUnavailable(f"抖音评论暂不可用（内容ID {video_id}，评论及分页信息缺失），保留原游标待补")
            # 个别作品会声称有评论却不返回列表；不能标记空页完成，也不能让单篇卡住整轮。
            if ("comments" in body and comments is None and type(more) in (int, bool) and more == 0
                    and (exact_count(body.get("total")) or 0) > 0):
                raise CommentUnavailable(f"抖音评论暂不可用（内容ID {video_id}，total>0但评论列表为空），保留原游标待补")
            if not isinstance(comments, list) or type(more) not in (int, bool) or more not in (0, 1):
                raise RuntimeError(f"抖音评论响应异常（内容ID {video_id}，comments={type(body.get('comments')).__name__}，"
                                   f"has_more={more if type(more) in (int, bool) else type(more).__name__}，"
                                   f"total={exact_count(body.get('total'))}），保留原游标，停止补采")
            cursor = body.get("cursor")
            if more and (not isinstance(cursor, int) or cursor <= int(state["cursor"] or 0)):
                raise RuntimeError("抖音评论 cursor 未推进")
            rows = [{"评论ID": c.get("cid"), "评论内容": c.get("text"), "评论点赞数": c.get("digg_count"),
                     "评论时间": timestamp(c.get("create_time")), "评论者昵称": (c.get("user") or {}).get("nickname"),
                     "评论者ID": (c.get("user") or {}).get("uid"), "IP属地": c.get("ip_label")}
                    for c in comments]
            store.save_page(video_id, rows, str(cursor or ""), not bool(more), body.get("total"))
            state = store.state(video_id)
            pages += 1
        if not state["done"]:
            store.mark(video_id, "页数上限，待续采")
    except BaseException:
        store.mark(video_id, "采集失败/中断，待续采")
        raise


def _archive_legacy(sources, seen):
    """确认已入累计 CSV 后改名备份；不直接删除用户的旧文件。"""
    for old in sources:
        with old.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not dataset_header(reader.fieldnames, COLUMNS) or any(
                row["视频ID"] and row["视频ID"] not in seen for row in reader
            ):
                raise ValueError(f"旧 CSV 尚未完整合并：{old}")
        backup = sidecar_path(old.with_name(old.name + ".bak"), "backups")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise ValueError(f"备份文件已存在，不覆盖：{backup}")
        old.rename(backup)


def _validate_csv(path):
    """拒绝残缺/串行的记录，防止在损坏的 CSV 上继续推进断点。"""
    return {row["内容ID"] for row in read_posts(path, COLUMNS, "抖音")}


def _prepare_csv(path):
    """首次运行合并旧版 CSV；往后只维护一个累计文件。"""
    seen = set()
    sources = sorted(p for p in OUTPUT.glob("douyin_*.csv") if not p.name.endswith("_高赞评论.csv"))
    for candidate in [path, *sources]:
        upgrade_csv(candidate, LEGACY_COLUMNS, COLUMNS, lambda r: canonical_post("抖音", r))
    if path.exists():
        seen = _validate_csv(path)
        _archive_legacy(sources, seen)
        return seen
    fd, tmp = tempfile.mkstemp(prefix="douyin_import_", suffix=".tmp", dir=OUTPUT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as target:
            writer = csv.writer(target, quoting=csv.QUOTE_ALL)
            writer.writerow(COLUMNS)
            for old in sources:
                with old.open(encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source)
                    if not dataset_header(reader.fieldnames, COLUMNS):
                        raise ValueError(f"旧 CSV 字段与当前版本不兼容：{old}")
                    for row in reader:
                        video_id = row["视频ID"]
                        if video_id and video_id not in seen:
                            writer.writerow([row.get(column, "") for column in COLUMNS])
                            seen.add(video_id)
        os.replace(tmp, path)
        _archive_legacy(sources, seen)
        return seen
    finally:
        Path(tmp).unlink(missing_ok=True)


def _save_progress(path, progress):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="dy_progress_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(progress, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def collect(words, cookie_file, start, end, max_pages=None, refresh=False, fetch=None, source="protocol",
            comment_pages=None, attempted=None):
    """先保全贴文和搜索断点；无论搜索成功或失败，评论可独立补采。"""
    if fetch is None:
        raise ValueError("单文件入口需要浏览器fetch回调")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    _prepare_csv(OUTPUT / "douyin.csv")
    attempted = set() if attempted is None else attempted
    # 先补已存贴文，防止某个失败搜索词长期阻塞既有评论任务。
    _sync_comments(fetch, cookie_file, start, end, comment_pages, attempted=attempted)
    result = (OUTPUT / "douyin.csv", len(_validate_csv(OUTPUT / "douyin.csv")))
    checkpoint = sidecar_path(OUTPUT / "douyin_progress.json", "state")
    progress = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    words = sorted(dict.fromkeys(words), key=lambda w: not (
        progress.get(w, {}).get("offset", 0) and not progress.get(w, {}).get("done")))
    for word in words:
        try:
            result = _collect_posts([word], cookie_file, start, end, max_pages, refresh, fetch, source)
        except BaseException:
            # 搜索受限时不再发送评论请求，只导出当前状态。
            _sync_comments(fetch, cookie_file, start, end, comment_pages, request=False)
            raise
        _sync_comments(fetch, cookie_file, start, end, comment_pages, attempted=attempted)
    return result


def _sync_comments(fetch, cookie_file, start, end, comment_pages, request=True, attempted=None):
    target = OUTPUT / "douyin.csv"
    if not target.exists():
        return
    store = CommentStore(target, "抖音", start, end)
    try:
        with target.open(encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                if not str(start) <= row["发布时间"][:10] <= str(end):
                    continue
                store.register(row["内容ID"])
        if request:
            # 先恢复已入库但尚未导出的旧评论，再发新请求；贴文去重不代表评论完成。
            store.export()
            store.report()
            attempted = attempted if attempted is not None else set()
            pending = [r[0] for r in store.db.execute("SELECT id FROM posts WHERE done=0 ORDER BY rowid")
                       if r[0] not in attempted]
            attempted.update(pending)
            unavailable = 0
            for ident in comment_jobs(store, pending, comment_pages):
                known_unavailable = store.state(ident)["status"].startswith("评论暂不可用")
                try:
                    collect_comments(fetch, cookie_file, store, ident, max_pages=1)
                except CommentUnavailable:
                    store.mark(ident, "评论暂不可用，保留原游标待补")
                    logger.warning("评论 %s 暂不可用：评论列表/分页数据缺失，本轮跳过该篇，未标完成；继续其它内容", ident)
                    if not known_unavailable:
                        unavailable += 1
                        if unavailable >= 3:
                            raise RuntimeError("抖音评论响应异常：连续3篇新遇到评论数据缺失，停止本轮；"
                                               "保留全部原断点，需冷却检查是否为整体访问异常") from None
                else:
                    unavailable = 0
    finally:
        try:
            export_csv(target, COLUMNS, read_posts(target, COLUMNS, "抖音"))
            store.export()
            store.report()
        finally:
            store.close()


def _collect_posts(words, cookie_file, start, end, max_pages=None, refresh=False, fetch=None, source="protocol"):
    if fetch is None:
        raise ValueError("单文件入口需要浏览器fetch回调")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / "douyin.csv"
    checkpoint = sidecar_path(OUTPUT / "douyin_progress.json", "state")
    # ponytail: 以 ID 集合去重，内存随视频数增长；超百万条时改用 SQLite 唯一索引。
    seen = _prepare_csv(target)
    progress = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    if refresh:
        for word in words:
            progress.pop(word, None)
        _save_progress(checkpoint, progress)
    records = {r["内容ID"]: r for r in read_posts(target, COLUMNS, "抖音")}
    pending_logs = {}

    def save_posts():
        export_csv(target, COLUMNS, records.values())
        for row in pending_logs.values():
            log_saved_row("抖音", row)
        pending_logs.clear()

    try:
        for keyword in dict.fromkeys(words):
            state = progress.get(keyword, {})
            if state and state.get("source", "protocol") not in ("protocol", "browser_rpc"):
                raise RuntimeError("搜索断点属于未知通道，拒绝重置或从首页重扫")
            if state.get("done") and not state.get("verified"):
                raise RuntimeError(f"{keyword} 的旧完成标记未经校验；保留断点，不自动重扫")
            if state.get("done"):
                print(f"{keyword} 已采完，跳过", flush=True)
                continue
            page = state.get("page", state.get("offset", 0) // 20)
            offset, observed = state.get("offset", 0), set()
            if page:
                print(f"{keyword} 从第 {page + 1} 页续采", flush=True)
            while max_pages is None or page < max_pages:
                print(f"搜索「{keyword}」第 {page + 1} 页…", flush=True)
                count = 20 if offset == 0 else 10
                for attempt in range(3):
                    body = fetch("aweme_search", ["--keyword", keyword, "--offset", str(offset),
                                               "--count", str(count)], cookie_file)
                    nil_info = body.get("search_nil_info") or {}
                    if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check":
                        raise RuntimeError(f"{keyword} 第 {page + 1} 页触发平台人机验证 (verify_check)，"
                                           "不能靠重试继续采集；请在抖音页面人工完成验证，更新登录态后续采")
                    if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "web_need_login":
                        raise RuntimeError(f"{keyword} 第 {page + 1} 页需要重新登录抖音")
                    items = body.get("aweme_list") or body.get("data") or []
                    suspicious = (not isinstance(items, list) or "has_more" not in body or not items
                                  or (not body["has_more"] and len(items) >= count))
                    if not suspicious:
                        break
                    if attempt == 2:
                        raise RuntimeError(f"{keyword} 第 {page + 1} 页连续返回可疑空页/满页末页；"
                                           "无法确认采集完整，断点未推进。稍后再运行重试")
                    delay = 10 * (attempt + 1)
                    print(f"{keyword} 第 {page + 1} 页响应可疑，{delay} 秒后重试", flush=True)
                    time.sleep(delay)
                before = len(seen)
                ids = set()
                for index, item in enumerate(items, 1):
                    if index % 5 == 0:
                        print(f"{keyword} 第 {page + 1} 页：处理 {index}/{len(items)}", flush=True)
                    video = item.get("aweme_info") or item.get("aweme_detail") or item
                    video_id = str(video.get("aweme_id") or "")
                    if not video_id:
                        continue
                    ids.add(video_id)
                    if video.get("images"):
                        continue
                    old = records.get(video_id, {})
                    if old:
                        continue  # 普通增量不重复请求旧详情；缺字段仅由 --repair-fields 显式补采。
                    timestamp = video.get("create_time")
                    try:
                        day = datetime.fromtimestamp(int(timestamp), CST).date()
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue  # 日期未知，不混入指定区间
                    if not start <= day <= end:
                        continue
                    stats = video.get("statistics") or {}
                    if (not video.get("ip_label") and not video.get("ip_attribution")) or (
                        not (video.get("author") or {}).get("nickname")) or (
                        (video.get("video") or {}).get("duration") is None) or any(
                        stats.get(k) is None for k in ("digg_count", "comment_count", "collect_count", "share_count")):
                        detail = fetch("aweme_detail", ["--aweme-id", video_id], cookie_file).get("aweme_detail") or {}
                        if detail and str(detail.get("aweme_id")) != video_id:
                            raise RuntimeError("抖音详情ID不匹配，停止补写")
                        for key in ("ip_label", "ip_attribution", "text_extra", "cha_list", "desc", "author"):
                            video[key] = video.get(key) or detail.get(key)
                        if (video.get("video") or {}).get("duration") is None:
                            video["video"] = detail.get("video") or video.get("video")
                        video["statistics"] = {**(detail.get("statistics") or {}), **{
                            k: v for k, v in stats.items() if v is not None}}
                    records[video_id] = merge_post(old, dict(zip(COLUMNS, _row(video, keyword))))
                    pending_logs[video_id] = records[video_id]
                    seen.add(video_id)
                if body["has_more"] and ids <= observed:
                    raise RuntimeError(f"{keyword} 第 {page + 1} 页重复，无法继续翻页")
                try:
                    next_offset = int(body["cursor"])
                except (KeyError, ValueError, TypeError):
                    raise RuntimeError(f"{keyword} 第 {page + 1} 页缺少有效 cursor，断点未推进") from None
                if next_offset <= offset:
                    raise RuntimeError(f"{keyword} 第 {page + 1} 页 cursor 未推进，断点未推进")
                observed.update(ids)
                # 先落 CSV，再原子推进检查点。崩溃时重跑同页，按 ID 跳过已写行。
                save_posts()
                # ponytail: 每页全文件校验 O(n)，超大数据量再换 SQLite 唯一索引。
                if _validate_csv(target) != seen:
                    raise RuntimeError("CSV 在采集中被外部改动；断点未推进，请先检查文件")
                progress[keyword] = {"offset": next_offset, "page": page + 1,
                                     "done": not bool(body["has_more"]), "verified": True,
                                     **({"source": source} if source != "protocol" else {})}
                _save_progress(checkpoint, progress)
                print(f"{keyword} 第 {page + 1} 页返回 {len(items)} 条、新增 {len(seen) - before} 条，"
                      f"累计 {len(seen)} 条", flush=True)
                if not body["has_more"]:
                    break
                page += 1
                offset = next_offset
    finally:
        # 中断也导出已取得的真实字段；不推进失败页面的游标。
        save_posts()
    return target, len(seen)


def _collect_browser_batch(words, fetch, start, end):
    blocked = []
    consecutive_errors = 0
    result = (OUTPUT / "douyin.csv", 0)
    attempted = set()  # 整轮共用；不能每换一个关键词就重试失败过的旧评论。
    for word in words:
        try:
            result = collect([word], None, start, end, fetch=fetch, source="browser_rpc", attempted=attempted)
            consecutive_errors = 0
        except RuntimeError as exc:
            message = str(exc)
            if not any(reason in message for reason in (
                "连续返回可疑空页/满页末页", "浏览器未加载「", "页重复，",
                "页缺少有效 cursor", "页 cursor 未推进",
            )):
                raise  # 验证、登录、CSV 损坏等不能继续请求
            checkpoint = sidecar_path(OUTPUT / "douyin_progress.json", "state")
            progress = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
            state = progress.get(word, {"offset": 0, "page": 0, "done": False,
                                        "verified": False, "source": "browser_rpc"})
            # 旧protocol断点同样可续采；失败只记待补，不把既有offset/page归零。
            state.update(done=False, last_error=message)
            progress[word] = state
            _save_progress(checkpoint, progress)
            blocked.append(word)
            consecutive_errors += 1
            print(f"{word} 暂停于当前页：{message}；继续下一词", file=sys.stderr, flush=True)
            if consecutive_errors >= 3:
                raise RuntimeError("连续 3 个关键词未能加载搜索页，暂停全量任务等待人工检查") from exc
    return (OUTPUT / "douyin.csv", len(_validate_csv(OUTPUT / "douyin.csv"))), blocked

def repair_fields(fetch, limit):
    target = OUTPUT / "douyin.csv"
    if not target.exists():
        raise ValueError("尚无贴文CSV，请先正常采集")
    _prepare_csv(target)
    records = read_posts(target, COLUMNS, "抖音")
    repaired = 0
    try:
        for index, row in enumerate(records):
            # 已有采集时间不代表其它核心字段完整；可选IP/话题为空不触发反复补采。
            if all(present(row.get(k)) for k in ("采集时间", "作者", "点赞数", "评论数", "收藏数", "分享数", "视频时长(秒)")):
                continue
            body = fetch("aweme_detail", ["--aweme-id", row["内容ID"]], None)
            detail = body.get("aweme_detail") or {}
            if str(detail.get("aweme_id")) != row["内容ID"] or body.get("status_code", 0) != 0:
                raise RuntimeError("详情ID/状态异常，字段补采停止")
            fresh = dict(zip(COLUMNS, _row(detail, row.get("搜索词", ""))))
            if not str(START) <= fresh["发布时间"][:10] <= str(END):
                raise RuntimeError("详情日期与数据集范围不一致")
            records[index] = merge_post(row, fresh)
            export_csv(target, COLUMNS, records)
            log_saved_row("抖音", records[index])
            repaired += 1
            if repaired >= limit:
                break
    finally:
        export_csv(target, COLUMNS, records)
    logger.info("旧贴文字段补采 %s 条；未取得原始采集时间的记录仍保留待补", repaired)
    return repaired


def check_environment():
    print(f"抖音：依赖导入成功；CSV目录 {OUTPUT}；登录有效性待运行时确认。")


def main():
    parser = argparse.ArgumentParser(description="抖音全量采集，默认浏览器9222，CSV写根目录output/csv")
    parser.add_argument("--keyword", action="append", help="只采指定词；默认使用文件内DEFAULT_KEYWORDS")
    parser.add_argument("--pages", type=int, help="可选的单词页数上限")
    parser.add_argument("--comment-pages", type=int)
    parser.add_argument("--no-year-queries", action="store_true")
    parser.add_argument("--shard", choices=("full", "forward", "reverse"), default="full")
    parser.add_argument("--interval", type=float, default=30.0, help="最小请求间隔秒，默认30，至少10；随机增加0–15秒，每30次休息5分钟")
    parser.add_argument("--test-requests", type=int, help="有界采集测试请求上限；到限时保留断点退出2")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--repair-fields", type=int, metavar="N", help="最多补采N条旧贴文详情；不搜新贴文、不推进评论断点")
    parser.add_argument("--browser-rpc", type=int, nargs="?", const=9222, default=9222, metavar="PORT")
    parser.add_argument("--check", action="store_true", help="只检查本地依赖，不打开浏览器/不采集")
    args = parser.parse_args()
    if args.pages is not None and args.pages < 1 or args.comment_pages is not None and args.comment_pages < 1:
        parser.error("页数必须大于0")
    if not 10 <= args.interval <= 3600 or not 1 <= args.browser_rpc <= 65535:
        parser.error("间隔须为10–3600秒，端口须为1–65535")
    if args.repair_fields is not None and args.repair_fields < 1:
        parser.error("repair-fields 必须大于0")
    if args.test_requests is not None and args.test_requests < 1:
        parser.error("test-requests 必须大于0")
    if args.shard != "full" and (args.keyword or args.no_year_queries or args.refresh or args.repair_fields is not None
                                  or args.pages is not None or args.comment_pages is not None or args.test_requests is not None):
        parser.error("分片只支持原134词、完整日期和无限制分页；不能重置断点或截断请求")
    if args.check:
        check_environment()
        return 0
    global INTERVAL
    INTERVAL = args.interval
    words = shard_queries(search_queries(args.keyword or DEFAULT_KEYWORDS, yearly=not args.no_year_queries), args.shard)
    prepare_douyin_browser(args.browser_rpc)
    browser = BrowserRPC(args.browser_rpc, args.interval)
    browser.test_request_limit = args.test_requests
    try:
        if args.repair_fields is not None:
            repair_fields(browser.fetch, args.repair_fields)
            return 0
        if not args.keyword and args.pages is None and not args.refresh and args.comment_pages is None:
            (path, total), blocked = _collect_browser_batch(words, browser.fetch, START, END)
        else:
            path, total = collect(words, None, START, END, args.pages, args.refresh, browser.fetch,
                                  "browser_rpc", args.comment_pages)
            blocked = []
    finally:
        browser.close()
    pending = json.loads(sidecar_path(path.with_name(path.stem + "_覆盖报告.json"), "reports").read_text(encoding="utf-8"))["未完成评论篇数"]
    print(f"抖音扫描结束：{total}条，待补关键词{len(blocked)}，待补评论{pending}；{path}")
    return 2 if blocked or pending else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
