"""
KB 索引管理器。

职责：
- 维护 docs/kb-index.json：记录每个章节的标题、关键词、mtime
- 增量更新：只重扫 mtime 变化的文件，快
- 周扫 / 手动 rebuild：全量重扫，确保索引与磁盘一致
- 提供 search()：直接查索引，不读原始 MD 文件
"""

import json
import logging
import math
import re
import datetime
import tempfile
import os
from collections import defaultdict
from pathlib import Path

from config import KB_DIR, DOCS_DIR

log = logging.getLogger(__name__)

INDEX_PATH = DOCS_DIR / "kb-index.json"
MANIFEST_PATH = DOCS_DIR / "kb-manifest.md"

# ── 分词（与 kb.py 保持一致）─────────────────────────────────────────────────

_STOPWORDS = {
    "a","an","the","is","are","was","were","be","been","being",
    "i","my","me","we","you","your","it","its","this","that",
    "to","of","in","on","for","with","at","by","from","how",
    "what","when","where","can","do","does","will","would","should",
    "and","or","but","if","not","no","yes","who","which","get",
    "name","just","about","some","there","have","has",
}

# 中文停用 bigram/trigram：疑问词和虚词产生的字符片段，在查询和文档中均出现导致误匹配
# 过滤掉这些片段可避免"谁负责招聘"命中含"谁负"内容的无关文档
_CN_STOPBIGRAMS = frozenset([
    # 疑问代词 + 下一字（who/what/which）— 2字
    "谁负", "谁是", "谁的", "谁来", "谁在", "谁会", "谁能",
    "什么", "么是", "是什", "则是", "是的",
    "哪些", "有哪", "哪里", "哪个", "哪儿",
    "为什", "为啥",
    # 疑问句尾虚词
    "了吗", "是吗",
    # 3字疑问短语（trigram 级别，避免 bigram 过滤后 trigram 仍泄漏）
    "谁负责", "是什么", "什么是", "则是什", "是谁的", "是谁来",
    "负责谁", "谁来负", "么样的", "哪些人", "哪些部",
    # 词边界碎片：仅作为更长词的一部分出现，独立无意义，易造成跨文档误匹配
    "责人",   # 总是 "负责人" 的尾片段
    "聘负",   # 总是 "招聘负责" 的中间片段
    "部负",   # 表格跨行拼接副产品："客服部|负责" → "部负" 伪 bigram
    "些人",   # 总是 "哪些人" 的尾片段，查询虚词
    "招聘负", "聘负责",  # trigram 碎片
    "服部负", "部负责",  # trigram 表格跨行碎片
    "有哪些", "哪些人",  # trigram 疑问虚词
])

_CN_SYNONYMS = {
    "婚假":["marriage","wedding"],"病假":["sick","medical"],
    "产假":["maternity","哺乳","育儿"],"陪产":["paternity"],"年假":["annual"],
    "调岗":["transfer","department"],"转岗":["transfer"],
    "远程":["remote","home"],"居家":["remote","home"],"外勤":["remote"],
    "打卡":["clock","attendance"],"补卡":["clock","attendance"],
    "考勤":["attendance","clock"],"报销":["reimburse","expense"],
    "出差":["travel"],"差旅":["travel","expense"],
    "绩效":["performance","review"],"离职":["resign"],
    "转正":["confirm","probation"],"社保":["insurance","social"],
    "薪资":["salary","payroll"],"工资":["salary","payroll"],
    "奖金":["bonus"],"内推":["referral"],
    # 请假/事假类
    "请假":["假期","年假","事假","病假","leave"],
    "事假":["请假","假期","leave","personal"],
    "有事":["事假","请假","假期"],
    "家里":["事假","请假","私事"],
    "私事":["事假","请假"],
    "紧急":["事假","请假","特殊"],
    # 哺乳/育儿类
    "哺乳":["产假","母婴","育儿","maternity"],
    "乳期":["产假","育儿","哺乳"],
    "产后":["产假","哺乳","育儿"],
    "育儿":["产假","哺乳期","儿童"],
    "母婴":["哺乳","产假","育儿"],
    # 政策/规定类（仅保留直接近义词，避免过度扩展导致误匹配）
    "政策":["制度"],
    "规定":["制度"],
    "制度":["政策","规定"],
    "新人":["入职","新员工","onboard"],
    # 工作时间类（让"上班/下班"命中"工作时间规则"标题）
    "上班":["工作时","作息","工作制"],
    "下班":["工作时","作息","工作制"],
    # 考勤违规类（多词共现让员工制度手册考勤章节得分更高）
    "迟到":["旷工","早退","考勤"],
    "旷工":["迟到","早退","考勤"],
    # 加班类（指向员工制度手册加班章节中的"调休"关键词）
    "加班":["调休","overtime"],
    # How-to 查询词 → 只保留"申请"，移除"方法/流程"避免大量误匹配
    "怎么":["申请"],
    "如何":["申请"],
    "怎样":["申请"],
    "么用":["使用","规则"],
    "么办":["申请"],
    # 忘记打卡 → 补卡
    "忘了":["补卡","忘记"],
    "忘打":["补卡","打卡"],
    # 时间/跨年（让"明年"命中含"次年/延长"的假期条款）
    "明年":["次年","延长","结转"],
    "次年":["明年","延长","累积"],
    "结转":["次年","累积","延长"],
    # 福利类
    "福利":["benefit","welfare","补贴"],
    "待遇":["福利","薪资","benefit"],
    "补贴":["福利","报销","津贴"],
    "津贴":["补贴","福利"],
    # 证明/文件类（中文查询能匹配英文模板文件）
    "证明":["certificate","certify"],
    "收入":["income"],
    "在职":["employment","certificate"],
    # 薪酬类互通（工资/薪酬/薪资 跨词匹配）
    "薪酬":["salary","payroll","工资","薪资"],
    "工资":["salary","payroll","薪酬","薪资"],
    # 3字 HR 高频词（trigram 级别）
    "年终奖":["bonus","奖金","绩效奖"],"年终":["bonus","奖金"],
    "绩效奖":["bonus","奖金","年终奖"],"试用期":["probation","转正","试用"],
    "差旅费":["travel","报销","出差"],"年休假":["annual","年假","休假"],
    "事假申":["事假","请假","leave"],"内推奖":["referral","内推","bonus"],
    "固定资":["asset","固定资产","设备"],"离职流":["resign","离职","流程"],
}

_SYNONYMS = {
    "annual":["年假","年休"],"leave":["年假","病假","婚假","产假","假期","休假"],
    "carry":["次年","延长","累积"],"sick":["病假","医疗"],
    "marriage":["婚假"],"marry":["婚假"],"wedding":["婚假"],
    "maternity":["产假"],"paternity":["陪产假"],"parental":["产假","陪产假"],
    "probation":["试用期","试用"],"confirm":["转正"],"confirmation":["转正"],
    "regulariz":["转正"],"salary":["薪资","工资","薪酬"],
    "payroll":["薪资","工资"],"payday":["薪资","工资","15日"],
    "pay":["薪资","工资"],"paycheck":["薪资","工资"],
    "bonus":["奖金","绩效奖"],"performance":["绩效","考核"],
    "review":["绩效","考核"],"kpi":["绩效","考核"],
    "transfer":["调岗","转岗"],"switch":["调岗","转岗"],
    "department":["部门","调岗"],"teams":["部门"],
    "remote":["远程","居家","外勤"],"home":["居家","远程","外勤"],
    "wfh":["远程","居家"],"clock":["打卡","补卡","考勤"],
    "punch":["打卡","补卡"],"forgot":["忘记","补卡","打卡"],
    "forget":["忘记","补卡","打卡"],"attendance":["考勤","打卡"],
    "late":["迟到","考勤"],"reimburse":["报销","差旅"],
    "reimbursement":["报销","差旅"],"expense":["报销","差旅费"],
    "travel":["差旅","出差","报销"],"referral":["内推","推荐"],
    "refer":["内推"],"resign":["离职","辞职"],
    "resignation":["离职","辞职"],"quit":["离职","辞职"],
    "onboard":["入职","新员工"],"hire":["招聘","入职"],
    "insurance":["社保","保险"],"medical":["医疗","病假","社保"],
    "social":["社保"],"overtime":["加班"],
    # 常用两字母大写缩写（IT/HR/OA 等在正常 token 化时被过滤）
    "it": ["电脑","网络","技术"],
    "hr": ["人事","人力"],
    "oa": ["审批","系统"],
    "ssc": ["人事","行政","共享"],
    "ai": ["智能","人工"],
    "hrbp": ["人事","人力","hrbp","tommy","joyce"],
    # 补充：英文查询中常见但原来缺失的词
    "pto": ["年假","假期","休假","leave"],
    "wfh": ["居家","远程"],
    "ooo": ["假期","休假","外出"],
    "paid": ["薪资","工资"],
    "payslip": ["薪资","工资"],
    "terminate": ["离职","辞职"],
    "terminated": ["离职","辞职"],
    "absence": ["考勤","打卡","请假"],
    "absent": ["考勤","打卡","请假"],
    "allowance": ["津贴","补贴","福利"],
    "policy": ["制度","规定"],
    "regulation": ["制度","规定"],
    "schedule": ["作息","工作时"],
    "postpartum": ["产假","哺乳"],
    "prenatal": ["产假","产检"],
    "pregnancy": ["产假","怀孕"],
    "rollover": ["结转","次年"],
    "carryover": ["结转","次年"],
    "compensation": ["薪酬","薪资","salary"],
    "asset": ["固定资产","设备"],
    "certificate": ["证明","证书"],
    "employment": ["在职","证明"],
    "quit": ["离职","辞职"],
    "fired": ["离职","辞职"],
    "urgent": ["紧急","事假","请假"],
    "emergency": ["紧急","事假","请假"],
}


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    # URL 预处理：先去除 URL，避免 URL 片段（如 Lark 随机 token）成为无效关键词
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)  # markdown 链接保留文字部分
    # 表格感知预处理：把 markdown 表格每列分成独立行，防止跨格 bigram（如"型天""数年"）
    if '|' in text:
        lines = text.splitlines()
        processed_lines = []
        for line in lines:
            if '|' in line and not line.strip().startswith('#'):
                cells = [c.strip() for c in line.split('|')
                         if c.strip() and not re.match(r'^[-:]+$', c.strip())]
                processed_lines.append('\n'.join(cells))
            else:
                processed_lines.append(line)
        text = '\n'.join(processed_lines)
    # 大写缩写（IT/HR/OA 等）在 lower 之前提取，避免被长度过滤器拦掉
    # 用 ASCII 边界而非 \b（Python 的 \b 把中文也当 word char）
    for abbr in re.findall(r'(?<![a-zA-Z0-9])[A-Z]{2,5}(?![a-zA-Z0-9])', text):
        lw = abbr.lower()
        tokens.add(lw)
        tokens.update(_SYNONYMS.get(lw, []))
    for word in re.findall(r"[a-zA-Z]+", text.lower()):
        # 2-letter words are normally filtered, but expand them if they're known abbreviations
        if len(word) == 2 and word in _SYNONYMS:
            tokens.add(word)
            tokens.update(_SYNONYMS.get(word, []))
        elif 3 <= len(word) <= 20 and word not in _STOPWORDS:
            tokens.add(word)
            tokens.update(_SYNONYMS.get(word, []))
            # 前缀匹配：处理词根键（如 "regulariz" 匹配 "regularization"）
            for stem, expansions in _SYNONYMS.items():
                if len(stem) >= 6 and word != stem and word.startswith(stem):
                    tokens.update(expansions)
    cn_chars = re.findall(r"[一-鿿]", text)
    for i in range(len(cn_chars) - 1):
        bigram = cn_chars[i] + cn_chars[i + 1]
        if bigram in _CN_STOPBIGRAMS:
            continue
        tokens.add(bigram)
        tokens.update(_CN_SYNONYMS.get(bigram, []))
    # trigram：捕获"年终奖""试用期""差旅费"等 3 字 HR 词
    for i in range(len(cn_chars) - 2):
        trigram = cn_chars[i] + cn_chars[i + 1] + cn_chars[i + 2]
        if trigram in _CN_STOPBIGRAMS:
            continue
        tokens.add(trigram)
        tokens.update(_CN_SYNONYMS.get(trigram, []))
    return tokens


# ── 索引结构 ─────────────────────────────────────────────────────────────────
#
# kb-index.json:
# {
#   "built_at": "2026-06-08T12:00:00",
#   "files": {
#     "HR/foo.md": {
#       "mtime": 1234567890.0,
#       "sections": [
#         {"title": "年假", "keywords": ["年假","annual",...], "content": "..."}
#       ]
#     }
#   }
# }

def _load_raw() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"built_at": "", "files": {}}


_MAX_SECTION_CHARS = 1600  # 单个 section 超过此长度则按段落继续拆分


def _chunk_large_section(title: str, content: str) -> list[dict]:
    """把超长 section 按段落切成 ≤ _MAX_SECTION_CHARS 的小块，每块保留标题上下文行。"""
    if len(content) <= _MAX_SECTION_CHARS:
        return [{"title": title, "keywords": sorted(_tokenize(content)), "content": content}]

    # 提取面包屑/标题行（以 [ 或 # 开头的行）
    lines = content.splitlines()
    header_lines, body_start = [], 0
    for i, line in enumerate(lines):
        if line.startswith("[") or line.startswith("#"):
            header_lines.append(line)
            body_start = i + 1
        else:
            break

    header = "\n".join(header_lines) + "\n" if header_lines else ""
    body = "\n".join(lines[body_start:])
    max_body = max(_MAX_SECTION_CHARS - len(header), 400)

    # 在空行基础上，也按中文章节标题和数字一级条目拆分段落
    # 防止"第七章 考勤"和"2 迟到/早退"等与无关内容混在同一个 chunk
    _PARA_SPLIT = re.compile(
        r'\n{2,}'                               # 空行（原有逻辑）
        r'|(?=\n第[一二三四五六七八九十百]+章)'  # 中文章节标题：第X章
        r'|(?=\n[1-9]\d*\s+[一-鿿])',   # 数字一级条目：如"2 迟到/早退"
        re.MULTILINE,
    )
    paragraphs = [p for p in _PARA_SPLIT.split(body) if p.strip()]
    chunks, buf, prev_tail = [], "", ""
    for para in paragraphs:
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) > max_body and buf:
            chunks.append(header + buf.strip())
            # 保留上一块最后一段作为下一块的重叠前缀（≤150字，避免占用太多预算）
            buf_paras = buf.strip().split("\n\n")
            prev_tail = buf_paras[-1].strip() if len(buf_paras[-1].strip()) <= 150 else ""
            buf = (prev_tail + "\n\n" + para).strip() if prev_tail else para
        else:
            buf = candidate
    if buf.strip():
        chunks.append(header + buf.strip())

    if not chunks:
        chunks = [content[:_MAX_SECTION_CHARS]]

    return [{"title": title, "keywords": sorted(_tokenize(c)), "content": c} for c in chunks]


def _scan_file(md_path: Path) -> list[dict]:
    """把一个 MD 文件切成 section 列表，子章节携带父级标题作为上下文。"""
    text = md_path.read_text(encoding="utf-8")
    parts = re.split(r"\n(?=#{1,3} )", text)
    sections = []
    h1 = h2 = ""

    for part in parts:
        part = part.strip()
        if not part or len(part) < 20:
            continue
        # Skip stub sections: heading with no real body (e.g. "## 4. Title" followed by nothing)
        body_lines = [l for l in part.splitlines()[1:] if l.strip()]
        if not body_lines or len("".join(body_lines)) < 30:
            continue
        first_line = part.splitlines()[0]
        title = first_line.lstrip("#").strip()
        level = len(first_line) - len(first_line.lstrip("#"))

        if level == 1:
            h1, h2 = title, ""
            content = part
        elif level == 2:
            h2 = title
            content = f"[{h1}]\n{part}" if h1 else part
        else:
            crumbs = [c for c in [h1, h2] if c]
            prefix = f"[{' > '.join(crumbs)}]\n" if crumbs else ""
            content = prefix + part

        sections.extend(_chunk_large_section(title, content))

    return sections


def build_index(force: bool = False) -> dict:
    """
    增量建索引：只重扫 mtime 变化的文件。
    force=True 则全量重扫。
    使用原子写（写临时文件 → rename）防止多进程竞态损坏索引。
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_raw()
    files_index = index.get("files", {})
    changed = []        # 本次需要重扫的文件（force 模式下含全部）
    truly_changed = []  # mtime 实际变化的文件（用于判断是否需要重建向量）

    all_mds = sorted(KB_DIR.rglob("*.md"))
    log.debug("KB 扫描: %s 下找到 %d 个 .md 文件", KB_DIR, len(all_mds))

    for md in all_mds:
        # 跳过文件名或任意父目录以 _ 开头的（草稿/模板）
        if any(part.startswith("_") for part in md.relative_to(KB_DIR).parts):
            continue
        key = md.relative_to(KB_DIR).as_posix()
        mtime = md.stat().st_mtime
        mtime_unchanged = key in files_index and files_index[key]["mtime"] == mtime
        if not force and mtime_unchanged:
            continue
        changed.append((key, md, mtime))
        if not mtime_unchanged:
            truly_changed.append(key)

    # 删除已不存在的文件条目
    existing_keys = {
        md.relative_to(KB_DIR).as_posix()
        for md in all_mds
        if not any(part.startswith("_") for part in md.relative_to(KB_DIR).parts)
    }
    for k in list(files_index.keys()):
        if k not in existing_keys:
            del files_index[k]

    for key, md, mtime in changed:
        try:
            sections = _scan_file(md)
            files_index[key] = {"mtime": mtime, "sections": sections}
        except Exception as e:
            log.warning("扫描文件失败，跳过: %s (%s)", key, e)

    index["files"] = files_index
    index["idf"] = _compute_idf(files_index)
    index["built_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    # 原子写：写临时文件再重命名，防止多进程并发损坏
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DOCS_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, INDEX_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise

    if changed:
        _write_manifest(index)

    # 向量索引：仅当 KB 文件 mtime 真正变化，或向量文件不存在时才重建
    # （force=True 但文件内容未变时跳过，避免 bot 每次重启都重建向量）
    _vec_path = DOCS_DIR / "kb-vectors.npy"
    if truly_changed or not _vec_path.exists():
        try:
            import vector_store as _vs
            _vs.build(index)
        except Exception as e:
            log.warning("向量索引构建失败（不影响主功能）: %s", e)
    else:
        log.info("KB 内容未变，跳过向量重建")

    return index


def _write_manifest(index: dict) -> None:
    """更新人读版 kb-manifest.md。"""
    lines = [
        "# KB 章节清单 · Knowledge Base Manifest",
        f"\n> 最后更新：{index['built_at']}",
        "\n---\n",
    ]
    for fname, fdata in sorted(index["files"].items()):
        mtime_str = datetime.datetime.fromtimestamp(fdata["mtime"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"## {fname}  _(更新: {mtime_str})_\n")
        for s in fdata["sections"]:
            lines.append(f"- **{s['title']}**")
        lines.append("")
    MANIFEST_PATH.write_text("\n".join(lines), encoding="utf-8")


def get_index() -> dict:
    """返回当前索引（不触发重扫）。"""
    return _load_raw()


def _compute_idf(files_index: dict) -> dict:
    """
    计算所有 token 的 IDF（Inverse Document Frequency）。
    IDF(t) = log(N / (df(t) + 1)) + 1.0，N = 总章节数。
    稀有词（如"婚假"）得高分，常见词（如"申请"）得低分。
    结果存入索引 JSON，search() 直接读取，零额外延迟。
    """
    df: dict[str, int] = defaultdict(int)
    n_sections = 0
    for fdata in files_index.values():
        for s in fdata["sections"]:
            for token in s["keywords"]:
                df[token] += 1
            n_sections += 1
    n = max(n_sections, 1)
    return {t: round(math.log(n / (cnt + 1)) + 1.0, 4) for t, cnt in df.items()}


# 纯索引/链接汇总文件——不参与主内容竞争，单独作为文档链接补充
_INDEX_FILES = frozenset(["HR资源库与文档索引.md", "交易所基础培训知识库索引.md"])

# 低优先级文件——非政策/制度类，仅在主内容不足时作为兜底
# 包括：产品培训文档、运营协议类文档（它们可能命中 HR 关键词但不提供 HR 政策答案）
_TRAINING_FILES = frozenset([
    "MCN主播培训文档.md",
    "交易所MCN培训_核心必修.md",
    "交易所MCN培训_进阶速查.md",
    "远程办公通讯响应规范.md",   # 通讯协议文档，非假期/薪资政策
])

# 联系人文件——当查询包含"找谁/联系"等信号时，额外注入最佳匹配条目
_CONTACT_FILE = "部门联系人.md"
_CONTACT_SIGNALS = frozenset([
    "找谁", "联系", "谁负责", "对接", "负责人", "联系方式", "邮箱",
    "who", "contact", "email", "reach",
    # 直接说部门缩写 → 意图就是"找联系人"
    "hr", "it", "ssc", "hrbp",
    # 财务/报销类问题 → 注入 Finance 联系人（Allen.ren）
    "报销", "财务", "发票", "付款", "reimburs", "invoice", "payment",
])


def search(query: str, top_k: int = 6, min_score: int = 1) -> list[str]:
    """
    两阶段检索：
    1. 内容文件  → 关键词匹配，每文件最多 2 条，共 top_k 条
    2. 索引文件  → 附加含 📄 链接的条目（最多 2 条，不占 top_k 名额）
    若内容文件 0 命中，降级为子串匹配兜底。
    排序使用 (score DESC, mtime DESC, fname ASC, title ASC) 保证结果确定性。
    """
    index = _load_raw()
    words = _tokenize(query)
    # IDF 加权：稀有词得高分，常见词得低分，显著提升检索精准度
    idf = index.get("idf", {})

    content_scored: list[tuple] = []
    doclink_scored: list[tuple] = []
    training_scored: list[tuple] = []

    if words:
        for fname, fdata in index.get("files", {}).items():
            mtime = fdata["mtime"]
            is_index = Path(fname).name in _INDEX_FILES
            is_contact = Path(fname).name == _CONTACT_FILE
            is_training = Path(fname).name in _TRAINING_FILES
            fname_stem = Path(fname).stem.lower()
            fname_bonus = sum(4 for w in words if len(w) >= 2 and w in fname_stem)
            for s in fdata["sections"]:
                title_hay = s["title"].lower()
                kw_set = set(s["keywords"])
                # 过滤门槛：至少 1 个原始关键词命中（不用 IDF 分数，避免低频词漏掉）
                raw_hits = sum(1 for w in words if w in kw_set)
                if raw_hits < min_score:
                    continue
                # 排序分：IDF 加权，标题命中额外 3× 奖励
                score = sum(
                    (3 if w in title_hay else 1) * idf.get(w, 1.0)
                    for w in words if w in kw_set
                )
                entry = (score, fname_bonus, mtime, fname, s["title"], s["content"])
                if is_index:
                    doclink_scored.append(entry)
                elif is_training:
                    training_scored.append(entry)
                else:
                    content_scored.append(entry)

    # 子串补充：token 命中 < 3 条时补跑子串匹配，提升召回率
    # （原来只在 0 命中时才跑，现在放宽门槛，避免稀少 token 匹配漏掉真正相关的内容）
    if len(content_scored) < 3:
        cn_frags = [query[i:i+2] for i in range(len(query)-1)
                    if re.match(r'[一-鿿]{2}', query[i:i+2])]
        en_words = [w for w in re.findall(r'[a-zA-Z]{4,}', query.lower())
                    if w not in _STOPWORDS]
        frags = cn_frags + en_words
        if frags:
            existing = {e[5] for e in content_scored}  # 已有内容去重
            for fname, fdata in index.get("files", {}).items():
                mtime = fdata["mtime"]
                is_index = Path(fname).name in _INDEX_FILES
                is_training = Path(fname).name in _TRAINING_FILES
                fname_stem = Path(fname).stem.lower()
                fname_bonus = sum(4 for f in frags if len(f) >= 2 and f in fname_stem)
                for s in fdata["sections"]:
                    if s["content"] in existing:
                        continue
                    # 子串命中数 × IDF 加权（碎片无 IDF 时默认 1.0）
                    hits = [(f, idf.get(f, 1.0)) for f in frags if f in s["content"].lower()]
                    if not hits:
                        continue
                    score = sum(w for _, w in hits)
                    entry = (score, fname_bonus, mtime, fname, s["title"], s["content"])
                    if is_index:
                        doclink_scored.append(entry)
                    elif is_training:
                        training_scored.append(entry)
                    else:
                        content_scored.append(entry)

    # 稳定排序：内容匹配质量(base)降序，文件名加成(fname_bonus)降序，mtime 降序，(fname, title) 保证确定性
    # entry = (base_score, fname_bonus, mtime, fname, title, content)
    _key = lambda x: (-x[0], -x[1], -x[2], x[3], x[4])
    content_scored.sort(key=_key)
    doclink_scored.sort(key=_key)
    training_scored.sort(key=_key)

    # 文件名直接包含查询词时（fname_bonus>0），最多取 3 条；否则最多 2 条
    fname_stem_map: dict[str, int] = {}
    for entry in content_scored:
        fname = entry[3]
        if fname not in fname_stem_map:
            fname_stem = Path(fname).stem.lower()
            fname_stem_map[fname] = sum(4 for w in words if len(w) >= 2 and w in fname_stem)

    file_count: dict[str, int] = defaultdict(int)
    selected_content = []
    for entry in content_scored:
        fname = entry[3]
        per_file_limit = 3 if fname_stem_map.get(fname, 0) > 0 else 2
        if file_count[fname] < per_file_limit:
            file_count[fname] += 1
            selected_content.append(entry)
        if len(selected_content) >= top_k:
            break

    # 从索引文件取含文档链接的条目（最多 2 条）
    # entry = (base_score, fname_bonus, mtime, fname, title, content) → content at index 5
    selected_doclinks = [
        e for e in doclink_scored
        if "📄" in e[5] or "链接：" in e[5]
    ][:2]

    # 联系人文件补充：当查询含"找谁/联系/who/contact"等信号时，
    # 始终从 部门联系人.md 独立选取最匹配的 ≤2 条，强制放到输出最前面。
    # 同时把 selected_content 中属于联系人文件的条目剔除（避免重复）。
    selected_contacts = []
    if words and any(sig in query.lower() for sig in _CONTACT_SIGNALS):
        contact_pool = []
        for fname, fdata in index.get("files", {}).items():
            if Path(fname).name != _CONTACT_FILE:
                continue
            mtime = fdata["mtime"]
            for s in fdata["sections"]:
                title_hay = s["title"].lower()
                kw_set = set(s["keywords"])
                score = sum(
                    (3 if w in title_hay else 1) * idf.get(w, 1.0)
                    for w in words if w in kw_set
                )
                if score > 0:
                    contact_pool.append((score, 0, mtime, fname, s["title"], s["content"]))
        contact_pool.sort(key=lambda x: (-x[0], -x[2]))
        selected_contacts = contact_pool[:2]
        if selected_contacts:
            # 把联系人文件的重复条目从 selected_content 中移除
            selected_content = [e for e in selected_content if Path(e[3]).name != _CONTACT_FILE]

    # 产品培训文件兜底：仅在主内容不足时补充（最多 2 条，不与主内容竞争名额）
    training_fallback: list[tuple] = []
    if len(selected_content) < 2:
        training_file_count: dict[str, int] = defaultdict(int)
        for entry in training_scored:
            fname = entry[3]
            if training_file_count[fname] < 2:
                training_file_count[fname] += 1
                training_fallback.append(entry)
            if len(training_fallback) >= 2:
                break

    # 格式化并去重，总字符不超过 8000（约 2300 tokens）
    # 联系人结果强制置顶，确保 LLM 能看到正确的联系人信息
    MAX_CONTEXT_CHARS = 8000
    MAX_SINGLE_SECTION = 2000  # 单条 section 超过此限截断，避免一个超大 section 撑满预算
    results = []
    seen: set[str] = set()
    total_chars = 0
    for entry in selected_contacts + selected_content + selected_doclinks + training_fallback:
        _, _, mtime, fname, _, content = entry
        if content in seen:
            continue
        seen.add(content)
        # 截断超大单条 section，避免一段内容耗尽全部预算
        if len(content) > MAX_SINGLE_SECTION:
            content = content[:MAX_SINGLE_SECTION]
        formatted = f"[来源: {fname} | 更新: {datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')}]\n{content}"
        remaining = MAX_CONTEXT_CHARS - total_chars
        if len(formatted) > remaining:
            if not results:  # 第一条结果超限时截断包含，否则跳过
                results.append(formatted[:remaining])
            break
        results.append(formatted)
        total_chars += len(formatted)
    return results


if __name__ == "__main__":
    import sys
    force = "--rebuild" in sys.argv
    idx = build_index(force=force)
    total = sum(len(v["sections"]) for v in idx["files"].values())
    mode = "全量重建" if force else "增量更新"
    print(f"[{mode}] {len(idx['files'])} 文件，{total} 章节，索引时间：{idx['built_at']}")
