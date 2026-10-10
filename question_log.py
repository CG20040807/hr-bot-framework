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
    # LLM 兜底"知识库未覆盖"的各种措辞（未找到/未涵盖/未包含/未说明）
    "未找到",
    "未涵盖",
    "未包含",
    "未说明",
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

# unanswered 即时通知去重：同一问题 1 小时内只发一次（可能多人问同一个未覆盖问题）
_unanswered_sent: dict[str, float] = {}


def is_unanswered_reply(kb_hits: int, reply: str) -> bool:
    """与 log_question 相同的未解答判定：0 命中或回答含兜底语。"""
    return kb_hits == 0 or any(m in reply for m in _UNANSWERED_MARKERS)


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
    # 迁移：questions 增加 sender 列（提问人显示名，2026-08-03）
    qcols = [r[1] for r in conn.execute("PRAGMA table_info(questions)")]
    if "sender" not in qcols:
        conn.execute("ALTER TABLE questions ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 迁移：feedback 增加 sender 列（点 👍/👎 的人，2026-08-03）
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(feedback)")]
    if "sender" not in fcols:
        conn.execute("ALTER TABLE feedback ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 保留期：只留最近 400 天数据，防止全员上线后表无限增长
    conn.execute("DELETE FROM questions WHERE ts < ?", (time.time() - 400 * 86400,))
    conn.execute("DELETE FROM feedback  WHERE ts < ?", (time.time() - 400 * 86400,))
    conn.commit()
    try:
        with conn:  # auto-commit on success, rollback on exception
            yield conn
    finally:
        conn.close()


# ── 记录问题 ──────────────────────────────────────────────────────────────────

def log_question(query: str, kb_hits: int, reply: str,
                 sender: str = "", unanswered=None) -> None:
    """记录一条用户问题。kb_hits=0 或回答含兜底语则标记为未解答。
    unanswered 显式传 False 可覆盖（guard 固定答案 kb_hits=0 但属已解答）。
    sender 是提问人显示名——open_id 是 app 级隔离的，只能由 bot 进程用自家
    SDK 解析，本模块的 lark-cli 通道解析不了跨 app open_id，故由调用方传入。"""
    is_unanswered = is_unanswered_reply(kb_hits, reply) \
        if unanswered is None else unanswered
    preview = reply[:120].replace("\n", " ")
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO questions (ts, query, kb_hits, reply_preview, is_unanswered, sender)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), query, kb_hits, preview, int(is_unanswered), sender),
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
                "SELECT query, reply_preview, is_unanswered, sender FROM questions WHERE ts >= ?",
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
    # 每个未解答问题最近的提问人（后面的覆盖前面的）
    sender_map = {}
    for r in rows:
        if r[2] and r[3]:
            sender_map[r[0]] = r[3]
    return {"total": total, "top": freq, "unanswered": unanswered,
            "unanswered_senders": sender_map}


def get_bot_stats(since: float) -> dict:
    """返回 since 以来的 bot 使用统计。

    返回:
        dict with:
            asker_count: 去重提问人数（sender 不为空的）
            question_count: 问题总条数
            not_helpful_users / not_helpful_count: 点"没帮助"的去重人
            helpful_users / helpful_count: 点"有帮助"的去重人
            total_questions / total_askers: 上线（2026-08-03）以来累计
    """
    _LAUNCH_TS = 1785600000.0  # 2026-08-03，早于首条问题即可
    empty = {"asker_count": 0, "question_count": 0,
             "not_helpful_users": [], "not_helpful_count": 0,
             "helpful_users": [], "helpful_count": 0,
             "total_questions": 0, "total_askers": 0}
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT sender FROM questions WHERE ts >= ? AND sender != ''",
                (since,),
            ).fetchall()
            asker_count = len(rows)

            question_count = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE ts >= ?", (since,),
            ).fetchone()[0]

            def _fb_users(action):
                r = conn.execute(
                    "SELECT DISTINCT sender FROM feedback WHERE ts >= ? AND action = ? AND sender != ''",
                    (since, action),
                ).fetchall()
                return [x[0] for x in r]

            not_helpful_users = _fb_users("not_helpful")
            helpful_users = _fb_users("helpful")

            total_questions = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE ts >= ?", (_LAUNCH_TS,),
            ).fetchone()[0]
            total_askers = conn.execute(
                "SELECT COUNT(DISTINCT sender) FROM questions WHERE ts >= ? AND sender != ''",
                (_LAUNCH_TS,),
            ).fetchone()[0]

            return {
                "asker_count": asker_count,
                "question_count": question_count,
                "not_helpful_users": not_helpful_users,
                "not_helpful_count": len(not_helpful_users),
                "helpful_users": helpful_users,
                "helpful_count": len(helpful_users),
                "total_questions": total_questions,
                "total_askers": total_askers,
            }
    except Exception as e:
        log.warning("读取 bot 统计失败: %s", e)
        return empty


# ── DM 发送 ───────────────────────────────────────────────────────────────────

def _send_dm(text: str) -> None:
    last_err = ""
    for attempt in range(3):
        if attempt > 0:
            time.sleep(attempt)  # 1s, 2s backoff
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
        if ok:
            log.info("日报已发送至管理员")
            return
        last_err = stdout[:300]
    log.warning("日报发送失败（3次重试均失败）: %s", last_err)


def log_feedback(action: str, query: str, reply_preview: str, sender: str = "") -> None:
    """记录用户的回答反馈（helpful / not_helpful），sender 为点击人显示名。"""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO feedback (ts, action, query, reply_preview, sender) VALUES (?,?,?,?,?)",
                (time.time(), action, query[:300], reply_preview[:200], sender),
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


def notify_not_helpful(query: str, reply_preview: str, user_open_id: str = "",
                       user_name: str = "") -> None:
    """向管理员发 DM，通知有「没帮助」的问题。同一问题 1 小时内去重。
    user_name 由调用方（bot 进程自家 SDK）解析后传入；否则退回 lark-cli 解析
    （跨 app open_id 会失败，退回原样显示）。"""
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
        display = user_name or _lookup_display_name(user_open_id)
        user_line = f'<at user_id="{user_open_id}">{display}</at> 点了「没帮助」\n\n'
    elif user_name:
        user_line = f"{user_name} 点了「没帮助」\n\n"
    else:
        user_line = ""
    text = (
        f"⚠️ **HR Bot 收到「没帮助」反馈**\n\n"
        f"{user_line}"
        f"**原问题：**\n{query[:300]}\n\n"
        f"**被点「没帮助」的回答原文：**\n{reply_preview[:1500]}"
    )
    _send_dm_markdown(text)


def notify_unanswered(query: str, reply_preview: str, user_open_id: str = "",
                      user_name: str = "") -> None:
    """知识库未覆盖的问题：立刻把问题+提问人 DM 给管理员（@提问人）。
    与 notify_not_helpful 同款 1 小时按问题去重，避免多人问同一问题时刷屏。"""
    now = time.time()
    dedup_key = query[:60]
    if now - _unanswered_sent.get(dedup_key, 0) < _NOT_HELPFUL_DEDUP_SECS:
        log.info("unanswered 通知去重，跳过: %s", dedup_key[:40])
        return
    _unanswered_sent[dedup_key] = now
    expired = [k for k, ts in _unanswered_sent.items() if now - ts > _NOT_HELPFUL_DEDUP_SECS * 2]
    for k in expired:
        del _unanswered_sent[k]
    if user_open_id:
        display = user_name or _lookup_display_name(user_open_id)
        user_line = f'<at user_id="{user_open_id}">{display}</at> 提问，知识库未覆盖\n\n'
    elif user_name:
        user_line = f"{user_name} 提问，知识库未覆盖\n\n"
    else:
        user_line = ""
    text = (
        f"📭 **HR Bot 遇到知识库未覆盖问题**\n\n"
        f"{user_line}"
        f"**原问题：**\n{query[:300]}\n\n"
        f"**给用户的回答原文：**\n{reply_preview[:1500]}"
    )
    _send_dm_markdown(text)


def _send_dm_markdown(text: str) -> None:
    """富文本 DM（post/markdown 渲染），用于「没帮助」和「未解答」通知；日报保持纯文本。"""
    last_err = ""
    for attempt in range(3):
        if attempt > 0:
            time.sleep(attempt)
        result = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--user-id", _REPORT_RECIPIENT,
             "--markdown", text,
             "--as", "bot"],
            capture_output=True, text=True, timeout=15,
        )
        stdout = result.stdout.strip()
        try:
            ok = json.loads(stdout).get("ok", False)
        except (json.JSONDecodeError, AttributeError):
            ok = '"ok":true' in stdout.replace(" ", "").replace("\n", "")
        if ok:
            log.info("not_helpful 富文本通知已发送")
            return
        last_err = stdout[:300]
    log.warning("not_helpful 通知发送失败（3次重试均失败）: %s", last_err)


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
    senders = report.get("unanswered_senders", {})
    if unanswered:
        lines.append(f"\n▌ 未解答问题 ({len(unanswered)} 条) — 建议补充知识库")
        for q, preview in unanswered[:20]:
            q_short = q[:55] + "…" if len(q) > 55 else q
            who = senders.get(q, "")
            lines.append(f"  · {q_short}" + (f"（{who}）" if who else ""))
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
            # 定时器可能提前零点几秒唤醒：补足到 9:00，否则醒来时未到整点，
            # 下一轮会算出一个 ~0 分钟的等待，导致日报被评估两次
            now = datetime.now()
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now < target:
                time.sleep((target - now).total_seconds())
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
