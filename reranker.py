"""
Cross-encoder 重排序 — BAAI/bge-reranker-v2-m3

中英双语，本地运行。在 BM25+向量双路 RRF 合并后二次精排，
低于阈值的 chunk 直接丢弃（宁可不答，不能答错）。
"""

import logging
import os
import threading

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

log = logging.getLogger(__name__)

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_model      = None
_model_lock = threading.Lock()
_loading    = False

# bge-reranker 输出为 logit（未经 sigmoid），经验值：
#   > 3   高度相关
#   0~3   相关
#  -1~0   弱相关（仍可接受）
#  < -1   噪音，送进 LLM 只会引发幻觉
# 从 -2.0 收紧到 -1.0：宁可少答，不能答错
_THRESHOLD = -1.0


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from sentence_transformers import CrossEncoder
            log.info("加载 Reranker 模型 %s ...", _MODEL_NAME)
            _model = CrossEncoder(_MODEL_NAME, max_length=512)
            log.info("Reranker 模型加载完成")
        except Exception as e:
            log.warning("Reranker 加载失败: %s，将跳过重排序（降级）", e)
            _model = None
    return _model


def preload_async() -> None:
    """后台异步加载，不阻塞启动。"""
    global _loading
    if _model is not None or _loading:
        return
    _loading = True

    def _run():
        global _loading
        try:
            _get_model()
        finally:
            _loading = False

    threading.Thread(target=_run, daemon=True, name="reranker-preload").start()
    log.info("Reranker 后台加载已启动")


def _keyword_window(query: str, chunk: str, window: int = 600) -> str:
    """提取 chunk 中关键词密度最高的窗口，用于 reranker 打分，避免长 chunk 末尾内容被截断。"""
    import re
    skip = {'怎么', '什么', '如何', '多少', '为什么', '几天', '多久', '怎样', '是否', '可以', '需要'}
    terms = [t for t in re.findall(r'[一-鿿]{2,}|\w{3,}', query) if t not in skip]
    if not terms:
        return chunk[:window]
    best_pos, best_count = 0, 0
    for i in range(0, max(1, len(chunk) - window + 1), 50):
        seg = chunk[i:i + window]
        cnt = sum(seg.count(t) for t in terms)
        if cnt > best_count:
            best_count, best_pos = cnt, i
    return chunk[best_pos:best_pos + window]


def rerank(query: str, chunks: list[str], threshold: float = _THRESHOLD,
           window_query: str = None, return_scores: bool = False):
    """
    对 chunks 用 cross-encoder 精排。
    - 用关键词窗口（600字）打分，返回原始完整 chunk（避免长 chunk 末尾被 max_length 截断）
    - window_query: 用于关键词窗口选取的查询（可包含 KB 原文扩展词），scoring 仍用原始 query
    - 低于 threshold 的 chunk 丢弃（宁可不答不能答错）
    - 模型未就绪时原样返回（降级到 RRF 排序）
    - return_scores=True 时返回 [(score, chunk)]；降级/异常时 score 记为 None
      （调用方据此判断"分数不可用"并回退到不按分数过滤）
    """
    if not chunks:
        return [] if not return_scores else []

    model = _get_model()
    if model is None:
        return chunks if not return_scores else [(None, c) for c in chunks]  # 降级

    try:
        wq = window_query if window_query else query
        windows = [_keyword_window(wq, c) for c in chunks]
        pairs  = [(query, w) for w in windows]  # score with original query
        scores = model.predict(pairs, show_progress_bar=False)
        ranked = sorted(zip(scores, chunks), key=lambda x: -x[0])

        passed = [(float(s), c) for s, c in ranked if s >= threshold]

        log.info(
            "[reranker] %d/%d chunks 通过阈值 %.1f | top=%.2f worst=%.2f",
            len(passed), len(chunks), threshold,
            ranked[0][0] if ranked else 0,
            ranked[-1][0] if ranked else 0,
        )

        if not passed:
            log.info("[reranker] 全部 chunk 低于阈值，返回空（宁可不答）")
            return []

        return passed if return_scores else [c for _, c in passed]

    except Exception as e:
        log.warning("Reranker 推理失败: %s，降级返回原始顺序", e)
        return chunks if not return_scores else [(None, c) for c in chunks]
