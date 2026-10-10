#!/usr/bin/env python3
"""
KB Eval Framework — 三阶段评测
  Phase 1: 从 KB sections 自动生成 (question, key_facts) 测例
  Phase 2: 对每题跑 bot (kb.search + llm.answer)，记录回答和召回 chunks
  Phase 3: LLM-as-judge 评 faithfulness + completeness；code-based 评 link 准确性

用法:
  python3 eval_kb.py                  # 全量（~1394 sections，自动过滤短/无用的）
  python3 eval_kb.py --files 员工手册.md 考勤制度.md
  python3 eval_kb.py --limit 50       # 只取前 N 个 section（调试用）
  python3 eval_kb.py --phase 2        # 跳过 phase 1，从已有测例文件继续
  python3 eval_kb.py --phase 3        # 跳过 phase 1+2，只重跑评分

输出:
  eval_results/cases.json    — 生成的测例
  eval_results/runs.json     — bot 回答
  eval_results/grades.json   — 评分结果
  eval_results/report.txt    — 人可读报告
"""

import argparse, json, os, re, sys, time, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

# ── LLM 客户端（复用 bot 的配置） ─────────────────────────────────────────────
try:
    _env = json.loads(open(os.path.expanduser("~/.claude/settings.json")).read()).get("env", {})
except Exception:
    _env = {}

from openai import OpenAI
_llm = OpenAI(
    base_url=_env.get("LLM_BASE_URL", "https://your-llm-gateway.example.com") + "/v1",
    api_key=_env.get("ANTHROPIC_AUTH_TOKEN", ""),
)
LLM_MODEL = _env.get("ANTHROPIC_MODEL", "claude-sonnet")

OUT_DIR = Path("eval_results")
OUT_DIR.mkdir(exist_ok=True)

# ── 过滤规则：跳过无实质内容的 section ────────────────────────────────────────
_SKIP_FILES = {
    "HR资源库与文档索引.md",
    "员工制度手册索引.md",
    "培训课程库.md",
    "内推岗位汇总.md",
    "部门联系人.md",
    "员工活动系列1.md",
    "员工活动系列2.md",
    "New Joiner Online Training Guide.md",
}
MIN_CONTENT_LEN = 80   # 内容太短（纯标题/导航）跳过


def _llm_call(messages: list, max_tokens: int = 800, temperature: float = 0.1) -> str:  # temperature 保留签名兼容，但部分后端只支持=1，不传
    for attempt in range(3):
        try:
            resp = _llm.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            return content.strip() if content else ""
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return ""


def _parse_json(text: str) -> object:
    """从 LLM 回复中提取 JSON，兼容被 markdown 包裹的情况。"""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ════════════════════════════════════════════════════════════════════════════════
# Phase 1 — 从 KB sections 生成测例
# ════════════════════════════════════════════════════════════════════════════════

_QGEN_PROMPT = """\
你是一名 HR 知识库测试工程师。以下是一段 HR 政策原文（来自企业内部知识库）。

请基于该原文，生成 1-3 道员工可能会在 HR 助手上提问的问题，以及对应的关键事实。

要求：
- 每道题的答案必须完全来自原文，不得需要额外推断
- 包含至少 1 个口语化表达
- key_facts 是原文中回答该问题所需的具体事实（数字、期限、条件等）
- 若该段内容太短或仅为标题/目录，返回空列表

原文来源：{source}
原文内容：
{content}

仅输出 JSON，格式：
[
  {{
    "question": "问题",
    "key_facts": ["事实1", "事实2"],
    "source_file": "{source}"
  }}
]"""


def generate_cases(sections: list[dict]) -> list[dict]:
    """批量生成测例，每 4 个 section 一组调用一次 LLM。"""
    cases = []
    batch_size = 4
    total = len(sections)

    for i in range(0, total, batch_size):
        batch = sections[i:i + batch_size]
        batch_results = []

        for sec in batch:
            content = sec["content"].strip()
            if len(content) < MIN_CONTENT_LEN:
                continue
            prompt = _QGEN_PROMPT.format(
                source=sec["source"],
                content=content[:1200],
            )
            try:
                raw = _llm_call([{"role": "user", "content": prompt}], max_tokens=600)
                parsed = _parse_json(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        item["section_content"] = content[:600]
                        batch_results.append(item)
            except Exception as e:
                print(f"  [qgen error] {sec['source']}: {e}")
            time.sleep(0.3)

        cases.extend(batch_results)
        done = min(i + batch_size, total)
        print(f"  phase1: {done}/{total} sections → {len(cases)} cases so far")

    return cases


# ════════════════════════════════════════════════════════════════════════════════
# Phase 2 — 运行 bot
# ════════════════════════════════════════════════════════════════════════════════

def run_bot(cases: list[dict], workers: int = 2) -> list[dict]:
    from kb import search
    from llm import answer as llm_answer

    lock = threading.Lock()
    runs = []
    total = len(cases)

    def _run_one(case: dict) -> dict:
        q = case["question"]
        time.sleep(1.0)  # 限速：防 LLM 网关 WAF 封 IP
        try:
            chunks = search(q)
            ans = llm_answer(q, chunks)
            # 提取 chunks 里的来源文件
            sources = []
            for c in chunks:
                m = re.search(r'\[来源:\s*(.+?)\s*\|', c)
                if m:
                    sources.append(m.group(1).strip())
            return {**case, "answer": ans, "chunks": chunks[:3], "chunk_sources": list(dict.fromkeys(sources))}
        except Exception as e:
            return {**case, "answer": f"ERROR: {e}", "chunks": [], "chunk_sources": []}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_one, c): i for i, c in enumerate(cases)}
        done = 0
        for fut in as_completed(futures):
            result = fut.result()
            with lock:
                runs.append(result)
                done += 1
                if done % 10 == 0 or done == total:
                    print(f"  phase2: {done}/{total} questions answered")

    runs.sort(key=lambda r: cases.index(next(c for c in cases if c["question"] == r["question"])))
    return runs


# ════════════════════════════════════════════════════════════════════════════════
# Phase 3 — LLM-as-judge 评分
# ════════════════════════════════════════════════════════════════════════════════

_FAITHFULNESS_PROMPT = """\
你是一名严格的 HR 知识库事实核查员。

员工问题：{question}

系统从知识库检索到的 context（这是唯一允许引用的依据）：
---
{context}
---

系统回答：
{answer}

判断任务：
1. 系统回答中，是否有任何内容超出了 context 的支撑（即 context 未提及的结论、推断、或数字）？
2. 若有，列出具体违规内容（引用回答原文）

严格标准：paraphrase / 换个说法表达 context 中的事实 = 合规。自行推断出 context 没有的结论 = 违规。

仅输出 JSON：
{{
  "faithful": true/false,
  "score": 0.0-1.0,
  "violations": ["回答中超出 context 的具体表述1", ...]
}}"""

_COMPLETENESS_PROMPT = """\
你是一名 HR 问答质量评估员。

员工问题：{question}

知识库关键事实（ground truth，来自原文）：
{key_facts}

系统回答：
{answer}

判断：系统回答是否覆盖了上述关键事实？若有遗漏，列出缺失的事实。

仅输出 JSON：
{{
  "complete": true/false,
  "score": 0.0-1.0,
  "missing": ["缺失事实1", ...]
}}"""


def grade_one(run: dict) -> dict:
    q = run["question"]
    ans = run["answer"]
    context = "\n\n".join(run["chunks"]) if run["chunks"] else "(无检索结果)"
    key_facts = run.get("key_facts", [])

    grade = {"question": q, "answer": ans[:200], "source_file": run.get("source_file", "")}

    # ── 1. Faithfulness ───────────────────────────────────────────────────────
    try:
        raw = _llm_call([{"role": "user", "content": _FAITHFULNESS_PROMPT.format(
            question=q, context=context[:5000], answer=ans
        )}], max_tokens=400)
        grade["faithfulness"] = _parse_json(raw)
    except Exception as e:
        grade["faithfulness"] = {"faithful": None, "score": None, "violations": [], "error": str(e)}

    # ── 2. Completeness ───────────────────────────────────────────────────────
    if key_facts and ans and "不在 HR" not in ans and "联系" not in ans:
        try:
            raw = _llm_call([{"role": "user", "content": _COMPLETENESS_PROMPT.format(
                question=q, key_facts="\n".join(f"- {f}" for f in key_facts), answer=ans
            )}], max_tokens=400)
            grade["completeness"] = _parse_json(raw)
        except Exception as e:
            grade["completeness"] = {"complete": None, "score": None, "missing": [], "error": str(e)}
    else:
        grade["completeness"] = {"complete": None, "score": None, "missing": [], "skipped": True}

    # ── 3. Link / Source check（code-based） ──────────────────────────────────
    expected_file = run.get("source_file", "")
    chunk_sources = run.get("chunk_sources", [])
    grade["link_ok"] = (expected_file in chunk_sources) if expected_file and chunk_sources else None

    time.sleep(1.0)  # 限速：防 LLM 网关 WAF 封 IP
    return grade


def grade_all(runs: list[dict]) -> list[dict]:
    grades = []
    total = len(runs)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(grade_one, r): i for i, r in enumerate(runs)}
        done = 0
        for fut in as_completed(futures):
            grades.append(fut.result())
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  phase3: {done}/{total} graded")
    return grades


# ════════════════════════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════════════════════════

def report(grades: list[dict]) -> str:
    total = len(grades)
    faith_fails  = [g for g in grades if g.get("faithfulness", {}).get("faithful") is False]
    compl_fails  = [g for g in grades if g.get("completeness", {}).get("complete") is False]
    link_fails   = [g for g in grades if g.get("link_ok") is False]

    lines = [
        f"{'='*60}",
        f"KB Eval Report  —  {total} questions",
        f"{'='*60}",
        f"  Faithfulness failures : {len(faith_fails)} / {total}",
        f"  Completeness failures : {len(compl_fails)} / {total}",
        f"  Link mismatch         : {len(link_fails)} / {total}",
        "",
    ]

    if faith_fails:
        lines.append("── FAITHFULNESS FAILURES（bot 超出原文下结论）────────────────")
        for g in faith_fails:
            lines.append(f"  Q: {g['question']}")
            for v in g["faithfulness"].get("violations", []):
                lines.append(f"    ✗ {v}")
            lines.append(f"  Score: {g['faithfulness'].get('score')}")
            lines.append("")

    if compl_fails:
        lines.append("── COMPLETENESS FAILURES（bot 遗漏关键事实）───────────────────")
        for g in compl_fails:
            lines.append(f"  Q: {g['question']}")
            for m in g["completeness"].get("missing", []):
                lines.append(f"    △ {m}")
            lines.append(f"  Score: {g['completeness'].get('score')}")
            lines.append("")

    if link_fails:
        lines.append("── LINK FAILURES（来源文件不匹配）────────────────────────────")
        for g in link_fails:
            lines.append(f"  Q: {g['question']}  (expected: {g['source_file']})")
        lines.append("")

    return "\n".join(lines)


def _fail_counts(grades: list[dict]) -> tuple:
    f = sum(1 for g in grades if g.get("faithfulness", {}).get("faithful") is False)
    c = sum(1 for g in grades if g.get("completeness", {}).get("complete") is False)
    l = sum(1 for g in grades if g.get("link_ok") is False)
    return f, c, l


def _compare_report(baseline: list[dict], weighted: list[dict]) -> str:
    """信用加权 A/B 对照：off（基线）vs on（加权），失败数越少越好。"""
    n = len(baseline)
    bf, bc, bl = _fail_counts(baseline)
    wf, wc, wl = _fail_counts(weighted)

    def _delta(b, w):
        d = b - w  # 正数 = 加权版失败更少 = 改善
        return f"改善 {-d:+d}" if d > 0 else (f"退化 {-d:+d}" if d < 0 else "持平")

    lines = [
        "=" * 60,
        f"A/B 对照报告 — 信用加权 off vs on（{n} 题，失败数越少越好）",
        "=" * 60,
        f"  Faithfulness 失败:  off={bf}  on={wf}   {_delta(bf, wf)}",
        f"  Completeness 失败:  off={bc}  on={wc}   {_delta(bc, wc)}",
        f"  Link 不匹配:        off={bl}  on={wl}   {_delta(bl, wl)}",
        "",
        "详细评分明细: grades-off.json / grades-on.json",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="*", help="Only eval these KB files")
    parser.add_argument("--limit", type=int, help="Max sections to process (debug)")
    parser.add_argument("--phase", type=int, default=1, help="Start from phase N (1/2/3)")
    parser.add_argument("--credit", choices=["on", "off"], default="on",
                        help="信用加权开关（单跑模式，默认 on）")
    parser.add_argument("--compare", action="store_true",
                        help="A/B 对照：信用加权 off / on 各跑一遍 Phase 2+3，输出对比报告")
    args = parser.parse_args()

    cases_f  = OUT_DIR / "cases.json"
    runs_f   = OUT_DIR / "runs.json"
    grades_f = OUT_DIR / "grades.json"
    report_f = OUT_DIR / "report.txt"

    # ── Phase 1: generate cases ───────────────────────────────────────────────
    if args.phase <= 1:
        print("\n[Phase 1] 从 KB sections 生成测例...")
        with open("docs/kb-index.json") as f:
            idx = json.load(f)

        sections = []
        for fname, fdata in idx["files"].items():
            if fname in _SKIP_FILES:
                continue
            if args.files and fname not in args.files:
                continue
            for sec in fdata.get("sections", []):
                sections.append({"source": fname, **sec})

        if args.limit:
            sections = sections[:args.limit]

        print(f"  待处理 sections: {len(sections)}")
        cases = generate_cases(sections)
        cases_f.write_text(json.dumps(cases, ensure_ascii=False, indent=2))
        print(f"  → {len(cases)} 测例保存至 {cases_f}")
    else:
        cases = json.loads(cases_f.read_text())
        print(f"[Phase 1 跳过] 加载 {len(cases)} 个已有测例")

    # ── A/B 对照模式：信用加权 off / on 各跑一遍 Phase 2+3 ────────────────────
    if args.compare:
        import credit_store
        arms = {}
        for arm in ("off", "on"):
            credit_store.ENABLED = (arm == "on")
            print(f"\n[Phase 2 · credit={arm}] 运行 bot 回答 {len(cases)} 题...")
            runs_arm = run_bot(cases)
            grades_arm = grade_all(runs_arm)
            arms[arm] = grades_arm
            (OUT_DIR / f"grades-{arm}.json").write_text(
                json.dumps(grades_arm, ensure_ascii=False, indent=2))
        txt = _compare_report(arms["off"], arms["on"])
        (OUT_DIR / "compare-report.txt").write_text(txt)
        print(f"\n{txt}")
        return

    # 单跑模式：按 --credit 设置信用加权开关
    import credit_store
    credit_store.ENABLED = (args.credit == "on")

    # ── Phase 2: run bot ──────────────────────────────────────────────────────
    if args.phase <= 2:
        print(f"\n[Phase 2] 运行 bot 回答 {len(cases)} 题...")
        runs = run_bot(cases)
        runs_f.write_text(json.dumps(runs, ensure_ascii=False, indent=2))
        print(f"  → 结果保存至 {runs_f}")
    else:
        runs = json.loads(runs_f.read_text())
        print(f"[Phase 2 跳过] 加载 {len(runs)} 条已有回答")

    # ── Phase 3: grade ────────────────────────────────────────────────────────
    print(f"\n[Phase 3] LLM-as-judge 评分 {len(runs)} 题...")
    grades = grade_all(runs)
    grades_f.write_text(json.dumps(grades, ensure_ascii=False, indent=2))
    print(f"  → 评分保存至 {grades_f}")

    txt = report(grades)
    report_f.write_text(txt)
    print(f"\n{txt}")
    print(f"完整报告: {report_f}")


if __name__ == "__main__":
    main()
