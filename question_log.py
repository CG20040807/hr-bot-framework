"""问题收集与日报模块。

职责：
- 记录每条用户问题（query、KB 命中数、回答摘要、是否未解答）
- 每天 9:00 向管理员发送 DM 日报：高频问题 Top10 + 未解答问题列表
"""

import contextlib
import json
import logging
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".local" / "data" / "lark-hr-bot-dept-questions.db"

# 管理员的 open_id，用于发送日报 DM（在 config.py 中配置）
from config import REPORT_RECIPIENT as _REPORT_RECIPIENT

# 触发"未解答"的回答关键词（与 llm.py 实际输出保持同步）
_UNANSWERED_MARKERS = [
    # 无知识库命中（llm.py no-context response）
    "没有找到相关内容",
    # LLM 兜底/范围外
    "联系 HR 团队",
    "联系HR团队",
    "请联系 HR",
    "不在我的服务范围",
    # 技术异常
    "出了点小问题",
    # English — no KB hit
    "couldn't find anything on that",
    # English — LLM 兜底
    "contact the HR team",
    "outside what I can help",
    # 技术异常 English
    "something went wrong",
]

# not_helpful 通知去重：1 小时内同一问题不重复发 DM
_not_helpful_sent: dict[str, float] = {}
_NOT_HELPFUL_DEDUP_SECS = 3600


# ── DB 初始化 ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _conn():
    """Context manager that opens a SQLite connection, handles transaction, and closes on exit."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            query        TEXT    NOT NULL,
            kb_hits      INTEGER NOT NULL,
            reply_preview TEXT,
            is_unanswered INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            action       TEXT    NOT NULL,
            query        TEXT,
            reply_preview TEXT
        )
    """)
    conn.commit()
    try:
        with conn:  # auto-commit on success, rollback on exception
            yield conn
    finally:
        conn.close()


# ── 记录问题 ──────────────────────────────────────────────────────────────────

def log_question(query: str, kb_hits: int, reply: str) -> None:
    """记录一条用户问题。kb_hits=0 或回答含兜底语则标记为未解答。"""
    is_unanswered = kb_hits == 0 or any(m in reply for m in _UNANSWERED_MARKERS)
    preview = reply[:120].replace("\n", " ")
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO questions (ts, query, kb_hits, reply_preview, is_unanswered)"
                " VALUES (?,?,?,?,?)",
                (time.time(), query, kb_hits, preview, int(is_unanswered)),
            )
    except Exception as e:
        log.warning("问题记录写入失败: %s", e)


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def _cluster_queries(query_list: list[str]) -> list[tuple[str, int]]:
    """按 token 重叠把相似问题合并，返回 (代表问题, 合并后总次数) 列表，按次数降序。"""
    from index_manager import _tokenize
    counter = Counter(query_list)
    # 防御：确保 _tokenize 返回的是 set（跨版本安全）
    unique = list(counter.keys())
    tokenized = {q: set(_tokenize(q)) for q in unique}

    assigned: set[str] = set()
    groups: list[tuple[str, int]] = []
    for q1 in sorted(unique, key=lambda q: -counter[q]):  # 从高频开始聚合
        if q1 in assigned:
            continue
        t1 = tokenized[q1]
        group = [q1]
        assigned.add(q1)
        for q2 in unique:
            if q2 in assigned:
                continue
            t2 = tokenized[q2]
            overlap = len(t1 & t2)
            min_size = min(len(t1), len(t2))
            if min_size > 0 and overlap / min_size >= 0.5:
                group.append(q2)
                assigned.add(q2)
        total = sum(counter[q] for q in group)
        groups.append((q1, total))

    return sorted(groups, key=lambda x: -x[1])


def generate_report(hours: int = 24) -> dict:
    """统计过去 N 小时的问题数据，返回 {total, top, unanswered}。"""
    since = time.time() - hours * 3600
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT query, reply_preview, is_unanswered FROM questions WHERE ts >= ?",
                (since,),
            ).fetchall()
    except Exception as e:
        log.warning("读取问题日志失败: %s", e)
        return {}

    total = len(rows)
    freq = _cluster_queries([r[0] for r in rows])[:10]
    # 去重并按出现次数排序，避免每次日报顺序随机
    unanswered_counter = Counter(r[0] for r in rows if r[2])
    preview_map = {r[0]: r[1] for r in rows if r[2]}
    unanswered = [(q, preview_map[q]) for q, _ in unanswered_counter.most_common()]
    return {"total": total, "top": freq, "unanswered": unanswered}


# ── DM 发送 ───────────────────────────────────────────────────────────────────

def _send_dm(text: str) -> None:
    result = subprocess.run(
        ["lark-cli", "im", "+messages-send",
         "--user-id", _REPORT_RECIPIENT,
         "--text", text,
         "--as", "bot"],
        capture_output=True, text=True, timeout=15,
    )
    stdout = result.stdout.strip()
    try:
        ok = json.loads(stdout).get("ok", False)
    except (json.JSONDecodeError, AttributeError):
        ok = '"ok":true' in stdout.replace(" ", "").replace("\n", "")
    if not ok:
        log.warning("日报发送失败: %s", stdout[:300])
    else:
        log.info("日报已发送至管理员")


def log_feedback(action: str, query: str, reply_preview: str) -> None:
    """记录用户的回答反馈（helpful / not_helpful）。"""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO feedback (ts, action, query, reply_preview) VALUES (?,?,?,?)",
                (time.time(), action, query[:300], reply_preview[:200]),
            )
    except Exception as e:
        log.warning("反馈记录写入失败: %s", e)


def _lookup_display_name(open_id: str) -> str:
    """用 lark-cli 把 open_id 转成显示名，失败时原样返回 open_id。"""
    try:
        r = subprocess.run(
            ["lark-cli", "api", "GET", f"/open-apis/contact/v3/users/{open_id}",
             "--as", "bot", "--params", '{"user_id_type":"open_id"}'],
            capture_output=True, text=True, timeout=8,
        )
        data = json.loads(r.stdout.split("\n", 1)[-1] if r.stdout.startswith("Fetch") else r.stdout)
        name = data.get("data", {}).get("user", {}).get("name", "")
        return name or open_id
    except Exception:
        return open_id


def notify_not_helpful(query: str, reply_preview: str, user_open_id: str = "") -> None:
    """向管理员发 DM，通知有「没帮助」的问题。同一问题 1 小时内去重。"""
    now = time.time()
    dedup_key = query[:60]
    last_sent = _not_helpful_sent.get(dedup_key, 0)
    if now - last_sent < _NOT_HELPFUL_DEDUP_SECS:
        log.info("not_helpful 通知去重，跳过: %s", dedup_key[:40])
        return
    _not_helpful_sent[dedup_key] = now
    expired = [k for k, ts in _not_helpful_sent.items() if now - ts > _NOT_HELPFUL_DEDUP_SECS * 2]
    for k in expired:
        del _not_helpful_sent[k]
    if user_open_id:
        display = _lookup_display_name(user_open_id)
        user_line = f"用户：{display}（{user_open_id}）\n\n"
    else:
        user_line = ""
    text = (
        f"⚠️ HR Bot 收到「没帮助」反馈\n\n"
        f"{user_line}"
        f"问题：{query[:120]}\n\n"
        f"回答摘要：{reply_preview[:180]}"
    )
    _send_dm(text)


def send_admin_dm(text: str) -> None:
    """向管理员发送 DM（公开接口，供外部模块调用）。"""
    _send_dm(text)


def send_daily_report() -> dict:
    """生成并发送日报，无数据时跳过。返回 report dict（供调用方取 unanswered）。"""
    report = generate_report(hours=24)
    if not report or report.get("total", 0) == 0:
        log.info("过去 24 小时无问题记录，跳过日报")
        return {}

    total = report["total"]
    date_str = datetime.now().strftime("%m/%d")
    lines = [f"📊 HR Bot 日报 {date_str}  共 {total} 条问题\n"]

    lines.append("▌ 高频问题 Top 10")
    for i, (q, cnt) in enumerate(report["top"], 1):
        q_short = q[:55] + "…" if len(q) > 55 else q
        lines.append(f"  {i}. [{cnt}次] {q_short}")

    unanswered = report["unanswered"]
    if unanswered:
        lines.append(f"\n▌ 未解答问题 ({len(unanswered)} 条) — 建议补充知识库")
        for q, preview in unanswered[:20]:
            q_short = q[:55] + "…" if len(q) > 55 else q
            lines.append(f"  · {q_short}")
    else:
        lines.append("\n▌ 未解答问题：无 ✓")

    _send_dm("\n".join(lines))
    return report


# ── 定时线程 ──────────────────────────────────────────────────────────────────

def _seconds_until_next(hour: int = 9, minute: int = 0) -> float:
    """计算距离下一个 HH:MM 的秒数。"""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def start_reporter_thread(post_report_hook=None) -> None:
    """启动后台线程，每天 9:00 发送日报。post_report_hook(unanswered) 在日报后调用。"""
    def _loop():
        while True:
            wait = _seconds_until_next(hour=9, minute=0)
            log.info("日报将在 %.0f 分钟后发送（下次 9:00）", wait / 60)
            time.sleep(wait)
            try:
                report = send_daily_report()
                if post_report_hook and report:
                    unanswered = report.get("unanswered", [])
                    if unanswered:
                        post_report_hook(unanswered)
            except Exception as e:
                log.error("发送日报出错: %s", e, exc_info=True)

    t = threading.Thread(target=_loop, name="daily-reporter", daemon=True)
    t.start()
    log.info("日报定时线程已启动（每日 9:00 发送）")
