"""反馈信用加权（Feedback Credit Weighting）。

设计灵感来自多 Agent 系统的信用机制（credit/trust scoring）：对表现不佳的
执行者降低调度权重。本模块将该思想迁移到 RAG 检索层——

  用户对答案的 👍/👎 反馈，归因到当次答案所引用的知识库文档，累计为每篇
  文档的「信用分」；检索精排时信用分作为乘性权重作用于 reranker 分数：
  被反复踩过的来源自动沉底，被验证过的高质量来源自动靠前。

性质：
- 只影响检索排序，不改动答案生成链路（答案仍是 KB 原文转述，无幻觉风险）
- 贝叶斯平滑，新文档天然中性（0.5），反馈越多权重越激进
- 可用 eval_kb.py --compare 做离线 A/B 对照，验证有效后再上线

信用分：score = (helpful + α) / (helpful + not_helpful + 2α)，先验 α=2
权重：  weight = W_MIN + (W_MAX - W_MIN) × score，默认区间 [0.7, 1.3]
"""

import contextlib
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".local" / "data" / "hr-bot-credit.db"

_PRIOR = 2.0          # 贝叶斯先验强度：α 越大，少量反馈越难撼动中性分
_W_MIN, _W_MAX = 0.7, 1.3

ENABLED = os.environ.get("HR_CREDIT_ENABLED", "1") != "0"  # eval_kb.py --compare 会在进程内覆盖

_CHUNK_SOURCE_RE = re.compile(r'^\[来源:\s*([^|]+?)\s*\|', re.MULTILINE)


@contextlib.contextmanager
def _conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_credit (
            doc         TEXT PRIMARY KEY,
            helpful     INTEGER NOT NULL DEFAULT 0,
            not_helpful INTEGER NOT NULL DEFAULT 0,
            updated     REAL    NOT NULL
        )
    """)
    conn.commit()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def enabled() -> bool:
    return ENABLED


def doc_of(chunk: str) -> str:
    """从 chunk 的 [来源: 文件名 | ...] 头提取文档名；提取失败返回空串。"""
    m = _CHUNK_SOURCE_RE.search(chunk)
    return m.group(1).strip() if m else ""


def score(doc: str) -> float:
    """文档信用分，范围 (0, 1)，无数据时为 0.5（中性）。"""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT helpful, not_helpful FROM doc_credit WHERE doc=?", (doc,)
            ).fetchone()
    except Exception:
        return 0.5
    if not row:
        return 0.5
    helpful, not_helpful = row
    return (helpful + _PRIOR) / (helpful + not_helpful + 2 * _PRIOR)


def weight(doc: str) -> float:
    """信用分映射为乘性权重，范围 [_W_MIN, _W_MAX]，中性文档为 1.0。"""
    return _W_MIN + (_W_MAX - _W_MIN) * score(doc)


def apply(scored: list) -> list:
    """对 [(reranker_score, chunk)] 施加信用权重并按加权分重排。

    reranker 分数缺失（None）的 chunk 不参与加权，统一排在末尾。"""
    weighted = []
    for s, chunk in scored:
        doc = doc_of(chunk)
        if s is not None and doc:
            weighted.append((s * weight(doc), chunk))
        else:
            weighted.append((s, chunk))
    weighted.sort(key=lambda item: item[0] if item[0] is not None else float("-inf"),
                  reverse=True)
    return weighted


def record_feedback(docs: list, helpful: bool) -> None:
    """把一次 👍/👎 归因到一组来源文档（同一文档一次反馈只计一次）。"""
    docs = [d for d in dict.fromkeys(docs) if d]
    if not docs:
        return
    col = "helpful" if helpful else "not_helpful"
    try:
        with _conn() as conn:
            for doc in docs:
                conn.execute(
                    f"INSERT INTO doc_credit (doc, {col}, updated) VALUES (?,?,?) "
                    f"ON CONFLICT(doc) DO UPDATE SET {col}={col}+1, updated=excluded.updated",
                    (doc, 1, time.time()),
                )
        log.info("[credit] %s → %s", "👍" if helpful else "👎", ", ".join(docs))
    except Exception as e:
        log.warning("[credit] 记录反馈失败: %s", e)


def stats() -> list:
    """全量信用档案，供评测报告/管理员工具展示。"""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT doc, helpful, not_helpful FROM doc_credit ORDER BY helpful + not_helpful DESC"
            ).fetchall()
    except Exception:
        return []
    return [
        {"doc": doc, "helpful": h, "not_helpful": nh,
         "score": round(score(doc), 3), "weight": round(weight(doc), 3)}
        for doc, h, nh in rows
    ]
