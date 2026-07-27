"""
向量检索 — paraphrase-multilingual-MiniLM-L12-v2 (384 维, 中英双语, ~220MB)

- 索引构建：把所有 KB 章节 encode 成向量，保存为 .npy + meta.json
- 查询：encode 查询串 → cosine similarity → top-k 结果
- 与 BM25+IDF 互补：向量检索擅长语义匹配，BM25 擅长关键词精准匹配
"""

import json
import logging
import threading
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

from config import DOCS_DIR

log = logging.getLogger(__name__)

_MODEL_NAME = "jinaai/jina-embeddings-v2-base-zh"
_VECTORS_PATH = DOCS_DIR / "kb-vectors.npy"
_META_PATH    = DOCS_DIR / "kb-vectors-meta.json"

_model   = None
_model_loading = False           # True while background thread is downloading
_vectors: Optional[np.ndarray] = None
_meta:    Optional[list]       = None


def _get_model():
    global _model
    if _model is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastembed import TextEmbedding
            log.info("加载向量模型 %s ...", _MODEL_NAME)
            _model = TextEmbedding(_MODEL_NAME)
        log.info("向量模型加载完成")
    return _model


def _preload_model_bg():
    global _model_loading
    import os
    # 用永久目录存模型，避免 /var/folders (temp) 重启被清空
    cache_dir = str(Path.home() / ".cache" / "fastembed_cache")
    os.makedirs(cache_dir, exist_ok=True)
    os.environ.setdefault("FASTEMBED_CACHE_PATH", cache_dir)
    try:
        _get_model()
    except Exception as e:
        log.warning("向量模型后台加载失败: %s", e)
    finally:
        _model_loading = False


def preload_async():
    """后台异步加载向量模型，不阻塞主线程。首次调用有效，之后幂等。"""
    global _model_loading
    if _model is not None or _model_loading:
        return
    _model_loading = True
    t = threading.Thread(target=_preload_model_bg, daemon=True, name="vector-preload")
    t.start()
    log.info("向量模型后台加载已启动")


def _encode(texts: list[str]) -> np.ndarray:
    """Encode texts, return L2-normalized float32 array."""
    model = _get_model()
    vecs = np.array(list(model.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-10)


def build(index: dict) -> None:
    """对 KB 索引中所有章节做 embedding 并保存到磁盘。"""
    global _vectors, _meta
    sections = []
    for fname, fdata in index.get("files", {}).items():
        for s in fdata["sections"]:
            sections.append({
                "fname":   fname,
                "mtime":   fdata["mtime"],
                "title":   s["title"],
                "content": s["content"],
            })

    if not sections:
        log.warning("vector_store.build: 没有章节可向量化")
        return

    # multilingual-e5 用 "passage: " 前缀表示文档段落
    texts = [f"passage: {s['title']} {s['content'][:400]}" for s in sections]

    log.info("向量化 %d 个 KB 章节...", len(sections))
    t0 = time.time()
    vecs = _encode(texts)
    log.info("向量化完成: %d 章节, %.1fs", len(sections), time.time() - t0)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    np.save(_VECTORS_PATH, vecs)
    _META_PATH.write_text(
        json.dumps(sections, ensure_ascii=False, indent=None), encoding="utf-8"
    )
    _vectors = vecs
    _meta    = sections
    log.info("向量索引已保存: %s", _VECTORS_PATH)


def _load() -> bool:
    """从磁盘加载已构建的向量索引，返回是否成功。"""
    global _vectors, _meta
    try:
        _vectors = np.load(_VECTORS_PATH)
        _meta    = json.loads(_META_PATH.read_text(encoding="utf-8"))
        log.info("向量索引加载完成: %d 章节", len(_meta))
        return True
    except Exception as e:
        log.warning("向量索引加载失败: %s", e)
        return False


_MIN_SIMILARITY = 0.25  # 低于此 cosine 相似度的 chunk 不进 RRF（明显无关，宁可不答）


def search(query: str, top_k: int = 12, min_score: float = _MIN_SIMILARITY,
           is_hyde: bool = False) -> list[tuple]:
    """
    返回 [(score, fname, mtime, title, content), ...] 按 cosine 分数降序。
    首次调用时加载磁盘索引；若模型未就绪则立即返回空列表（降级到纯 BM25）。
    低于 min_score 的结果过滤掉（宁可不答不能答错）。
    is_hyde=True 时用 "passage: " 前缀编码（假设答案语义更接近 KB 原文）。
    """
    global _vectors, _meta
    if _model is None:
        return []   # 模型还在下载，跳过向量检索，降级 BM25

    if _vectors is None and not _load():
        return []

    prefix = "passage: " if is_hyde else "query: "
    q_vec = _encode([f"{prefix}{query}"])[0]
    scores = _vectors @ q_vec                 # cosine similarity (已归一化)
    top_idx = np.argsort(scores)[::-1][:top_k * 2]  # 多取一些，过滤后还够用

    results = [
        (float(scores[i]),
         _meta[i]["fname"],
         _meta[i]["mtime"],
         _meta[i]["title"],
         _meta[i]["content"])
        for i in top_idx
        if float(scores[i]) >= min_score
    ]
    return results[:top_k]
