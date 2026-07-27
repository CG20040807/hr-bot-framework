"""
lark_sync.py — 自动同步 Lark 知识库到本地 KB 文件。

功能：
- 读取 sync_sources.json，逐条检查 Lark 文档的 revision_id
- 仅当 revision_id 变化时才更新本地 KB 文件
- 支持 wiki 页面（HTML）和 docx 文件（HTML）
- 支持多源合并文件（通过 section_marker 定位要更新的段落）
- 更新后自动重建 KB 索引
- 支持 --dry-run 模式（只检查，不写文件）
- 持久化 revision_id 到 .sync_state.json

用法：
  python3 lark_sync.py               # 增量检查，只更新有变化的源
  python3 lark_sync.py --dry-run     # 检查但不写入
  python3 lark_sync.py --force       # 强制重新拉取所有源
"""

import argparse
import hashlib
import os
import json
import logging
import re
import subprocess
import sys
import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
SYNC_SOURCES = SCRIPT_DIR / "sync_sources.json"
SYNC_STATE = SCRIPT_DIR / ".sync_state.json"


# ── HTML → Markdown 转换器 ─────────────────────────────────────────────────────

class LarkHTMLToMarkdown(HTMLParser):
    """将 lark-cli 返回的 HTML 内容转换为 Markdown 格式。"""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []
        self._stack: list[str] = []
        self._current_text = ""
        self._list_level = 0
        self._list_counters: list[int] = []
        self._list_types: list[str] = []  # "ol" or "ul"
        self._skip_tags = {"sub-page-list", "sub-page"}

    def _push(self, tag):
        self._stack.append(tag)

    def _pop(self, tag):
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def _in_tag(self, tag):
        return tag in self._stack

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag in self._skip_tags:
            self._push("skip")
            return
        if tag == "title":
            self._push("title")
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush_text()
            self._push(tag)
        elif tag == "p":
            self._flush_text()
            self._push("p")
        elif tag == "ol":
            self._list_level += 1
            self._list_counters.append(0)
            self._list_types.append("ol")
        elif tag == "ul":
            self._list_level += 1
            self._list_counters.append(0)
            self._list_types.append("ul")
        elif tag == "li":
            self._flush_text()
            if self._list_types and self._list_types[-1] == "ol":
                self._list_counters[-1] += 1
                self._push(f"li_ol_{self._list_level}")
            else:
                self._push(f"li_ul_{self._list_level}")
        elif tag in ("bold", "b", "strong"):
            self._current_text += "**"
            self._push(tag)
        elif tag in ("italic", "i", "em"):
            self._current_text += "_"
            self._push(tag)
        elif tag == "a":
            href = attrs_dict.get("href", "")
            self._push(f"a:{href}")
        elif tag in ("br",):
            self._current_text += "\n"

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            if self._stack and self._stack[-1] == "skip":
                self._stack.pop()
            return

        if tag == "title":
            self._flush_text()
            self._pop("title")
        elif tag in ("h1", "h2", "h3", "h4"):
            text = self._current_text.strip()
            self._current_text = ""
            level = int(tag[1])
            prefix = "#" * (level + 1)  # shift: h1→##, h2→###, h3→####
            self.lines.append(f"\n{prefix} {text}\n")
            self._pop(tag)
        elif tag == "p":
            text = self._current_text.strip()
            self._current_text = ""
            if text:
                self.lines.append(text + "\n")
            self._pop("p")
        elif tag == "ol":
            self._list_level -= 1
            if self._list_counters:
                self._list_counters.pop()
            if self._list_types:
                self._list_types.pop()
        elif tag == "ul":
            self._list_level -= 1
            if self._list_counters:
                self._list_counters.pop()
            if self._list_types:
                self._list_types.pop()
        elif tag == "li":
            text = self._current_text.strip()
            self._current_text = ""
            indent = "  " * (self._list_level - 1) if self._list_level > 1 else ""
            if self._stack and self._stack[-1].startswith("li_ol_"):
                counter = self._list_counters[-1] if self._list_counters else 1
                self.lines.append(f"{indent}{counter}. {text}")
            else:
                self.lines.append(f"{indent}· {text}")
            # pop the li_ marker
            if self._stack and self._stack[-1].startswith("li_"):
                self._stack.pop()
        elif tag in ("bold", "b", "strong"):
            self._current_text += "**"
            self._pop(tag)
        elif tag in ("italic", "i", "em"):
            self._current_text += "_"
            self._pop(tag)
        elif tag == "a":
            # find and pop the a: entry
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i].startswith("a:"):
                    href = self._stack.pop(i)[2:]
                    text = self._current_text.strip()
                    self._current_text = ""
                    if href:
                        self._current_text = f"[{text}]({href})"
                    else:
                        self._current_text = text
                    break

    def handle_data(self, data):
        if self._stack and self._stack[-1] == "skip":
            return
        self._current_text += data

    def _flush_text(self):
        text = self._current_text.strip()
        self._current_text = ""
        if text:
            self.lines.append(text)

    def get_markdown(self) -> str:
        self._flush_text()
        return "\n".join(self.lines).strip()


def html_to_markdown(html_content: str) -> str:
    """Convert Lark HTML content to Markdown."""
    parser = LarkHTMLToMarkdown()
    parser.feed(html_content)
    md = parser.get_markdown()
    # Clean up: collapse 3+ blank lines to 2
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md


# ── Sheet → Markdown 转换器 ────────────────────────────────────────────────────

def _cell_value(cell) -> str:
    """Extract plain text from a sheet cell (str / None / rich-text list)."""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        parts = []
        for item in cell:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "url":
                link = item.get("link", "")
                text = item.get("text", "")
                # mailto: → keep just the email address
                parts.append(link[7:] if link.startswith("mailto:") else (text or link))
            else:
                parts.append(item.get("text", ""))
        return "".join(parts).strip()
    return str(cell).strip()


def sheet_to_markdown(values: list) -> str:
    """Convert sheets +read row data to KB-friendly Markdown.

    Groups rows by 一级部门 under ## headings so the file splits into
    manageable sections during indexing (avoids 9000+ char single-section).
    """
    if not values or len(values) < 3:
        return ""

    # Row 0: title (may be bilingual with \n separator)
    title_raw = _cell_value(values[0][0]) if values[0] else ""
    title = " / ".join(p.strip() for p in title_raw.split("\n") if p.strip())

    # Row 2: column headers — take first line of each bilingual header
    header_row = (list(values[2]) + [None] * 5)[:5]
    headers = []
    for h in header_row:
        raw = _cell_value(h)
        headers.append(raw.split("\n")[0].strip() if "\n" in raw else raw)

    # Rows 3+: data; carry forward dept name columns (0, 1) for merged cells
    last_dept = [""] * 5
    rows = []
    for row in values[3:]:
        cells = (list(row) + [None] * 5)[:5]
        display = []
        for i, cell in enumerate(cells):
            val = _cell_value(cell)
            if val:
                last_dept[i] = val
                display.append(val)
            else:
                display.append(last_dept[i] if i < 2 else "")
        rows.append(display)

    # Group by 一级部门 (col 0), emit ## heading per group
    # Sub-table skips col 0 (now the heading) to avoid redundancy
    sub_headers = headers[1:]
    sep = "|" + " --- |" * len(sub_headers)

    lines = [f"# {title}", ""]
    current_dept = None
    for row in rows:
        dept = row[0]
        if dept != current_dept:
            current_dept = dept
            dept_heading = dept.replace("\n", " ").strip()
            lines.append(f"\n## {dept_heading}\n")
            lines.append("| " + " | ".join(sub_headers) + " |")
            lines.append(sep)
        cleaned = [c.replace("\n", " ").strip() for c in row[1:]]
        lines.append("| " + " | ".join(cleaned) + " |")

    return "\n".join(lines)


# ── lark-cli 调用 ──────────────────────────────────────────────────────────────

def _extract_sheet_token(url: str) -> Optional[str]:
    """Extract spreadsheet token from a /sheets/TOKEN URL."""
    m = re.search(r'/sheets/([A-Za-z0-9]+)', url)
    return m.group(1) if m else None


def _extract_base_token(url: str) -> Optional[str]:
    """Extract base token from a /base/TOKEN URL."""
    m = re.search(r'/base/([A-Za-z0-9]+)', url)
    return m.group(1) if m else None


def _lark_table_to_md(table_str: str) -> str:
    """将 <lark-table>...</lark-table> 内容转换为 markdown 管道表格。"""
    rows = re.findall(r'<lark-tr>(.*?)</lark-tr>', table_str, re.DOTALL)
    if not rows:
        return ''
    result = []
    for i, row in enumerate(rows):
        cells = re.findall(r'<lark-t[dh][^>]*>(.*?)</lark-t[dh]>', row, re.DOTALL)
        clean = []
        for c in cells:
            c = re.sub(r'\{[^}]+\}', '', c)       # 去掉 {align=...}
            c = re.sub(r'<[^>]+>', '', c)           # 去掉内层标签
            c = re.sub(r'\*+([^*]+)\*+', r'\1', c) # 去掉加粗
            c = ' '.join(c.split())
            clean.append(c.strip())
        if clean:
            result.append('| ' + ' | '.join(clean) + ' |')
            if i == 0:
                result.append('|' + '---|' * len(clean))
    return '\n'.join(result)


def lark_markdown_to_clean(md: str) -> str:
    """将 Lark 特有 markdown 标签转换为纯文本/标准 markdown。"""
    # lark-table → pipe table
    md = re.sub(
        r'<lark-table[^>]*>(.*?)</lark-table>',
        lambda m: _lark_table_to_md(m.group(1)),
        md, flags=re.DOTALL
    )
    # mention-doc → [标题](URL)，保留链接供 bot 推送
    def _mention_doc_to_link(m: re.Match) -> str:
        full = m.group(0)
        title = m.group(1).strip()
        token_m = re.search(r'token="([^"]+)"', full)
        type_m = re.search(r'type="([^"]+)"', full)
        if not token_m or not type_m:
            return title
        _type_path = {"wiki": "wiki", "docx": "docx", "sheet": "sheets", "bitable": "base"}
        path = _type_path.get(type_m.group(1), type_m.group(1))
        url = f"https://your-tenant.larksuite.com/{path}/{token_m.group(1)}"
        return f"[{title}]({url})"
    md = re.sub(r'<mention-doc[^>]*>(.*?)</mention-doc>', _mention_doc_to_link, md, flags=re.DOTALL)
    # text 样式标签 → 内容
    md = re.sub(r'<text[^>]*>(.*?)</text>', r'\1', md, flags=re.DOTALL)
    # callout / quote-container / grid / column / view → 保留内容
    for tag in ('callout', 'quote-container', 'grid', 'column', 'view'):
        md = re.sub(rf'<{tag}[^>]*>(.*?)</{tag}>', r'\1', md, flags=re.DOTALL)
    # file → [filename](url)；有 token 则构造真实链接，否则退化为附件名
    def _file_tag(m: re.Match) -> str:
        tag = m.group(0)
        name_m = re.search(r'name="([^"]*)"', tag)
        token_m = re.search(r'token="([^"]*)"', tag)
        name = name_m.group(1) if name_m else "附件"
        if token_m:
            return f'[{name}](https://your-tenant.larksuite.com/file/{token_m.group(1)})'
        return f'[附件: {name}]'
    md = re.sub(r'<file[^/]*/>', _file_tag, md)
    # mention-user → 保留为 [HR联系人] 占位，避免答案语义断裂
    md = re.sub(r'<mention-user[^/]*/>', '[HR联系人]', md)
    # image / block-ref / sheet / whiteboard → 删除
    for pat in (r'<image[^/]*/>', r'<block-ref[^/]*/>', r'<sheet[^/]*/>', r'<whiteboard[^/]*/>'):
        md = re.sub(pat, '', md)
    # sub-page-list / sub-page → 删除（内容由 _fetch_recursive 单独拉取并追加）
    md = re.sub(r'<sub-page-list[^>]*>.*?</sub-page-list>', '', md, flags=re.DOTALL)
    md = re.sub(r'<sub-page\b[^/]*/>', '', md)
    # 去掉 Lark 行内属性 {align=...} {color=...} 等
    md = re.sub(r'\s*\{[a-zA-Z][^}]*\}', '', md)
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


def fetch_lark_doc(url: str) -> Optional[dict]:
    """使用 lark-cli 拉取文档（Lark markdown 格式，含表格），失败返回 None。"""
    if "/wiki/" not in url and "/docx/" not in url:
        log.error("无法识别的 Lark URL 格式: %s", url)
        return None
    cmd = ["lark-cli", "docs", "+fetch", "--doc", url, "--as", "user"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("lark-cli 调用失败 (%s): %s", url, result.stderr.strip())
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.error("lark-cli 超时: %s", url)
        return None
    except json.JSONDecodeError as e:
        log.error("lark-cli 返回非 JSON 内容 (%s): %s", url, e)
        return None
    except FileNotFoundError:
        log.error("lark-cli 未找到，请确认已安装并在 PATH 中")
        return None


def extract_subpage_tokens(lark_md: str) -> list:
    """从 Lark markdown 中提取所有 <mention-doc token="..." type="...">Title</mention-doc> 引用。
    返回 [(token, title, doc_type)] 列表，去重，保持顺序。
    """
    seen = set()
    pairs = []
    for m in re.finditer(
        r'<mention-doc\s+token="([^"]+)"\s+type="([^"]+)"[^>]*>(.*?)</mention-doc>',
        lark_md, re.DOTALL
    ):
        token, doc_type, title = m.group(1), m.group(2), m.group(3).strip()
        if token not in seen:
            seen.add(token)
            pairs.append((token, title, doc_type))
    return pairs


def fetch_lark_doc_ex(url: str) -> Tuple[Optional[dict], Optional[str]]:
    """拉取 Lark 文档，返回 (result_dict, error_reason)，成功时 error_reason 为 None。"""
    if "/wiki/" not in url and "/docx/" not in url:
        return None, f"无法识别的 URL 格式: {url}"
    cmd = ["lark-cli", "docs", "+fetch", "--doc", url, "--as", "user"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            try:
                ej = json.loads(result.stdout)
                code = ej.get("code") or (ej.get("error") or {}).get("code", "")
                msg = ej.get("msg") or (ej.get("error") or {}).get("message", "")
                if code:
                    err = f"错误码 {code}: {msg}"
            except Exception:
                pass
            return None, err or "未知错误"
        return json.loads(result.stdout), None
    except subprocess.TimeoutExpired:
        return None, "请求超时 (30s)"
    except json.JSONDecodeError as e:
        return None, f"返回内容非 JSON: {e}"
    except FileNotFoundError:
        return None, "lark-cli 未找到"


def _fetch_recursive(
    url: str,
    lark_base: str,
    visited: set,
    failures: list,
    parent_title: str = "",
    depth: int = 0,
    max_depth: int = 8,
) -> Optional[str]:
    """
    递归拉取 url 及其所有子页面，返回合并后的 cleaned markdown。
    拉取失败的页面以 dict 形式追加到 failures 列表。
    """
    if depth > max_depth:
        failures.append({
            "url": url, "parent": parent_title, "title": url,
            "reason": f"超过最大递归深度 ({max_depth} 层)",
        })
        return None

    # 用 token 去重，避免循环引用
    token_m = re.search(r'/(?:wiki|docx)/([A-Za-z0-9]+)', url)
    token = token_m.group(1) if token_m else url
    if token in visited:
        return None
    visited.add(token)

    result, err = fetch_lark_doc_ex(url)
    if result is None:
        failures.append({"url": url, "parent": parent_title, "title": url, "reason": err})
        return None

    _, raw_md = get_revision_and_content(result)
    if not raw_md:
        failures.append({"url": url, "parent": parent_title, "title": url, "reason": "内容为空"})
        return None

    # 在清理前提取内嵌文档引用（清理会把 mention-doc 标签变成纯文字）
    subpages = extract_subpage_tokens(raw_md)
    this_md = lark_markdown_to_clean(raw_md)
    parts = [this_md]

    for sub_token, sub_title, sub_type in subpages:
        if sub_type == "sheet":
            log.info("  %s↳ 内嵌表格: %s (sheet, 跳过递归)", "  " * depth, sub_title)
            continue  # sheet 由 sync_sources.json 单独配置，不在这里递归
        sub_url = f"{lark_base}/wiki/{sub_token}" if sub_type == "wiki" else f"{lark_base}/docx/{sub_token}"
        log.info("  %s↳ 内嵌文档: %s [%s]", "  " * depth, sub_title, sub_type)
        import time as _time; _time.sleep(0.8)   # 限速，避免 429
        sub_md = _fetch_recursive(
            sub_url, lark_base, visited, failures,
            parent_title=sub_title, depth=depth + 1, max_depth=max_depth,
        )
        if sub_md:
            heading = "#" * min(depth + 2, 6)
            parts.append(f"\n{heading} {sub_title}\n\n{sub_md}")

    return "\n\n".join(p for p in parts if p)


def get_revision_and_content(fetch_result: dict) -> Tuple[Optional[str], Optional[str]]:
    """从 lark-cli 返回结果中提取 revision_id（内容 hash）和 markdown 内容。"""
    try:
        data = fetch_result.get("data", {})
        md = data.get("markdown", "")
        if not md:
            return None, None
        revision_id = hashlib.md5(md.encode()).hexdigest()
        return revision_id, md
    except (KeyError, TypeError) as e:
        log.error("解析 lark-cli 返回结果失败: %s", e)
        return None, None


def fetch_lark_sheet(spreadsheet_token: str) -> Optional[dict]:
    """使用 lark-cli sheets +read 拉取电子表格数据。"""
    cmd = ["lark-cli", "sheets", "+read",
           "--spreadsheet-token", spreadsheet_token, "--as", "user"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("lark-cli sheets +read 失败 (%s): %s", spreadsheet_token, result.stderr.strip())
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.error("lark-cli sheets +read 超时: %s", spreadsheet_token)
        return None
    except json.JSONDecodeError as e:
        log.error("lark-cli sheets +read 返回非 JSON (%s): %s", spreadsheet_token, e)
        return None
    except FileNotFoundError:
        log.error("lark-cli 未找到，请确认已安装并在 PATH 中")
        return None


def get_sheet_revision_and_content(fetch_result: dict) -> Tuple[Optional[int], Optional[str]]:
    """从 sheets +read 返回结果中提取 revision 和 Markdown 表格内容。"""
    try:
        data = fetch_result["data"]
        revision = data.get("revision")
        values = data.get("valueRange", {}).get("values", [])
        content = sheet_to_markdown(values)
        return revision, content
    except (KeyError, TypeError) as e:
        log.error("解析 sheets +read 返回结果失败: %s", e)
        return None, None


def fetch_lark_bitable(base_token: str, table_id: str) -> Optional[str]:
    """使用 lark-cli base +record-list 拉取多维表格，直接返回 Markdown 文本。"""
    cmd = ["lark-cli", "base", "+record-list",
           "--base-token", base_token,
           "--table-id", table_id,
           "--limit", "200",
           "--format", "markdown",
           "--as", "user"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("lark-cli base +record-list 失败 (%s/%s): %s",
                      base_token, table_id, result.stderr.strip())
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.error("lark-cli base +record-list 超时: %s/%s", base_token, table_id)
        return None
    except FileNotFoundError:
        log.error("lark-cli 未找到，请确认已安装并在 PATH 中")
        return None


# ── 状态持久化 ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    SYNC_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 本地文件更新 ───────────────────────────────────────────────────────────────

def update_kb_section(kb_path: Path, section_marker: Optional[str], new_content: str) -> bool:
    """
    更新 KB 文件中指定 section 的内容。
    - section_marker 为 None 时：覆盖整个文件
    - section_marker 不为 None 时：找到 section_marker 所在行，
      替换到下一个同级 --- 分隔符或文件末尾
    返回是否实际发生了变化。
    """
    if not kb_path.exists():
        # 新建文件时也要写入 marker，否则下次 sync 找不到该 section
        content = (section_marker + "\n\n" + new_content) if section_marker else new_content
        kb_path.write_text(content, encoding="utf-8")
        return True

    old_content = kb_path.read_text(encoding="utf-8")

    if section_marker is None:
        if old_content == new_content:
            return False
        kb_path.write_text(new_content, encoding="utf-8")
        return True

    # Find the section by marker
    lines = old_content.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip() == section_marker.strip():
            start_idx = i
            break

    if start_idx is None:
        log.warning("未找到 section_marker '%s'，追加到文件末尾", section_marker)
        # Prepend section_marker so future syncs can find and replace this section
        section_content = section_marker + "\n\n" + new_content
        new_full = old_content.rstrip() + "\n\n---\n\n" + section_content
        if new_full == old_content:
            return False
        kb_path.write_text(new_full, encoding="utf-8")
        return True

    # Find the end of this section (next "---" separator or EOF)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    # Build new content (keep the marker line so future syncs can find it again)
    before = lines[:start_idx]
    after = lines[end_idx:]

    new_section_lines = new_content.split("\n")
    rebuilt = "\n".join(before + [lines[start_idx]] + new_section_lines + after)

    if rebuilt == old_content:
        return False

    kb_path.write_text(rebuilt, encoding="utf-8")
    return True


# ── 删除确认 & 同步后清理 ────────────────────────────────────────────────────────

def _confirm_deleted(url: str) -> bool:
    """用 wiki +node-get 确认 URL 真正被删除（code 131005 not found by token），而非临时故障。"""
    try:
        result = subprocess.run(
            ["lark-cli", "wiki", "+node-get", "--node-token", url, "--as", "user"],
            capture_output=True, text=True, timeout=20
        )
        raw = (result.stderr or result.stdout).strip()
        js = raw[raw.find("{"):] if "{" in raw else ""
        if not js:
            return False
        data = json.loads(js)
        if data.get("ok"):
            return False
        err = data.get("error", {})
        code = err.get("code", 0)
        msg  = err.get("message", "")
        return code == 131005 and "not found" in msg.lower() and "not in wiki" not in msg
    except Exception:
        return False


def _remove_source_from_config(config: dict, url: str) -> tuple[list[str], list[str]]:
    """从 config 中删除 url 对应的 source 条目，返回 (被清空的 local_file 列表, 有 section_marker 的文件列表)."""
    empty_files = []
    section_files = []
    for entry in config.get("kb_files", []):
        matched = [s for s in entry.get("sources", []) if s.get("url") == url]
        if not matched:
            continue
        for s in matched:
            if s.get("section_marker"):
                section_files.append((entry["local_file"], s["section_marker"]))
        entry["sources"] = [s for s in entry.get("sources", []) if s.get("url") != url]
        if not entry["sources"]:
            empty_files.append(entry["local_file"])
    config["kb_files"] = [e for e in config["kb_files"] if e.get("sources")]
    return empty_files, section_files


def _notify_sync_result(changed_files: set[str], total_changed: int) -> None:
    """同步完成后，通过 lark-cli 向管理员发送变更通知 DM。"""
    now = datetime.datetime.now().strftime("%m/%d %H:%M")
    files_list = "\n".join(f"  · {f}" for f in sorted(changed_files))
    text = (
        f"📚 KB 同步完成 ({now})\n"
        f"更新了 {total_changed} 条来源，涉及 {len(changed_files)} 个文件：\n"
        f"{files_list}\n\n"
        f"索引已自动重建 ✓"
    )
    try:
        result = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--user-id", os.environ.get("HR_ADMIN_OPEN_ID", ""),
             "--text", text,
             "--as", "bot"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("同步通知已发送至管理员")
        else:
            log.warning("同步通知发送失败: %s", result.stderr.strip()[:200])
    except Exception as e:
        log.warning("同步通知发送异常: %s", e)


def _run_dead_link_cleanup(dry_run: bool, fix: bool = False) -> None:
    """扫描 KB 内联链接 + kb_url_map 推送链接，检测死链。
    - 始终写入 bot.py 黑名单(--update-bot)：非破坏、可逆，确保 bot 不再推失效链接。
    - fix=True 时才 --fix：从 KB .md 删除死链行（仅在内容有变更/强制同步时执行）。"""
    script = SCRIPT_DIR / "check_dead_links.py"
    if not script.exists():
        log.warning("check_dead_links.py 不存在，跳过死链清理")
        return
    cmd = [sys.executable, str(script)]
    if not dry_run:
        cmd += ["--update-bot"]
        if fix:
            cmd += ["--fix"]
    log.info("运行死链检测: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPT_DIR))
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info("  [dead-links] %s", line)
    except Exception as e:
        log.warning("死链清理失败: %s", e)


def _refresh_url_map(config: dict) -> None:
    """从 sync_sources.json 刷新 kb_url_map.json，保留已有标签（含手写条目）。"""
    url_map_path = SCRIPT_DIR / "kb_url_map.json"
    import json as _json
    old_map: dict = {}
    if url_map_path.exists():
        try:
            old_map = _json.loads(url_map_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 从旧映射开始，保留手写条目（不在 sync_sources.json 中的 KB 文件）
    new_map: dict = dict(old_map)
    for entry in config.get("kb_files", []):
        fname = entry["local_file"]
        # sync_sources 中的条目优先覆盖（内容来源可能已变）
        for src in entry.get("sources", []):
            if src["doc_type"] in ("wiki", "docx", "sheet"):
                label = src.get("notes", "").split(" / ")[0].strip() or fname.replace(".md", "")
                new_map[fname] = {"url": src["url"], "label": label}
                break
    url_map_path.write_text(_json.dumps(new_map, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("kb_url_map.json 已刷新：%d 项", len(new_map))


def _discover_new_pages(config: dict, kb_dir: Path) -> list[dict]:
    """扫描 KB markdown 文件中的飞书 wiki/docx 链接，找出尚未在 sync_sources.json 中的潜在新页面。"""
    import re as _re
    known_urls = {
        s["url"]
        for e in config.get("kb_files", [])
        for s in e.get("sources", [])
    }
    url_pat = _re.compile(r'\[([^\]\n]{1,80})\]\((https://[^\s)]+(?:wiki|docx)[^\s)]+)\)')
    found: dict[str, str] = {}  # url → label
    for md in sorted(kb_dir.glob("*.md")):
        for line in md.read_text(errors="replace").splitlines():
            for m in url_pat.finditer(line):
                label, url = m.group(1).strip(), m.group(2).strip()
                if url not in known_urls and url not in found:
                    found[url] = label
    return [{"url": u, "label": l} for u, l in found.items()]


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def run_sync(dry_run: bool = False, force: bool = False) -> None:
    if not SYNC_SOURCES.exists():
        log.error("sync_sources.json 不存在: %s", SYNC_SOURCES)
        sys.exit(1)

    config = json.loads(SYNC_SOURCES.read_text(encoding="utf-8"))
    kb_dir = SCRIPT_DIR / config.get("kb_dir", "kb")
    state = load_state()

    total_checked = 0
    total_changed = 0
    any_kb_changed = False
    changed_files: set[str] = set()
    all_failures: list = []

    for kb_entry in config.get("kb_files", []):
        local_file = kb_entry["local_file"]
        kb_path = kb_dir / local_file
        sources = kb_entry.get("sources", [])

        for source in sources:
            url = source["url"]
            section_marker = source.get("section_marker")
            notes = source.get("notes", "")
            total_checked += 1

            state_key = url
            stored_rev = state.get(state_key, {}).get("revision_id") if not force else None

            doc_type = source.get("doc_type", "docx")
            log.info("检查: %s [%s] (type=%s)", local_file, notes or url, doc_type)

            if doc_type == "sheet":
                spreadsheet_token = source.get("spreadsheet_token") or _extract_sheet_token(url)
                if not spreadsheet_token:
                    log.error("  → sheet 类型缺少 spreadsheet_token，跳过: %s", url)
                    continue
                fetch_result = fetch_lark_sheet(spreadsheet_token)
                if fetch_result is None:
                    log.warning("  → 拉取失败，跳过（sheet 无法确认删除，保留原文件）")
                    continue
                revision_id, md_content = get_sheet_revision_and_content(fetch_result)
                if revision_id is None:
                    log.warning("  → 无法获取 revision，跳过")
                    continue

            elif doc_type == "bitable":
                base_token = source.get("base_token") or _extract_base_token(url)
                table_id = source.get("table_id", "")
                if not base_token or not table_id:
                    log.error("  → bitable 类型缺少 base_token 或 table_id，跳过: %s", url)
                    continue
                md_content = fetch_lark_bitable(base_token, table_id)
                if md_content is None:
                    log.warning("  → 拉取失败，跳过")
                    continue
                revision_id = hashlib.md5(md_content.encode()).hexdigest()

            else:
                base_m = re.match(r'(https://[^/]+)', url)
                lark_base = base_m.group(1) if base_m else "https://your-tenant.larksuite.com"
                page_failures: list = []
                visited_tokens: set = set()
                md_content = _fetch_recursive(
                    url, lark_base, visited_tokens, page_failures,
                    parent_title=notes or local_file,
                )
                all_failures.extend(page_failures)
                if md_content is None:
                    # 确认是否真正删除（避免因网络抖动误删）
                    if _confirm_deleted(url):
                        log.warning("  → 页面已删除（wiki +node-get 131005），清理 KB")
                        empty_files, section_files = _remove_source_from_config(config, url)
                        if not dry_run:
                            for fname in empty_files:
                                fpath = kb_dir / fname
                                if fpath.exists():
                                    fpath.unlink()
                                    log.info("  → 已删除 KB 文件: %s", fname)
                                    any_kb_changed = True
                            for fname, marker in section_files:
                                fpath = kb_dir / fname
                                if fpath.exists():
                                    update_kb_section(fpath, marker, "")
                                    log.info("  → 已清空 section '%s' in %s", marker, fname)
                                    any_kb_changed = True
                            state.pop(url, None)
                            SYNC_SOURCES.write_text(
                                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                        else:
                            log.info("  → [DRY-RUN] 将删除: %s / sections: %s", empty_files, section_files)
                    else:
                        log.warning("  → 拉取失败（可能是网络/权限问题），保留原文件")
                    continue
                revision_id = hashlib.md5(md_content.encode()).hexdigest()

            if not force and stored_rev == revision_id:
                log.info("  → 未变化 (rev %s)", revision_id)
                continue

            log.info("  → 发现更新: rev %s → %s", stored_rev, revision_id)
            total_changed += 1

            if not md_content:
                log.warning("  → 内容为空，跳过更新")
                continue

            if dry_run:
                log.info("  → [DRY-RUN] 将更新 %s（section: %s）", local_file, section_marker or "整个文件")
                log.info("  → [DRY-RUN] 内容预览（前200字）：%s", md_content[:200])
                continue

            changed = update_kb_section(kb_path, section_marker, md_content)
            if changed:
                any_kb_changed = True
                changed_files.add(local_file)
                log.info("  → 已写入 %s", kb_path)
            else:
                log.info("  → 内容无实质变化，跳过写入")

            # Update state
            state[state_key] = {
                "revision_id": revision_id,
                "last_synced": datetime.datetime.now().isoformat(timespec="seconds"),
                "local_file": local_file,
            }

    if not dry_run:
        save_state(state)

    log.info("同步完成：检查 %d 条，发现变化 %d 条", total_checked, total_changed)

    # 同步后通知管理员（有变更时才发）
    if changed_files and not dry_run:
        _notify_sync_result(changed_files, total_changed)

    # 死链检测：每次同步都刷新 bot 推送黑名单（非破坏，防止推送已失效页面链接）；
    # 仅当内容有变更/强制同步时，才顺带从 KB .md 删除死链行(--fix)。
    _run_dead_link_cleanup(dry_run, fix=(any_kb_changed or force))

    # 同步后刷新 kb_url_map.json（keep existing labels）
    if not dry_run:
        _refresh_url_map(config)

    # 发现潜在新页面（KB 内出现但不在 sync_sources.json 的 wiki/docx 链接）
    new_pages = _discover_new_pages(config, kb_dir)
    if new_pages:
        sep = "━" * 60
        print(f"\n{sep}")
        print(f"发现 {len(new_pages)} 个潜在新页面（未在同步列表中，可能需要添加到知识库）")
        print(sep)
        for p in new_pages:
            print(f"  [{p['label']}]")
            print(f"   {p['url']}")
        print(f"{sep}\n")

    # 持久化潜在新页面清单，供 kb_healer.py 每日匹配未解答问题时读取
    # （只在真实同步时写入，dry-run 保持零副作用）
    if not dry_run:
        (SCRIPT_DIR / "potential_new_pages.json").write_text(
            json.dumps(new_pages, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 子页面拉取失败报告
    if all_failures:
        sep = "━" * 60
        print(f"\n{sep}")
        print(f"子页面拉取失败报告（共 {len(all_failures)} 条）")
        print(sep)
        for i, f in enumerate(all_failures, 1):
            print(f"[{i}] 上级页面: {f.get('parent', '—')}")
            print(f"    标题/URL: {f.get('title', f.get('url', ''))}")
            print(f"    链接:     {f.get('url', '')}")
            print(f"    原因:     {f.get('reason', '未知')}")
        print(sep + "\n")
    else:
        log.info("所有子页面均拉取成功，无失败记录。")

    # Rebuild index if any KB file changed
    if any_kb_changed and not dry_run:
        log.info("重建 KB 索引...")
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "index_manager.py")],
                capture_output=True, text=True, cwd=str(SCRIPT_DIR)
            )
            if result.returncode == 0:
                log.info("KB 索引重建完成: %s", result.stdout.strip())
            else:
                log.error("KB 索引重建失败: %s", result.stderr.strip())
        except Exception as e:
            log.error("无法调用 index_manager.py: %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同步 Lark 知识库到本地 KB 文件")
    parser.add_argument("--dry-run", action="store_true", help="检查变化但不写入文件")
    parser.add_argument("--force", action="store_true", help="强制重新拉取所有源，忽略 revision_id 缓存")
    parser.add_argument("--discover", action="store_true", help="只扫描 KB 发现潜在新页面，不执行同步")
    args = parser.parse_args()

    if args.discover:
        config = json.loads(SYNC_SOURCES.read_text(encoding="utf-8"))
        kb_dir = SCRIPT_DIR / config.get("kb_dir", "kb")
        new_pages = _discover_new_pages(config, kb_dir)
        if new_pages:
            print(f"发现 {len(new_pages)} 个潜在新页面：")
            for p in new_pages:
                print(f"  [{p['label']}]  {p['url']}")
        else:
            print("未发现新页面（所有链接均已在同步列表中）")
    else:
        run_sync(dry_run=args.dry_run, force=args.force)
