#!/usr/bin/env python3
"""
扫描 KB 目录下所有 .md 文件里的飞书链接，用 lark-cli wiki +node-get 并发检测哪些已失效。

用法：
  python3 check_dead_links.py              # 扫描，只列出死链
  python3 check_dead_links.py --fix        # 自动从 KB 文件删除死链行
  python3 check_dead_links.py --update-bot # 同时把死链 URL 写入 bot.py 的 _DEAD_URLS
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

KB_DIR = Path(__file__).parent / "kb"
BOT_PY  = Path(__file__).parent / "bot.py"
URL_MAP = Path(__file__).parent / "kb_url_map.json"

_URL_RE = re.compile(
    r'\[([^\]\n]{0,80})\]\((https://[a-zA-Z0-9._-]+\.larksuite\.com/[^\s)]+)\)'
)


def extract_links(kb_dir: Path) -> dict[str, list[tuple[str, str, int]]]:
    """返回 {url: [(file, label, lineno), ...]}"""
    url_map: dict[str, list] = {}
    for md in sorted(kb_dir.glob("*.md")):
        for i, line in enumerate(md.read_text(errors="replace").splitlines(), 1):
            for m in _URL_RE.finditer(line):
                label, url = m.group(1).strip(), m.group(2).strip()
                url_map.setdefault(url, []).append((md.name, label, i))
    return url_map


def extract_url_map_links(path: Path) -> dict[str, tuple[str, str]]:
    """返回 kb_url_map.json 里的推送链接 {url: (label, 对应KB文件名)}。
    这些是 bot 实际推给用户的文档级链接，必须一起做死链检测。"""
    if not path.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    data = json.loads(path.read_text())
    for fname, meta in data.items():
        url = (meta or {}).get("url", "").strip()
        if url.startswith("https://") and "larksuite.com" in url:
            out[url] = (meta.get("label", fname), fname)
    return out


def check_url(url: str, retries: int = 3) -> tuple[str, bool, str]:
    """检测链接是否存在，返回 (url, is_alive, title_or_reason)。

    并发下 lark-cli 偶发限流/瞬时错误，会落到"保守判活"分支——这会漏掉真正的死链。
    因此对"不确定"结果（reason 以 [skip 开头）重试若干次；只要拿到一次确定的
    存活/删除结论就立即返回，全部重试仍不确定才按判活处理。"""
    import time as _t
    last = (url, True, "[skip, no attempt]")
    for attempt in range(retries):
        last = _check_url_once(url)
        if not last[2].startswith("[skip"):
            return last          # 确定结论（有标题 / not in wiki / 131005 not found）
        if attempt < retries - 1:
            # 429 限流需要更长退避；其它瞬时错误短退避即可
            backoff = (3.0 * (attempt + 1)) if "429" in last[2] else (1.0 + attempt)
            _t.sleep(backoff)
    return last


def _check_url_once(url: str) -> tuple[str, bool, str]:
    """单次检测：用 lark-cli wiki +node-get。"""
    try:
        result = subprocess.run(
            ["lark-cli", "wiki", "+node-get", "--node-token", url, "--as", "user"],
            capture_output=True, text=True, timeout=15
        )
        # lark-cli 把 JSON 输出到 stderr（进度行也在 stderr），stdout 为空
        raw = result.stderr.strip() or result.stdout.strip()
        json_start = raw.find("{")
        if json_start < 0:
            return url, True, "[skip, no json]"
        out = raw[json_start:]
        try:
            data = json.loads(out)
            if data.get("ok"):
                title = data.get("data", {}).get("node", {}).get("title", "")
                return url, True, title
            err  = data.get("error", {})
            code = err.get("code", 0)
            msg  = err.get("message", "unknown")
            # 131005 有两种含义：
            #   "not found by token ..."   → 真正删除，dead
            #   "not in wiki"              → 文件存在但不是 wiki 节点，alive
            if code == 131005 and "not in wiki" in msg:
                return url, True, "[file/sheet, not wiki node]"
            if code == 131005 and "not found" in msg.lower():
                return url, False, msg[:100]
            # 其他错误（权限等）保守处理为 alive，不误删
            return url, True, f"[skip, err {code}]"
        except json.JSONDecodeError:
            return url, True, "[skip, non-json]"
    except subprocess.TimeoutExpired:
        return url, True, "[skip, timeout]"
    except Exception as e:
        return url, True, f"[skip, {e}]"


def remove_link_from_file(filepath: Path, url: str) -> int:
    lines = filepath.read_text(errors="replace").splitlines(keepends=True)
    new_lines = [l for l in lines if url not in l]
    removed = len(lines) - len(new_lines)
    if removed:
        filepath.write_text("".join(new_lines))
    return removed


def update_bot_dead_urls(bot_py: Path, dead_urls: list[str]) -> None:
    text = bot_py.read_text()
    m = re.search(r'_DEAD_URLS:\s*set\[str\]\s*=\s*\{([^}]*)\}', text, re.DOTALL)
    if not m:
        print("  [warn] 在 bot.py 里找不到 _DEAD_URLS，请手动添加")
        return
    existing = set(re.findall(r'"(https://[^"]+)"', m.group(1)))
    new_urls = [u for u in dead_urls if u not in existing]
    if not new_urls:
        print("  所有死链已在 _DEAD_URLS 中，无需更新")
        return
    entries = m.group(1).rstrip()
    if entries and not entries.endswith(","):
        entries += ","
    for u in new_urls:
        entries += f'\n    "{u}",'
    new_block = f'_DEAD_URLS: set[str] = {{{entries}\n}}'
    text = text[:m.start()] + new_block + text[m.end():]
    bot_py.write_text(text)
    print(f"  已将 {len(new_urls)} 条死链加入 bot.py _DEAD_URLS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="自动从 KB 文件删除死链行")
    parser.add_argument("--update-bot", action="store_true", help="把死链写入 bot.py _DEAD_URLS")
    parser.add_argument("--update-json", action="store_true", help="把死链写入 dead_links.json（bot 启动时自动加载）")
    parser.add_argument("--workers", type=int, default=1,
                        help="并发线程数（默认 1 串行：并发会让 lark-cli 偶发瞬时错误把真死链误判为存活，"
                             "漏检死链后果比慢更严重；92 条链接串行约 2-3 分钟，3x/天同步完全够用）")
    args = parser.parse_args()

    print(f"扫描 {KB_DIR} 与 {URL_MAP.name} ...")
    url_map = extract_links(KB_DIR)          # KB .md 内联链接 {url: [(file,label,lineno)]}
    map_links = extract_url_map_links(URL_MAP)  # 推送用文档级链接 {url: (label, kbfile)}
    all_urls = set(url_map) | set(map_links)
    total = len(all_urls)
    print(f"找到 {total} 个唯一链接"
          f"（KB内联 {len(url_map)} / 推送映射 {len(map_links)}），"
          f"开始并发检测（workers={args.workers}）...\n")

    dead: list[tuple[str, str]] = []
    alive_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(check_url, url): url for url in all_urls}
        done = 0
        for fut in as_completed(futures):
            url, is_alive, info = fut.result()
            done += 1
            if is_alive:
                alive_count += 1
            else:
                dead.append((url, info))
            if done % 10 == 0 or done == total:
                print(f"  进度: {done}/{total}  死链: {len(dead)}")

    print(f"\n{'='*60}")
    print(f"检测完成：正常/跳过 {alive_count} / 死链 {len(dead)} / 共 {total}")
    print(f"{'='*60}")

    if not dead:
        print("未发现死链！")
        return

    print("\n【死链列表（页面不存在）】")
    for url, reason in sorted(dead):
        print(f"\n  {url}")
        for fname, label, lineno in url_map.get(url, []):
            print(f"    [{label}]  ← {fname}:{lineno}  (KB内联)")
        if url in map_links:
            label, kbfile = map_links[url]
            print(f"    [{label[:40]}]  ← kb_url_map.json → {kbfile}  (推送映射)")

    dead_urls = [u for u, _ in dead]
    dead_map_only = [u for u in dead_urls if u not in url_map]
    if dead_map_only:
        print(f"\n  ⚠️ 其中 {len(dead_map_only)} 条来自推送映射(kb_url_map.json)——"
              f"bot 会把这些链接推给用户，加 --update-bot 拉黑后即停止推送。"
              f"映射条目与对应 KB 文件的去留请人工确认，本脚本不自动删除。")

    if args.fix:
        print("\n【自动修复：从 KB 文件删除死链行（仅 KB 内联，不动 kb_url_map/文件）】")
        file_urls: dict[str, list[str]] = {}
        for url in dead_urls:
            for fname, _, _ in url_map.get(url, []):
                file_urls.setdefault(fname, []).append(url)
        for fname, urls in file_urls.items():
            fpath = KB_DIR / fname
            total_removed = 0
            for url in urls:
                total_removed += remove_link_from_file(fpath, url)
            print(f"  {fname}: 删除 {total_removed} 行")

    if args.update_bot:
        print("\n【更新 bot.py _DEAD_URLS】")
        update_bot_dead_urls(BOT_PY, dead_urls)

    if args.update_json:
        json_path = BOT_PY.parent / "dead_links.json"
        json_path.write_text(json.dumps(dead_urls, ensure_ascii=False, indent=2))
        print(f"\n【写入 dead_links.json】{len(dead_urls)} 条死链 → {json_path}")

    if not args.fix and not args.update_bot and not args.update_json:
        print("\n提示: 加 --fix 自动从 KB 删除死链，加 --update-bot 写入 bot.py 黑名单")


if __name__ == "__main__":
    main()
