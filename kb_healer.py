"""KB 自愈模块。

Feature 1: try_heal(query) — 同步后重试检索，返回新的 contexts 或 None
Feature 2: suggest_and_add_docs(...) — LLM 分析未解答问题，自动补充 sync_sources.json
"""

import json
import logging
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).parent
_SYNC_SOURCES_PATH = _PROJECT_DIR / "sync_sources.json"
_POTENTIAL_PAGES_PATH = _PROJECT_DIR / "potential_new_pages.json"
_PENDING_SUGGEST_PATH = _PROJECT_DIR / "pending_kb_suggestions.json"


def try_heal(query: str) -> Optional[list]:
    """
    触发全量 KB 同步 → 重建索引 → 重新检索。
    返回 contexts（非空列表），或 None（同步后仍无结果）。
    """
    log.info("[healer] 开始自愈同步, query=%s", query[:40])
    try:
        subprocess.run(
            ["python3", str(_PROJECT_DIR / "lark_sync.py")],
            capture_output=True, text=True, timeout=120,
            cwd=str(_PROJECT_DIR),
        )
    except Exception as e:
        log.warning("[healer] lark_sync 失败: %s", e)

    try:
        from index_manager import build_index
        import kb
        build_index(force=True)
        kb.mark_fresh()
    except Exception as e:
        log.warning("[healer] KB 重建失败: %s", e)
        return None

    try:
        from kb import search
        contexts = search(query)
        return contexts if contexts else None
    except Exception as e:
        log.warning("[healer] search 失败: %s", e)
        return None


def suggest_and_add_docs(
    unanswered: list[tuple[str, str]],
    notify_fn: Callable[[str], None],
) -> None:
    """
    后台线程：拿"潜在新页面清单"（由 lark_sync.py 每次同步后写入 potential_new_pages.json，
    只包含 KB 现有文档里链接到、但尚未同步的 wiki/docx 页面）跟当天未解答问题做 LLM 匹配。

    有匹配 → 写入 pending_kb_suggestions.json 并通知管理员，等待管理员回复
    「确认补充」/「忽略建议」，由 bot.py 的管理员命令处理确认后才真正写入
    sync_sources.json 并同步——不自动改配置，不自动写入。
    notify_fn: 接受一条文本，发 DM 给管理员。
    """
    def _run():
        try:
            if not _POTENTIAL_PAGES_PATH.exists():
                log.info("[healer] potential_new_pages.json 不存在，跳过")
                return
            potential: list[dict] = json.loads(_POTENTIAL_PAGES_PATH.read_text(encoding="utf-8"))
            if not potential:
                log.info("[healer] 无潜在新页面，跳过")
                return
            if not unanswered:
                log.info("[healer] 无未解答问题，跳过匹配")
                return

            data = json.loads(_SYNC_SOURCES_PATH.read_text(encoding="utf-8"))
            kb_files: list = data.get("kb_files", [])
            existing_urls = {
                src.get("url", "") for e in kb_files for src in e.get("sources", [])
            }

            # available: token -> {title, url}，排除已同步的
            available: dict[str, dict] = {}
            for p in potential:
                url = p.get("url", "")
                label = p.get("label", "")
                if not url or url in existing_urls:
                    continue
                token = url.rstrip("/").split("/")[-1]
                available[token] = {"title": label, "url": url}

            if not available:
                log.info("[healer] 潜在新页面均已同步，跳过")
                return

            from openai import OpenAI
            from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
            client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

            questions_text = "\n".join(f"- {q}" for q, _ in unanswered[:20])
            pages_text = "\n".join(
                f"- {info['title']} | token: {token}"
                for token, info in list(available.items())[:60]
            )

            resp = client.chat.completions.create(
                model=LLM_MODEL,
                max_tokens=600,
                temperature=0,
                messages=[{"role": "user", "content": (
                    f"HR Bot 今日未解答的员工问题：\n{questions_text}\n\n"
                    f"知识库尚未同步的候选页面：\n{pages_text}\n\n"
                    "哪些候选页面的内容可能覆盖上述未解答问题？返回 JSON 数组，每项 "
                    "{\"token\":\"...\",\"title\":\"...\"}。"
                    "只返回 JSON，没有合适的候选返回 []。"
                )}],
            )
            suggested = json.loads(resp.choices[0].message.content.strip())
            if not suggested:
                log.info("[healer] LLM 判断无相关候选页面")
                return

            # 只保留 token 确实存在于 available 的项（防止幻觉）
            items = []
            for item in suggested:
                token = (item.get("token") or "").strip()
                info = available.get(token)
                if not info:
                    continue
                items.append({"token": token, "title": info["title"], "url": info["url"]})

            if not items:
                log.info("[healer] LLM 建议的 token 均不在候选清单中，丢弃")
                return

            pending = {
                "id": uuid.uuid4().hex[:8],
                "created": datetime.now().isoformat(timespec="seconds"),
                "items": items,
                "unanswered_count": len(unanswered),
            }
            _PENDING_SUGGEST_PATH.write_text(
                json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("[healer] 已写入待确认建议 %s，共 %d 项", pending["id"], len(items))

            lines = "\n".join(f"  · {it['title']}" for it in items)
            notify_fn(
                f"🔍 发现可能相关的未同步文档\n\n"
                f"基于今日 {len(unanswered)} 条未解答问题，以下页面可能有帮助：\n"
                f"{lines}\n\n"
                f"回复「确认补充」将其加入知识库并同步；回复「忽略建议」可清除此提示。"
            )
        except Exception as e:
            log.error("[healer] suggest_and_add_docs 失败: %s", e, exc_info=True)

    threading.Thread(target=_run, name="kb-gap-healer", daemon=True).start()


def apply_pending_suggestion() -> Optional[dict]:
    """
    管理员回复「确认补充」后调用：把 pending_kb_suggestions.json 里的候选写入
    sync_sources.json，执行同步 + 重建索引。

    返回 {"added": [标题...]}，无待确认项时返回 None。
    """
    if not _PENDING_SUGGEST_PATH.exists():
        return None
    try:
        pending = json.loads(_PENDING_SUGGEST_PATH.read_text(encoding="utf-8"))
        items = pending.get("items", [])
        if not items:
            _PENDING_SUGGEST_PATH.unlink(missing_ok=True)
            return None

        data = json.loads(_SYNC_SOURCES_PATH.read_text(encoding="utf-8"))
        kb_files: list = data.get("kb_files", [])
        existing_urls = {
            src.get("url", "") for e in kb_files for src in e.get("sources", [])
        }

        added = []
        for it in items:
            url = it["url"]
            if url in existing_urls:
                continue
            kb_files.append({
                "local_file": f"{it['title']}.md",
                "sources": [{
                    "url": url,
                    "doc_type": "wiki",
                    "lang": "bilingual",
                    "revision_id": None,
                    "section_marker": None,
                    "notes": it["title"],
                }],
            })
            added.append(it["title"])

        data["kb_files"] = kb_files
        _SYNC_SOURCES_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("[healer] 已确认补充 %d 个文档到 sync_sources.json", len(added))

        subprocess.run(
            ["python3", str(_PROJECT_DIR / "lark_sync.py")],
            capture_output=True, text=True, timeout=180,
            cwd=str(_PROJECT_DIR),
        )
        from index_manager import build_index
        import kb
        build_index(force=True)
        kb.mark_fresh()

        _PENDING_SUGGEST_PATH.unlink(missing_ok=True)
        return {"added": added}
    except Exception as e:
        log.error("[healer] apply_pending_suggestion 失败: %s", e, exc_info=True)
        return None


def dismiss_pending_suggestion() -> bool:
    """管理员回复「忽略建议」后调用：清除待确认建议，不写入任何内容。"""
    if _PENDING_SUGGEST_PATH.exists():
        _PENDING_SUGGEST_PATH.unlink()
        return True
    return False
