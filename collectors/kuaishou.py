"""快手单文件浏览器RPC采集器；复用本机9224登录态和页面原生客户端，CSV写output/csv。"""
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
STATE_DIR = ROOT / "runtime/kuaishou"
DEFAULT_KEYWORDS = ('双减', '双减政策', '义务教育双减', '减轻学生作业负担', '减轻校外培训负担', '作业管理', '五项管理', '校外培训治理', '课后服务', '校内课后服务', '课后延时服务', '延时托管', '三点半课堂', '三点半难题', '430课后服务', '430课堂', '课后看护', '校内托管', '午托', '晚托', '寒暑假校内托管', '课后服务收费', '普惠性课后服务', '课后托管', '托管班', '校外托管', '学生托管', '小饭桌', '作业托管', '假期托管', '寒暑假托管', '暑托班', '晚辅', '作业辅导班', '课外培训', '校外培训', '学科培训', '学科类培训', '非学科类培训', '补习班', '辅导班', '培训班', '培训机构', '教培', '教培行业', '培训学校', 'K12培训', '一对一辅导', '一对一家教', '小班课', '线上培训', '网课', '在线教育', '隐形变异培训', '无证办学', '办学资质', '无资质培训', '预收费', '收费监管', '资金监管', '培训广告', '广告治理', '黑白名单', '教培机构注销', '机构跑路', '退费难', '营转非', '非营利机构', '机构转型', '行政处罚', '吊销办学许可', '家长', '学生', '教师', '老师', '班主任', '校长', '教培从业者', '教培老师', '机构老板', '家长委员会', '焦虑', '教育焦虑', '鸡娃', '内卷', '减负', '减负不减负', '躺平', '佛系', '分流', '学区房', '教育公平', '负担', '压力', '睡眠不足', '近视', '心理健康', '抑郁', '厌学', '牛娃', '牛蛙', '普娃', '渣娃', '海淀妈妈', '顺义妈妈', '鸡血家长', '佛系养娃', '掐尖', '点招', '密考', '暗考', '抢跑', '起跑线', '剧场效应', 'AI辅导', 'AI家教', '人工智能辅导', '智能学习', '学习机', '作业帮', '猿辅导转型', '非学科类', '艺术培训', '体育培训', '编程教育', '科学素养', '研学旅行', '营地教育', '素质教育', '教师弹性上下班', '教师课后服务津贴', '教师负担', '双减 课后服务', '校外培训 退费难')
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("kuaishou")

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


_collector_lease = None


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
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"采集未完成：{exc}", file=sys.stderr)
        return 1


import hashlib
import math
import re
import stat
import subprocess
import time
import traceback
from contextlib import closing
from urllib.parse import quote, urlparse
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler, getproxies, proxy_bypass
from playwright.sync_api import Error as PlaywrightError, sync_playwright

DEFAULT_REQUIREMENTS = None
BASE = "https://www.kuaishou.com"
SEARCH = "/rest/v/search/feed"
COMMENTS = "/rest/v/photo/comment/list"


def check_environment():
    print(f"快手：浏览器RPC依赖检查通过，默认9224；CSV目录 {OUTPUT}；未联网或验证登录。")


def cdp_ready(port):
    """与抖音相同的本机CDP校验；仅明确拒绝连接才允许启动浏览器。"""
    try:
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


def prepare_kuaishou_browser(port):
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
        raise RuntimeError("未找到 Chrome/Edge，请设置 CHROME_PATH 或先手动启动快手 CDP 浏览器")
    proxy_args = []
    proxies = getproxies()
    proxy = proxies.get("https") or proxies.get("http") or proxies.get("all")
    if proxy_bypass("www.kuaishou.com"):
        proxy_args = ["--no-proxy-server"]
    elif proxy:
        parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
        if parsed.username or parsed.password or parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
            raise RuntimeError("代理格式不支持或含凭据，不能安全传给 Chrome；请先人工准备浏览器")
        proxy_args = [f"--proxy-server={parsed.scheme}://{parsed.netloc}"]
    profile = STATE_DIR / "browser"
    profile.mkdir(parents=True, exist_ok=True)
    logger.info("打开快手专用浏览器，端口 %s；人工登录后复用本地档案，不导出Cookie。", port)
    process = subprocess.Popen([
        browser, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *proxy_args, "about:blank",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), start_new_session=os.name != "nt")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if cdp_ready(port):
            return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError("快手浏览器未就绪；请检查原 profile 是否被占用，不自动换登录态")


BRIDGE_JS = r"""({exclusive}) => {
    if (location.origin !== 'https://www.kuaishou.com') return {ready:false};
    const client = document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.rebornClient?.rest;
    if (client?.type !== 'REST' || typeof client.mutate !== 'function') return {ready:false};
    const old = window.__ksCollectorRPC;
    if (old && (!exclusive || old.version !== 1 || old.busy !== false || old.uncertain !== false ||
        typeof old.stopped !== 'boolean')) return {ready:false};
    const bridge = {
        version:1, busy:false, stopped:false, uncertain:false,
        async call(path, body) {
            if (this.busy || this.stopped) return {ok:false, reason:'bridge_stopped_or_busy'};
            if (!["/rest/v/search/feed", "/rest/v/photo/comment/list"].includes(path) ||
                !body || typeof body !== 'object' || Array.isArray(body)) return {ok:false, reason:'invalid_request'};
            const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length);
            if (location.origin !== 'https://www.kuaishou.com' ||
                document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.rebornClient?.rest !== client) {
                this.stopped = true; return {ok:false, reason:'page_changed'};
            }
            if (visible('.global-protect-popup, .ks-captcha, #captcha_container') || /验证码|人机验证/.test(document.title)) {
                this.stopped = true; return {ok:false, reason:'manual_verification'};
            }
            // Only booleans leave this check; credentials stay entirely in the browser.
            const names = new Set(document.cookie.split(';').filter(v => v.includes('=') && v.split('=').slice(1).join('=').trim())
                .map(v => v.trim().split('=')[0]));
            if (!names.has('userId') || !(names.has('kuaishou.server.webday7_st') || names.has('kwssectoken'))) {
                this.stopped = true; return {ok:false, reason:'need_login'};
            }
            this.busy = true;
            let timer;
            try {
                // mutate is the site's Promise dispatcher, NOT a business mutation here: whitelist above is read-only.
                const payload = await Promise.race([
                    client.mutate({url:path, method:'POST', variables:body,
                        headers:{'content-type':'application/json'}, timeout:25000}),
                    new Promise((_, reject) => {timer = setTimeout(() => reject(null), 35000)})
                ]);
                if (!payload || payload.result !== 1) this.stopped = true;
                const text = JSON.stringify(payload);
                if (typeof text !== 'string' || new TextEncoder().encode(text).length > 8*1024*1024) {
                    this.stopped = true; return {ok:false, reason:'invalid_payload'};
                }
                return {ok:true, payload};
            } catch (_) {
                // Native timeouts may leave a request in flight. A new process must not reclaim this bridge.
                this.stopped = this.uncertain = true;
                return {ok:false, reason:'native_request_failed'};
            } finally {clearTimeout(timer); this.busy = false}
        }
    };
    window.__ksCollectorRPC = bridge;
    return {ready:true, version:1};
}"""

CST = timezone(timedelta(hours=8))
LEGACY_FIELDS = ["视频标题或描述", "话题标签", "作者", "发布时间", "播放量", "点赞数",
                 "评论数", "分享数", "视频时长", "IP 属地"]
FIELDS = post_columns([*LEGACY_FIELDS, "视频ID", "视频链接", "作者ID"])  # 时长单位：秒


def load_keywords(path):
    """Read only the search section, deduplicate words and explicit A + B examples."""
    if path is None:
        return list(DEFAULT_KEYWORDS)
    text = Path(path).read_text(encoding="utf-8-sig")
    match = re.search(r"^## 一、搜索关键词\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise ValueError("需求文件中没有找到‘## 一、搜索关键词’")
    section = match.group(1)
    words = []
    for line in section.splitlines():
        cells = [s.strip() for s in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) != 2:
            continue
        if cells[0] == "类别" or re.fullmatch(r"[-: ]+", cells[0]):
            continue
        words.extend(w.strip().rstrip("。；;") for w in cells[1].split("、"))
    words.extend(" ".join(part.strip() for part in m.split("+"))
                 for m in re.findall(r"\*\*([^*\n]+\+[^*\n]+)\*\*", section))
    result = list(dict.fromkeys(w for w in words if w))
    if not result:
        raise ValueError("关键词表为空")
    return result


def number(value):
    # Do not turn rounded display strings such as '1.2万' into purported exact counts.
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n if math.isfinite(n) and n >= 0 else None


def count(value):
    return exact_count(value)


def published(value):
    n = number(value)
    if not n:
        return None
    try:
        # photo.timestamp in the saved feed is milliseconds; accept second-based imports too.
        return datetime.fromtimestamp(n / 1000 if n >= 100_000_000_000 else n, CST)
    except (ValueError, OverflowError, OSError):
        return None


def normalize(feed, keyword, start, end):
    photo, author = feed.get("photo"), feed.get("author") or {}
    if not isinstance(photo, dict) or not photo.get("id"):
        raise ValueError("feeds 条目缺少 photo.id；停止，避免把接口变化当作空结果")
    timestamp = published(photo.get("timestamp"))
    if timestamp is None or not start <= timestamp.date() <= end:
        return None
    caption = photo.get("caption")
    tags = [tag["name"] for tag in (feed.get("tags") or [])
            if isinstance(tag, dict) and tag.get("type") == 1 and tag.get("name")]
    if not tags and isinstance(caption, str):
        # ponytail: caption fallback only handles contiguous hashtags; structured tags take priority.
        tags = re.findall(r"#([^\s#，。！？、]+)", caption)
    duration = number(photo.get("duration"))
    row = {"视频ID": str(photo["id"]), "视频链接": BASE + "/short-video/" + quote(str(photo["id"]), safe=""),
           "搜索关键词": keyword, "视频标题或描述": caption, "话题标签": " | ".join(dict.fromkeys(tags)),
           "作者": author.get("name"), "作者ID": author.get("id"),
           "发布时间": timestamp.isoformat(sep=" "),
           "播放量": count(photo.get("viewCount")), "点赞数": count(photo.get("likeCount")),
           # comment.us_c is a permission flag, NOT a count. These fields remain missing until proved.
           "评论数": None, "收藏数": count(photo.get("collectCount")), "分享数": None, "IP 属地": None,
           "视频时长": round(duration / 1000, 3) if duration is not None else None,
           "日期状态": "范围内",
           "采集时间": datetime.now(CST).isoformat(timespec="seconds")}
    return canonical_post("快手", row)


def csv_row(row):
    return {key: csv_cell(row.get(key)) for key in FIELDS}


def search_body(keyword, cursor="", search_session=""):
    body = {"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": cursor}
    if search_session:
        body["searchSessionId"] = search_session
    return body


def check_page(data):
    if not isinstance(data, dict) or data.get("result") != 1:
        code = data.get("result") if isinstance(data, dict) else None
        raise RuntimeError(f"接口拒绝 result={code}；请检查登录态/签名，不自动重试")
    if not isinstance(data.get("feeds"), list):
        raise RuntimeError("响应缺少 feeds 数组，不能当作搜索无结果")
    cursor = data.get("pcursor")
    if cursor is not None and not isinstance(cursor, (str, int)):
        raise RuntimeError("pcursor 类型异常")
    return data["feeds"], str(cursor) if cursor is not None else ""


class BudgetExhausted(RuntimeError):
    pass


class SearchClient:
    """本机CDP -> 页面原生REST客户端；不复制签名/Cookie，不提供HTTP备用通道。"""
    def __init__(self, port=9224, request_limit=None, delay=60):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("RPC 调试端口必须在 1–65535 之间")
        if (request_limit is not None and (type(request_limit) is not int or request_limit < 1)
                or type(delay) not in (int, float) or not math.isfinite(delay) or delay < 10):
            raise ValueError("请求预算须为正整数，间隔须为至少10秒的有限数")
        self.limit, self.used, self.delay, self.last_request = request_limit, 0, delay, 0.0
        self.playwright = self.page = None
        self._stopped = self._owns_bridge = False
        try:
            if not cdp_ready(port):
                raise RuntimeError("快手RPC浏览器尚未启动；请通过总开关启动或先准备专用浏览器")
            self.playwright = sync_playwright().start()
            browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
            if len(browser.contexts) != 1:
                raise RuntimeError("快手RPC浏览器上下文不唯一；不猜测登录态")
            context = browser.contexts[0]
            pages = [p for p in context.pages if urlparse(p.url).scheme == "https"
                     and urlparse(p.url).netloc == "www.kuaishou.com"]
            if not pages:
                page = context.new_page()
                page.goto(BASE + "/", wait_until="domcontentloaded", timeout=25000)
                pages = [page]  # 仅冷启动初始化，不打开旧作品或重放搜索页。
            ready_js = "() => document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.rebornClient?.rest?.type === 'REST'"
            deadline = time.monotonic() + 25
            while True:
                ready = [p for p in pages if p.evaluate(ready_js)]
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("快手页面原生客户端未就绪；请人工检查登录或页面版本，不回退HTTP/fetch")
                time.sleep(0.5)
            if len(ready) != 1:
                raise RuntimeError("快手RPC可用标签页不唯一；请只保留一个快手业务页，不自动关闭页面")
            self.page = ready[0]
            exclusive = _collector_lease is not None and not _collector_lease.closed
            installed = self.page.evaluate(BRIDGE_JS, {"exclusive": exclusive})
            if not isinstance(installed, dict) or installed.get("ready") is not True or installed.get("version") != 1:
                raise RuntimeError("快手RPC旧连接忙碌、结果不确定、未持有采集锁或页面版本变化；"
                                   "请停止旧任务并人工检查页面，不接管、不刷新、不重发")
            self._owns_bridge = True
            logger.info("快手浏览器RPC已连接：端口%s，页面原生签名/登录态；间隔至少%s秒，每30次休息5分钟", port, delay)
        except PlaywrightError:
            self.close()
            raise RuntimeError("快手RPC连接或页面初始化失败；请检查原浏览器/代理，未启动采集请求") from None
        except BaseException:
            self.close()
            raise

    def close(self):
        self._stopped = True
        try:
            if self._owns_bridge and self.page is not None:
                try:
                    self.page.evaluate("""() => {const r=window.__ksCollectorRPC;
                        if (!r || r.version!==1) return;
                        r.stopped=true;
                        if (r.busy===false && r.uncertain===false) delete window.__ksCollectorRPC;
                    }""")
                except Exception:
                    pass  # 断连/刷新时不补发请求；不关闭用户标签页或浏览器。
        finally:
            self._owns_bridge = False
            if self.playwright is not None:
                self.playwright.stop()
                self.playwright = None

    def post(self, path, body):
        if path not in (SEARCH, COMMENTS) or not isinstance(body, dict):
            raise ValueError("接口不在只读采集范围")
        json.dumps(body, allow_nan=False)
        if self._stopped:
            raise RuntimeError("快手RPC本轮已停止，不自动重试")
        if self.limit is not None and self.used >= self.limit:
            raise BudgetExhausted("达到本次 --max-requests，已保存此前结果")
        if self.used and self.used % 30 == 0:
            logger.info("已执行30次业务请求，休息5分钟；可Ctrl+C停止，保留原断点")
            time.sleep(300)
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        wire = []
        def observe(response):
            try:
                parsed = urlparse(response.url)
                if (parsed.scheme == "https" and parsed.netloc == "www.kuaishou.com" and parsed.path == path
                        and response.request.method == "POST" and response.request.post_data_json == body):
                    # Use cached headers: header_value() yields to the driver inside this callback,
                    # so evaluate() could return before the observation is appended.
                    headers = response.headers
                    wire.append({"status": response.status, "type": headers.get("content-type", ""),
                                 "retry": headers.get("retry-after")})
            except Exception:
                wire.append({})  # 元数据不完整也拒绝把响应认作成功。
        self.page.on("response", observe)
        self.used += 1  # 失败/未知结果也占预算，不退回额度、不重发。
        self.last_request = time.monotonic()
        try:
            raw = self.page.evaluate("""async ([path, body]) => {
                const rpc=window.__ksCollectorRPC;
                return JSON.stringify(rpc?.version===1 ? await rpc.call(path,body) : {ok:false,reason:'bridge_missing'});
            }""", [path, body])
            reply = json.loads(raw)
            retry_after = wire[0].get("retry") if len(wire) == 1 else None
            if retry_after is None:
                retry_after = "未提供"
            elif not re.fullmatch(r"[0-9]{1,12}|[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} [0-9:]{8} GMT", retry_after):
                retry_after = "invalid"
            status = wire[0].get("status") if len(wire) == 1 else None
            if type(status) is int and status != 200:
                raise RuntimeError(f"接口 HTTP {status}；Retry-After={retry_after!r}；停止采集")
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                reason = reply.get("reason") if isinstance(reply, dict) else None
                if reason in ("need_login", "manual_verification"):
                    raise RuntimeError(f"快手需要人工登录/验证；Retry-After={retry_after!r}；请在原快手专用窗口完成后续采")
                raise RuntimeError(f"快手页面原生请求失败，结果可能不确定；Retry-After={retry_after!r}；"
                                   "不自动重试，请人工检查浏览器/代理")
            if len(wire) != 1 or status != 200 or "application/json" not in wire[0].get("type", "").lower():
                raise RuntimeError("快手RPC响应不是唯一HTTP200 JSON；可能登录/验证页或页面变化，不当作空结果")
            data = reply.get("payload")
            if not isinstance(data, dict) or type(data.get("result")) is not int or data["result"] != 1:
                code = data.get("result") if isinstance(data, dict) else None
                code = code if type(code) is int else "unknown"
                if code == 2056:
                    raise RuntimeError(f"快手需要重新登录；Retry-After={retry_after!r}；请在原专用浏览器完成人工登录后续采")
                if code == 7935:
                    raise RuntimeError(f"快手需要人工验证；Retry-After={retry_after!r}；请在原专用浏览器检查访问限制，不自动重试")
                message = data.get("error_msg") if isinstance(data, dict) else None
                message = " ".join(message.split())[:160] if isinstance(message, str) else "未提供"
                if re.search(r"https?://|(?:cookie|authorization|\w*token|sign(?:ature)?|__NS_\w+)[\"']?\s*[:=]", message, re.I):
                    message = "服务端说明含敏感字段，已省略"
                limited = bool(re.search(r"操作太快|请求过于频繁|访问过于频繁|请求频繁|访问频繁|操作频繁", message))
                label = "快手请求频率受限" if limited else "接口拒绝"
                ident = str(body.get("photoId", ""))
                context = f"，内容ID={ident}" if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident) else ""
                raise RuntimeError(f"{label} result={code}；Retry-After={retry_after!r}；服务端说明：{message}；"
                                   f"接口={path}{context}；停止本轮，由总开关判断是否冷却续采")
            return data
        except (PlaywrightError, ValueError, TypeError):
            self._stopped = True
            raise RuntimeError("快手RPC断连/页面跳转或返回异常，结果未知；不自动重试，原断点保留") from None
        except BaseException:
            self._stopped = True
            raise
        finally:
            self.page.remove_listener("response", observe)

    def search(self, keyword, cursor, session):
        return self.post(SEARCH, search_body(keyword, cursor, session))

    def comments(self, photo_id, cursor="", page=1):
        return self.post(COMMENTS, {"photoId": photo_id, "page": str(page), "pcursor": cursor, "type": "STATIC"})

    def comment_count(self, photo_id):
        return count(self.comments(photo_id).get("commentCountV2"))


def collect_comments(client, store, photo_id, max_pages=None):
    current, pages = store.state(photo_id), 0
    try:
        while not current["done"] and (max_pages is None or pages < max_pages):
            data = client.comments(photo_id, current["cursor"], current["pages"] + 1)
            rows, cursor = data.get("rootCommentsV2"), data.get("pcursorV2")
            if data.get("result") != 1 or not isinstance(rows, list) or "pcursorV2" not in data:
                raise RuntimeError("快手评论响应不完整，不能当作无评论")
            if not isinstance(cursor, (str, int)):
                raise RuntimeError("快手评论 pcursorV2 类型异常")
            # 成功响应的明确总数可独立保存；空页/游标异常仍不得推进评论页或标完成。
            total = exact_count(data.get("commentCountV2"))
            if total is not None:
                with store.db:
                    store.db.execute("UPDATE posts SET total=? WHERE id=?", (total, str(photo_id)))
            normalized = [{"评论ID": c.get("comment_id"), "评论内容": c.get("content"),
                           "评论点赞数": c.get("likeCount"), "评论时间": timestamp(c.get("timestamp")),
                           "评论者昵称": c.get("author_name"), "评论者ID": c.get("author_id"),
                           "IP属地": c.get("ip_location")} for c in rows]
            store.save_page(photo_id, normalized, str(cursor), str(cursor) in ("", "no_more"), data.get("commentCountV2"))
            current, pages = store.state(photo_id), pages + 1
        if not current["done"]:
            store.mark(photo_id, "页数上限，待续采")
    except BaseException:
        store.mark(photo_id, "采集失败/中断，待续采")
        raise


class SearchState:
    """SQLite is authoritative; CSV is an atomically replaced snapshot."""
    def __init__(self, path, output, start, end):
        self.output = Path(output)
        if path is not None:
            path = Path(path)
            if any(is_link_or_junction(p) for p in (path, *path.parents)):
                raise ValueError("增量状态路径不能经过符号链接或 junction")
            if path.exists() and path.stat().st_nlink > 1:
                raise ValueError("增量状态文件不能为硬链接")
        if self.output.exists() and (path is None or not path.is_file()):
            raise FileExistsError("CSV 已存在但没有对应视频ID状态库；不能可靠续采旧文件，请指定新的 --out")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:")
        self.dirty = True  # Recover a missing/partial CSV from committed rows after a crash.
        try:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS videos (
                    photo_id TEXT PRIMARY KEY, payload TEXT NOT NULL, enriched INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS progress (
                    keyword TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT '', page INTEGER NOT NULL DEFAULT 0,
                    cursors TEXT NOT NULL DEFAULT '[]', done INTEGER NOT NULL DEFAULT 0);
            ''')
            config = json.dumps({"version": 2, "output": os.path.normcase(str(self.output.absolute())),
                                 "fields": FIELDS, "start": start.isoformat(), "end": end.isoformat()},
                                ensure_ascii=False, sort_keys=True)
            saved = self.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if saved is not None and saved[0] != config:
                old = json.loads(saved[0])
                compatible = (((old.get("version") == 1 and old.get("fields") == LEGACY_FIELDS)
                               or (old.get("version") == 2 and old.get("fields") == FIELDS))
                              and old.get("start") == start.isoformat() and old.get("end") == end.isoformat()
                              and Path(old.get("output", "")).name == self.output.name)
                if not compatible:
                    raise ValueError("该 CSV 的日期范围与状态库不一致，请保持原参数或指定新的 --out")
                if path is not None:
                    backup_path = path.with_suffix(path.suffix + ".schema-v1.bak")
                    if not backup_path.exists():
                        backup = sqlite3.connect(backup_path)
                        try:
                            self.db.backup(backup)
                        finally:
                            backup.close()
                with self.db:
                    self.db.execute("UPDATE meta SET value=? WHERE key='config'", (config,))
            if saved is None:
                if self.output.exists():
                    raise FileExistsError("状态库未初始化，拒绝覆盖现有 CSV；请选择新的 --out")
                with self.db:
                    self.db.execute("INSERT INTO meta VALUES ('config', ?)", (config,))
        except BaseException:
            self.db.close()
            raise

    def begin(self, keywords, restart):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO progress(keyword) VALUES (?)", ((k,) for k in keywords))
            if restart:
                # 只有显式 --restart 才重扫；完成一轮后普通启动零搜索请求。
                self.db.executemany("UPDATE progress SET cursor='', session='', page=0, cursors='[]', done=0 WHERE keyword=?",
                                    ((k,) for k in keywords))

    def progress(self, keyword):
        cursor, session, page, cursors, done = self.db.execute(
            "SELECT cursor, session, page, cursors, done FROM progress WHERE keyword=?", (keyword,)).fetchone()
        return cursor, session, page, json.loads(cursors), bool(done)

    def checkpoint(self, keyword, cursor, session, page, cursors, done):
        with self.db:
            self.db.execute("UPDATE progress SET cursor=?, session=?, page=?, cursors=?, done=? WHERE keyword=?",
                            (cursor, session, page, json.dumps(cursors), done, keyword))

    def get(self, photo_id):
        saved = self.db.execute("SELECT payload, enriched FROM videos WHERE photo_id=?", (photo_id,)).fetchone()
        return (canonical_post("快手", {**json.loads(saved[0]), "视频ID": photo_id}), bool(saved[1])) if saved else None

    def put(self, photo_id, row, enriched):
        with self.db:
            self.db.execute("INSERT INTO videos VALUES (?, ?, ?) ON CONFLICT(photo_id) DO UPDATE SET payload=excluded.payload, enriched=excluded.enriched",
                            (photo_id, json.dumps(row, ensure_ascii=False), enriched))
        self.dirty = True

    def sync_csv(self):
        if not self.dirty and self.output.exists():
            return
        # ponytail: 每页重写全部结果；数据量成为瓶颈后改增量快照。
        export_csv(self.output, FIELDS, (canonical_post("快手", {**json.loads(payload), "视频ID": photo_id})
                   for photo_id, payload in self.db.execute("SELECT photo_id, payload FROM videos ORDER BY rowid")))
        self.dirty = False

    def close(self):
        self.db.close()


def export_search(client, keywords, output, start, end, pages=None, comment_counts=True,
                  state_path=None, restart=False, comment_pages=None, retry_stalled=True):
    """Persist IDs and page checkpoints; resume failures and add only new videos to the CSV."""
    state = SearchState(state_path, output, start, end)
    written = 0
    comments = None
    attempted = set()
    stalled = 0
    comments_paused = False

    def enrich(photo_id):
        nonlocal stalled, comments_paused
        comments.register(photo_id)
        current = comments.state(photo_id)
        if current["done"]:
            return
        known_stalled = current["status"].startswith("分页停滞")
        try:
            collect_comments(client, comments, photo_id, max_pages=1)
        except RuntimeError as exc:
            if not str(exc).startswith(("评论空页或游标循环", "评论页全部重复")):
                raise  # 登录、限流、签名、响应结构异常仍停止平台，不绕过。
            comments.mark(photo_id, "分页停滞，保留原游标待检查")
            if not known_stalled:
                stalled += 1  # 旧缺口不重复计入新故障熔断；仍每轮从原游标尝试一次。
            logger.warning("评论 %s 分页停滞：保留未完成状态，不重扫；继续其他内容", photo_id)
            if stalled >= 3:
                comments_paused = True
                logger.warning("连续3篇评论分页停滞：本轮暂停评论请求，继续搜索并保存贴文；"
                               "评论保留原游标和未完成状态，下次运行再补采")
        else:
            stalled = 0
        finally:
            row = state.get(photo_id)[0]
            summary = comments.state(photo_id)
            if summary["total"] is not None:
                row["评论数"] = summary["total"]
            state.put(photo_id, row, summary["done"])

    def enrich_all(photo_ids):
        if not comment_counts or comments_paused:
            return
        pending = [ident for ident in dict.fromkeys(photo_ids) if ident not in attempted
                   and (retry_stalled or not comments.state(ident)["status"].startswith("分页停滞"))]
        attempted.update(pending)
        for ident in comment_jobs(comments, pending, comment_pages):
            enrich(ident)
            if comments_paused:
                break

    try:
        comments = CommentStore(output, "快手", start, end)
        state.begin(keywords, restart)
        keywords = sorted(keywords, key=lambda k: not (state.progress(k)[2] > 0 and not state.progress(k)[4]))
        state.sync_csv()
        # 补采所有已保存视频的评论，旧版 enriched 只代表评论总数，不代表评论内容。
        saved_ids = [r[0] for r in state.db.execute("SELECT photo_id FROM videos")]
        for photo_id in saved_ids:
            comments.register(photo_id)
        comments.export()
        comments.report()
        enrich_all(saved_ids)  # 不信任旧enriched/已搜完标记；以独立评论库为准。
        state.sync_csv()
        for keyword in keywords:
            cursor, session, page, cursors, done = state.progress(keyword)
            if done:
                continue
            pages_this_run = 0
            while pages is None or pages_this_run < pages:
                data = client.search(keyword, cursor, session)
                feeds, next_cursor = check_page(data)
                pending = []
                for feed in feeds:
                    row = normalize(feed, keyword, start, end)
                    if row is None:
                        continue
                    photo_id = row["视频ID"]
                    saved = state.get(photo_id)
                    if saved is None:
                        state.put(photo_id, row, False)
                        written += 1
                    else:
                        # 旧表只保存过10列；重遇同ID必须补收藏数/作者ID/关键词等，不只去重跳过。
                        row = merge_post(saved[0], row)
                        state.put(photo_id, row, saved[1])
                    log_saved_row("快手", row)  # SQLite事务已提交；CSV随后导出，不输出整表旧数据。
                    comments.register(photo_id)
                    pending.append(photo_id)
                # 已保存整页贴文后，搜索和评论断点独立提交；评论失败不导致重搜旧页。
                if next_cursor and next_cursor != "no_more" and (next_cursor == cursor or next_cursor in cursors):
                    raise RuntimeError("搜索游标重复，已保存断点；必要时使用 --restart 重扫，仍保留视频ID去重")
                session = data.get("searchSessionId") or session
                if not isinstance(session, str):
                    raise RuntimeError("searchSessionId 类型异常；已保存数据，不推进游标")
                page += 1
                pages_this_run += 1
                done = not next_cursor or next_cursor == "no_more"
                if not done:
                    cursors.append(next_cursor)
                cursor = next_cursor if not done else ""
                state.checkpoint(keyword, cursor, session, page, cursors, done)
                state.sync_csv()
                logger.info("关键词=%s 页=%s 当前页=%s 本次新增=%s", keyword, page, len(feeds), written)
                enrich_all(pending)
                if done:
                    break
                # Relevance ranking: old IDs/old dates never imply the remaining pages contain nothing new.
        return written
    finally:
        try:
            state.sync_csv()
        finally:
            state.close()
            if comments is not None:
                try:
                    comments.export()
                    comments.report()
                finally:
                    comments.close()


def repair_fields(client, keywords, output, state_path, start, end, limit):
    """搜索接口已确认有collectCount/author.id；只回填已存ID，不改变正常搜索断点。"""
    state = SearchState(state_path, output, start, end)
    matched, requests = set(), 0
    try:
        pending = {ident for (ident,) in state.db.execute("SELECT photo_id FROM videos")
                   if any(not present(state.get(ident)[0].get(k)) for k in ("收藏数", "作者ID", "搜索词", "采集时间"))}
        for keyword in keywords:
            cursor, session, cursors = "", "", set()
            while requests < limit and pending:
                data = client.search(keyword, cursor, session)
                requests += 1
                feeds, next_cursor = check_page(data)
                for feed in feeds:
                    row = normalize(feed, keyword, start, end)
                    saved = state.get(row["内容ID"]) if row and row["内容ID"] in pending else None
                    if saved:
                        merged = merge_post(saved[0], row)
                        state.put(row["内容ID"], merged, saved[1])
                        log_saved_row("快手", merged)
                        matched.add(row["内容ID"])
                        if all(present(merged.get(k)) for k in ("收藏数", "作者ID", "搜索词", "采集时间")):
                            pending.discard(row["内容ID"])
                state.sync_csv()
                if next_cursor in ("", "no_more"):
                    break
                if next_cursor == cursor or next_cursor in cursors:
                    raise RuntimeError("字段补采搜索游标重复，停止")
                cursors.add(next_cursor)
                cursor, session = next_cursor, data.get("searchSessionId") or session
                if not isinstance(session, str):
                    raise RuntimeError("字段补采 searchSessionId 类型异常，停止")
            if requests >= limit or not pending:
                break
    finally:
        try:
            state.sync_csv()
        finally:
            state.close()
    logger.info("字段补采搜索%s页，匹配并更新%s条既有贴文；未重新搜到的ID仍待补", requests, len(matched))
    return len(matched)


def is_link_or_junction(path):
    """Reject links/reparse points without requiring Python 3.12 Path.is_junction."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="快手关键词检索 -> CSV（补写已有ID的字段，省略全空列，不下载视频）")
    ap.add_argument("--requirements", type=Path, default=None, help="可选Markdown词表；默认内置检索词")
    ap.add_argument("--check", action="store_true", help="只检查本地环境，不联网采集")
    ap.add_argument("--keyword", action="append", help="只搜索指定词，可重复；默认使用文件内DEFAULT_KEYWORDS")
    ap.add_argument("--list-keywords", action="store_true", help="仅列出关键词，不联网/不启动浏览器")
    ap.add_argument("--start-date", type=date.fromisoformat, default=START, help="开始日期，不得早于2021-07-24")
    ap.add_argument("--end-date", type=date.fromisoformat, default=END, help="结束日期，不得晚于2026-07-24")
    ap.add_argument("--pages", type=int, default=None, help="可选：本次每词页数上限，未完页保留断点；默认翻至接口结束")
    ap.add_argument("--max-requests", type=int, default=None, help="可选：本次请求上限，含评论数补取；默认不截断")
    ap.add_argument("--delay", type=float, default=60.0, help="请求间隔秒，默认60秒（至少10秒）；每30次休息5分钟")
    ap.add_argument("--rpc-port", type=int, default=9224, help="本机Chrome/Edge调试端口，默认9224；复用浏览器登录态")
    ap.add_argument("--no-comment-counts", action="store_true", help="仅采贴文，不补评论（覆盖报告标记待补采）")
    ap.add_argument("--comment-pages", type=int, help="本次每篇评论页数上限；默认翻完可见一级评论")
    ap.add_argument("--no-year-queries", action="store_true", help="不追加逐年检索词")
    ap.add_argument("--shard", choices=("full", "forward", "reverse"), default="full")
    ap.add_argument("--state-file", type=Path, help="目录搬迁后显式指定原 search-state/*.sqlite3，保留视频ID")
    ap.add_argument("--out", type=Path, default=None, help="增量 CSV 路径；默认 output/csv/快手双减.csv，需保留对应内部状态库")
    ap.add_argument("--repair-fields", type=int, metavar="N", help="最多搜索N页回填已有ID字段；不新增贴文、不修改搜索/评论断点")
    ap.add_argument("--restart", action="store_true", help="显式从首页重扫指定关键词；普通启动绝不自动重扫")
    ap.add_argument("--retry-stalled-comments", action="store_true", default=True,
                    help="兼容旧参数：新一轮默认也补停滞评论，只从原游标尝试；本轮不反复重发")
    args = ap.parse_args(argv)
    if not 1 <= args.rpc_port <= 65535:
        ap.error("RPC端口必须在1–65535之间")
    if ((args.pages is not None and args.pages < 1)
            or (args.max_requests is not None and args.max_requests < 1)
            or (args.comment_pages is not None and args.comment_pages < 1)
            or not math.isfinite(args.delay) or args.delay < 10):
        ap.error("pages/max-requests 必须为正数，delay 必须为至少 10 的有限数")
    if args.repair_fields is not None and args.repair_fields < 1:
        ap.error("repair-fields 必须大于0")
    if not START <= args.start_date <= args.end_date <= END:
        ap.error(f"日期必须满足 {START} <= 开始日期 <= 结束日期 <= {END}，不能扩大本任务范围")
    if args.shard != "full" and (args.keyword or args.requirements or args.no_year_queries or args.restart
                                  or args.start_date != START or args.end_date != END or args.pages is not None
                                  or args.comment_pages is not None or args.max_requests is not None
                                  or args.repair_fields is not None or args.no_comment_counts):
        ap.error("分片只支持原134词、完整日期和无限制分页；不能重置断点或截断请求")
    if args.check:
        check_environment()
        return 0
    try:
        keywords = list(dict.fromkeys(w.strip() for w in args.keyword if w.strip())) if args.keyword else load_keywords(args.requirements)
        if not keywords:
            raise ValueError("关键词不能为空")
        keywords = shard_queries(search_queries(keywords, args.start_date, args.end_date, not args.no_year_queries), args.shard)
        if args.list_keywords:
            for word in keywords:
                logger.info("%s", word)
            logger.info("去重后共 %s 个关键词（含组合检索）", len(keywords))
            return 0
        output = args.out or OUTPUT / "快手双减.csv"
        output = Path(os.path.abspath(output))
        if not output.is_relative_to(OUTPUT) or any(is_link_or_junction(p) for p in (output, *output.parents)):
            raise ValueError("CSV 必须位于根目录output/csv内，且不能经过符号链接或 junction")
        digest = hashlib.sha256(os.path.normcase(str(output)).encode("utf-8")).hexdigest()
        state_path = args.state_file or STATE_DIR / "search-state" / (digest + ".sqlite3")
        if output.exists() and not state_path.exists() and not args.state_file:
            candidates = []
            for candidate in (STATE_DIR / "search-state").glob("*.sqlite3"):
                if is_link_or_junction(candidate):
                    continue
                with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as old_db:
                    saved = old_db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
                old = json.loads(saved[0]) if saved else {}
                if (Path(old.get("output", "")).name == output.name and old.get("start") == str(args.start_date)
                        and old.get("end") == str(args.end_date)):
                    candidates.append(candidate)
            if len(candidates) != 1:
                raise ValueError("目录搬迁后无法唯一定位原状态库；请用 --state-file 指定原 search-state/*.sqlite3，不覆盖 CSV")
            state_path = candidates[0]
            logger.info("检测到目录搬迁，沿用原视频ID状态库：%s", state_path.name)
        client = None
        try:
            prepare_kuaishou_browser(args.rpc_port)
            client = SearchClient(args.rpc_port, args.max_requests, args.delay)
            output.parent.mkdir(parents=True, exist_ok=True)
            logger.info("CSV -> %s；关键词 %s 个；范围 %s～%s", output, len(keywords), args.start_date, args.end_date)
            logger.info("低频采集：单并发，间隔至少%s秒，每30次请求休息5分钟", args.delay)
            logger.warning("分享数、IP 属地暂无已验证来源；全空列不导出，缺口记录在字段报告，不填假0。")
            if args.repair_fields is not None:
                if not output.exists():
                    raise ValueError("尚无贴文CSV，请先正常采集")
                repair_fields(client, keywords, output, state_path, args.start_date, args.end_date, args.repair_fields)
                return 0
            total = export_search(client, keywords, output, args.start_date, args.end_date, args.pages,
                                  not args.no_comment_counts, state_path=state_path, restart=args.restart,
                                  comment_pages=args.comment_pages, retry_stalled=args.retry_stalled_comments)
            logger.info("本次新增：%s 条，%s 次请求；%s。分享数、IP 属地仍缺失；搜索结果不保证覆盖平台全部历史视频。",
                        total, client.used, "达到指定页数或接口末尾，已保存断点" if args.pages else "本轮已遍历全部关键词的可返回分页，下次启动增量扫描")
            report = json.loads(sidecar_path(output.with_name(output.stem + "_覆盖报告.json"), "reports").read_text(encoding="utf-8"))
            pending = report["未完成评论篇数"]
            if pending:
                logger.warning("尚有 %s 篇评论未遍历完成；详见覆盖报告，下次运行续采", pending)
            return 2 if pending else 0
        finally:
            if client:
                client.close()
    except BudgetExhausted:
        logger.info("有界测试达到请求上限；原搜索/评论断点保留，未完成不等于采集成功")
        return 2
    except Exception as exc:
        # 底层浏览器异常可能含完整请求URL；只输出已脱敏业务错误或异常类型。
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError, FileExistsError)) else type(exc).__name__
        location = traceback.extract_tb(exc.__traceback__)[-1]
        logger.error("采集未完成：%s（%s:%s）；如已创建 CSV，已写入的数据保留。",
                     message, Path(location.filename).name, location.lineno)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
