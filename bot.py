"""
【部门 HR 问答 Bot】HR Dept Q&A Assistant — Lark WebSocket 长连接

启动：python3 bot.py
停止：kill $(cat /tmp/lark-hr-bot-dept.pid)
"""
from __future__ import annotations  # 让类型注解变成字符串，避免 lark.im.v1.xxx 在导入时被解析

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time as _time
import uuid
from pathlib import Path
from typing import Optional

# 公司 DNS 把 msg-frontier-sg.larksuite.com（飞书 WS 网关）解析到劣质 Akamai
# 边缘节点（TLS 握手成功率约 1/3，长连接反复超时）；公共 DNS 解析出的节点健康。
# 在进程内接管该域名解析：启动时用 dig@公共DNS 动态取 IP，失败则用内置健康清单。
import socket as _socket

_FRONTIER_PAT = re.compile(r"^msg-frontier.*\.larksuite\.com$")
_FRONTIER_FALLBACK_IPS = [
    "23.200.143.69", "23.200.143.70", "23.200.143.74", "23.200.143.79",
    "23.200.143.81", "23.200.143.83", "23.200.143.89", "23.32.20.17",
    "23.32.20.24", "23.32.20.25", "23.32.20.26", "23.32.20.32",
    "23.32.20.35", "23.32.20.41", "23.32.20.43",
]

def _resolve_frontier_ips() -> list:
    for dns in ("223.5.5.5", "119.29.29.29"):
        try:
            out = subprocess.run(
                ["dig", "+short", f"@{dns}", "+time=2", "+tries=1",
                 "msg-frontier-sg.larksuite.com"],
                capture_output=True, text=True, timeout=4,
            ).stdout
            ips = [l.strip() for l in out.splitlines()
                   if l.strip()[:1].isdigit() and l.strip().count(".") == 3]
            if ips:
                return list(dict.fromkeys(ips))
        except Exception:
            continue
    return _FRONTIER_FALLBACK_IPS

_FRONTIER_IPS = _resolve_frontier_ips()
_orig_getaddrinfo = _socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if isinstance(host, str) and _FRONTIER_PAT.match(host):
        out = []
        for fam, typ, pro, canon, sockaddr in infos:
            if fam == _socket.AF_INET:
                out.extend((fam, typ, pro, canon, (ip, sockaddr[1]))
                           for ip in _FRONTIER_IPS)
            else:
                out.append((fam, typ, pro, canon, sockaddr))
        return out or infos
    return infos

_socket.getaddrinfo = _patched_getaddrinfo

from config import APP_ID, APP_SECRET, ADMIN_OPEN_ID
from kb import search, has_intent
from llm import answer, answer_stream, is_guard_query, strip_preamble, strip_article_citations, needs_clarification, clarify, _detect_lang, self_check
import question_log

# lark_oapi 在 main() 里延迟导入（dispatcher_handler 会拉入 25 个 API 处理器，耗时较长）
# 以下变量由 main() 的 _init_lark_sdk() 填充，函数调用时已保证可用
lark = None  # type: ignore
_sdk_client = None  # type: ignore
CreateMessageRequest = CreateMessageRequestBody = None  # type: ignore
CreateMessageReactionRequest = CreateMessageReactionRequestBody = None  # type: ignore
DeleteMessageReactionRequest = None  # type: ignore
PatchMessageRequest = PatchMessageRequestBody = None  # type: ignore
Emoji = None  # type: ignore
P2CardActionTrigger = P2CardActionTriggerResponse = None  # type: ignore
CallBackToast = CallBackCard = None  # type: ignore

_ADMIN_OPEN_ID = ADMIN_OPEN_ID  # 管理员 open_id（在 config.py 中配置）
_ADMIN_RELOAD_CMDS = frozenset(["kb重载", "kb reload", "/kb-reload", "知识库重载", "/kb"])
_ADMIN_SYNC_CMDS = frozenset(["同步", "sync", "/sync", "kb同步"])
_ADMIN_KB_CONFIRM_CMDS = frozenset(["确认补充", "confirm kb", "/kb-confirm"])
_ADMIN_KB_DISMISS_CMDS = frozenset(["忽略建议", "dismiss kb", "/kb-dismiss"])

_GREETINGS = {
    "hi","hello","你好","早","早上好","下午好","晚上好","嗨","hey","哈喽","早安","晚安",
    "good morning","good afternoon","good evening",
    "在吗","在不在","有人吗","你在吗","你在不在",
}
# 打招呼词根：短消息里只要以这些开头，视为打招呼
_GREETING_STEMS = ("你好", "嗨", "hi", "hey", "hello", "哈喽", "早上好", "下午好", "晚上好", "早安", "晚安", "good morning", "good afternoon", "good evening")
_BOT_INTRO_ZH = "你好呀 👋 我是公司 HR 助手～假期、报销、绩效、福利、入职……这些 HR 问题都可以问我，有什么需要帮忙的吗？"
_BOT_INTRO_EN = "Hey there 👋 I'm the company HR assistant! Ask me anything about leave, expenses, performance, benefits, or onboarding — happy to help 😊"

# 社交类短消息集合，命中后直接回应，不走 KB
_SOCIAL_THANKS = {
    "谢谢","谢谢你","谢谢啦","谢谢哦","谢谢了","谢谢您","非常感谢","非常感谢你",
    "多谢","多谢你","多谢啦","感谢","感谢你","感谢啦","感谢您","太感谢了","感谢感谢",
    "thanks","thank you","thx","ty","thank u","many thanks","thanks a lot","thanks so much",
    "thank you so much","cheers",
}
_SOCIAL_ACK = {
    "好的","好","嗯","嗯嗯","嗯好的","好的好的","好滴","收到","收到了","收到收到",
    "了解","了解了","了解了解","明白","明白了","明白了谢谢","知道了","知道了谢谢",
    "好的谢谢","好的收到","ok","okay","ok thanks","okok","alright",
    "got it","noted","i see","understood","sure","roger",
}
_SOCIAL_COMPLIMENT = {
    "辛苦了","你辛苦了","辛苦啦","厉害","厉害了","好厉害","不错","很棒","真棒",
    "棒棒","太棒了","666","nice","great","awesome","perfect","excellent","cool",
    "good job","well done","amazing","brilliant",
}
_SOCIAL_THANKS_REPLY_ZH  = "不客气～有其他问题随时来找我 😊"
_SOCIAL_THANKS_REPLY_EN  = "You're welcome! Feel free to ask anytime 😊"
_SOCIAL_ACK_REPLY_ZH     = "好的！还有其他问题随时问我～"
_SOCIAL_ACK_REPLY_EN     = "Got it! Let me know if there's anything else 😊"
_SOCIAL_COMP_REPLY_ZH    = "谢谢夸奖 😊 有什么需要帮忙的随时说～"
_SOCIAL_COMP_REPLY_EN    = "Aw, thanks! I'm here whenever you need me 😊"

# ── Grounding check：答案数字若不在 KB contexts 里，追加"请以实际政策为准" ─────────

_GROUNDING_RE = re.compile(r'\d+(?:\.\d+)?(?:天|日|月|年|%|元|万|小时|工作日|个工作)')


def _grounding_check(reply: str, contexts: list[str]) -> str:
    combined = " ".join(contexts)

    def _annotate(m: "re.Match") -> str:
        num = m.group(0)
        if num in combined:
            return num
        return f"{num}（请以实际政策为准）"

    return _GROUNDING_RE.sub(_annotate, reply)


# ── 精准链接：从 KB chunks 提取真实 URL + 按来源文件匹配 portal ─────────────────

# KB chunk 里的 markdown 超链接格式
_URL_RE = re.compile(r'\[([^\]\n]{1,80})\]\((https://[^\s)]{10,})\)')
# LLM 偶尔仍会输出"相关文档：[...]"格式的末尾链接行，统一清除
_LINK_LINE_RE = re.compile(r'^(相关文档[：:]\s*|Related documents?[：:\s]+)\[', re.IGNORECASE)

# 已删除 / 失效的飞书文档链接，不推送给用户
_DEAD_URLS: set[str] = set()  # 由 dead_links.json 自动维护，每周一扫描更新
# 自动死链列表：由 check_dead_links.py --update-json 写入，bot 启动时合并
_DEAD_LINKS_FILE = Path(__file__).parent / "dead_links.json"
try:
    _auto_dead: list[str] = json.loads(_DEAD_LINKS_FILE.read_text())
    _DEAD_URLS |= set(_auto_dead)
except FileNotFoundError:
    pass
except Exception as _e:
    pass  # 加载失败不影响启动，下次扫描会重写

# 文档级链接注册表：当某个 KB 文件被召回时，自动附加对应的主文档链接。
# 用于解决"内容在内容文件，PDF 链接在索引文件"导致链接无法跟随内容推送的问题。
_DOC_LINKS: dict[str, tuple[str, str]] = {}
# kb_url_map.json 在 main() 启动时加载（log 初始化后）

_CHUNK_SOURCE_RE = re.compile(r'^\[来源:\s*([^|]+?)\s*\|')
# 用于剥离 chunk header，取出实质内容做引用核查
_CHUNK_HEADER_STRIP_RE = re.compile(r'^\[来源:[^\]]*\]\s*', re.MULTILINE)
# 中文 8+字 或 英文 6+字 的连续片段，LLM "照抄" 原则下必然会在 reply 中原样出现
# 英文 HR 常用词（leave/salary/annual/policy/benefits）最长 8 字，10+ 阈值会导致英文内容全部 miss
_CITE_PHRASE_RE = re.compile(r'[一-鿿]{8,}|[a-zA-Z]{6,}')

# 话题级权威链接白名单：某些高频"交叉话题"的关键内容以样板文字形式散落在多个文档里
# （例："勇敢说/Speakup"作为页脚出现在 8+ 个 KB 文件），按文档级机制会一次推出一堆
# 无关链接。对这类话题：查询命中时只推这一条权威链接，并抑制"因样板文字被召回"的
# 文档级链接（不影响该话题以外的正常链接）。
# 注意：权威页可能位于"不抓取内容"的受限空间——此处仅作为跳转链接推给用户，
#       绝不把该空间内容纳入本地 KB（受限空间仅作跳转链接，不抓内容）。
_TOPIC_LINKS: list[dict] = [
    {
        "id": "speakup",
        # 查询里出现下列任一词即判定为该话题（命中后独占推送，跳过其它所有链接）
        "query": ("勇敢说", "speakup", "speak up"),
        "label": "员工认可机制 · 勇敢说 / Speakup",
        "url": "https://your-tenant.larksuite.com/wiki/在此填入权威页链接",
    },
]


# 查询分词：ASCII 单词、数字串、CJK 连续串分别切出，避免把 "LBPM培训资料"
# 这类混合串当成一个永远匹配不到的巨型 token（\w 在 Python 会连 CJK 一起吞）
_QUERY_TERM_RE = re.compile(r'[a-z]{2,}|[0-9]{2,}')


def _query_terms(query: str) -> list[str]:
    """提取查询词用于文件名相关性打分。
    英文/数字：正则直接提取；
    中文：滑动 bigram + trigram，避免贪婪匹配把整句当一个 token 导致文件名永远匹配不上。"""
    terms = _QUERY_TERM_RE.findall(query.lower())
    cn = re.findall(r'[一-鿿]', query)
    for i in range(len(cn)):
        if i + 1 < len(cn):
            terms.append(cn[i] + cn[i + 1])
        if i + 2 < len(cn):
            terms.append(cn[i] + cn[i + 1] + cn[i + 2])
    return list(set(terms))


def _link_context_relevant(chunk: str, match_start: int, query: str) -> bool:
    """检查链接前后各 150 字范围内是否包含查询关键词。
    比原版（仅前150字）更宽松，比全文匹配更精准。"""
    if not query:
        return True
    window = chunk[max(0, match_start - 150):match_start + 150].lower()
    terms = _query_terms(query)
    if not terms:
        return True
    return any(t in window for t in terms)


def _chunk_in_reply(chunk: str, reply: str) -> bool:
    """检查 chunk 的实质内容是否被 LLM 引用进了 reply。
    中文 8+ 连续字、英文 6+ 连续字母。LLM 系统提示要求原文照抄，
    被引用的 chunk 必然在 reply 里留下可识别片段。"""
    content = _CHUNK_HEADER_STRIP_RE.sub("", chunk).strip()
    reply_l = reply.lower()
    for phrase in _CITE_PHRASE_RE.findall(content)[:40]:
        if phrase.lower() in reply_l:
            return True
    return False


# PDF/PPT/PPTX 等附件文件 stub 标记：这类文件内容无法解析，KB 里只存链接占位符。
# 检测到 stub 后，跳过 _chunk_in_reply（无实质内容可引用），只凭分数+文件名相关性推链接。
_STUB_MARKER = "内容暂未录入知识库"


def _is_stub_chunk(chunk: str) -> bool:
    """判断 chunk 是否来自附件 stub 文件（PDF/PPT/PPTX 等无法解析内容的文件）。"""
    return _STUB_MARKER in chunk


def _curate_links(contexts: list[str], query: str = "",
                  scores: list = None,
                  reply: str = "") -> list[tuple[str, str]]:
    """推送链接 = 回答真正引用的文档链接。

    机制零：话题白名单（命中即独占）。
    机制一（文档级，主）：扫 top-3 chunks，四道关卡：
      ① reranker 分数下限（< 0.05 全跳）；
      ② topic guard（大全文档只在主题查询时推）；
      ③ _chunk_in_reply（LLM 真正引用才推）；stub 附件文件+文件名相关，或高分(>0.5)+文件名相关时可绕过③；
      ④ 文件名相关性过滤（max_score > 0 时只留匹配文件；= 0 时只留分最高的一篇）。
    机制二（内联级，辅）：扫 top-2 chunks 正文里的跨文档超链接；source chunk 须通过
      _chunk_in_reply；链接上下文须含查询词；topic guard 同样适用。
    统一过滤死链（_DEAD_URLS）与重复（按 URL 和标题去重）。"""
    seen_urls: set[str] = set()
    seen_title_keys: set[str] = set()
    links: list[tuple[str, str]] = []

    def _title_key(label: str) -> str:
        return "".join(label.split())[:12]

    def _add(label: str, url: str) -> bool:
        if "larksuite.com" not in url or url in _DEAD_URLS or url in seen_urls:
            return False
        tk = _title_key(label)
        if tk in seen_title_keys:
            return False
        seen_urls.add(url)
        seen_title_keys.add(tk)
        links.append((label, url))
        return True

    # 机制零：话题级权威链接白名单（最高优先，命中即独占）
    # 命中高频交叉话题时，只推这一条/几条权威链接，并完全跳过文档级/内联链接。
    # 原因：这类话题（如"勇敢说/Speakup"）的关键词会以样板页脚、或语义相近
    # （举报↔保密/密级、监督↔新员工规范）的方式召回一堆无关文档，按文档级机制会
    # 把它们的链接一起刷出去。对这些话题，权威链接就是唯一正确答案，其余一律不推。
    q_lower = query.lower()
    matched_topics = [t for t in _TOPIC_LINKS
                      if any(p.lower() in q_lower for p in t["query"])]
    if matched_topics:
        for t in matched_topics:
            _add(t["label"], t["url"])
        log.info("[links] 命中话题白名单 %s，独占推送 query=%r -> %d 条",
                 [t["id"] for t in matched_topics], query[:30], len(links))
        return links

    # 机制一：文档级规范链接——内容 + 标题双重核查。
    # ① _chunk_in_reply：chunk 实质内容确实出现在 reply 里才算被引用。
    # ② topic guard：新员工指引是"大全文档"，嵌入了 LBPM/团建/福利等内容，
    #    非入职类查询不推其链接，避免乱入。
    # ③ 文件名相关性：对所有通过①②的候选，优先推文件名与查询关键词匹配的文件。
    # 扫描范围：top-3（而非 top-2），让真正的专项文档有机会被发现（避免被
    # 大全文档占满 top-2 而挤出去）。

    # 新员工大全文档的 topic guard：只有入职相关查询才推其链接
    _NJ_FNAMES = {"新员工入职快速指引.md", "New Joiner Online Training Guide.md"}
    _NJ_KEYWORDS = ["入职", "新员工", "onboard", "new joiner", "orientation",
                    "first day", "第一天", "试用期", "岗前"]

    def _is_nj_query() -> bool:
        ql = query.lower()
        return any(kw.lower() in ql for kw in _NJ_KEYWORDS)

    nj_allowed = _is_nj_query()

    def _fname_score(fname: str) -> int:
        terms = _query_terms(query)
        fl = fname.lower()
        return sum(1 for t in terms if t in fl)

    # 关卡①：reranker 分数下限——top 分太低说明 KB 无好答案，不推任何链接
    top_score = max(scores[:3]) if scores and len(scores) >= 1 else 1.0
    if top_score < 0.05:
        log.info("[links] reranker top 分 %.3f < 0.05，跳过所有链接", top_score)
        return links

    candidates: list[tuple[str, tuple]] = []  # (fname, (label, url))
    for i, ctx in enumerate(contexts[:3]):     # ← top-3 扫描，给专项文档机会
        m = _CHUNK_SOURCE_RE.match(ctx)
        if not m:
            continue
        fname = m.group(1).strip()
        doc = _DOC_LINKS.get(fname)
        if not doc:
            continue
        # 关卡②：topic guard
        if fname in _NJ_FNAMES and not nj_allowed:
            log.info("[links] topic guard（非入职查询），跳过新员工指引链接: %s", fname[:40])
            continue
        # 关卡③：_chunk_in_reply——以下情况绕过：
        #   a) stub 文件（PDF/PPT/PPTX 等附件，无实质内容可引用）且文件名相关
        #   b) 高分(>0.5) + 文件名相关（弥补 LLM 改写导致漏推）
        chunk_score = scores[i] if scores and i < len(scores) else 0.0
        if reply and not _chunk_in_reply(ctx, reply):
            if _is_stub_chunk(ctx) and _fname_score(fname) > 0:
                log.info("[links] stub 附件文件+文件名相关，跳过 _chunk_in_reply: %s", fname[:40])
            elif chunk_score > 0.5 and _fname_score(fname) > 0:
                log.info("[links] 高分(%.2f)+文件名相关，绕过 _chunk_in_reply: %s",
                         chunk_score, fname[:40])
            else:
                log.info("[links] chunk 未被引用，跳过文档链接: %s", fname[:40])
                continue
        candidates.append((fname, doc))

    if candidates:
        scored = [(fname, doc, _fname_score(fname)) for fname, doc in candidates]
        max_score = max(s for _, _, s in scored)
        if max_score > 0:
            # 关卡④-a：有文件名相关的，只推匹配的
            suppressed = [fn for fn, _, s in scored if s == 0]
            if suppressed:
                log.info("[links] 文件名相关性过滤，跳过低相关文件: %s", suppressed)
            candidates = [(fn, doc) for fn, doc, s in scored if s > 0]
        else:
            # 关卡④-b：无文件名相关的候选
            # reranker 高度置信（>0.7）时，保留最高分 1 条——说明内容真实命中，
            # 只是文件名（如"员工制度手册"）不含查询词（如"年假"），属于大全文档正常覆盖。
            # 低分时（<0.7）说明匹配本就不确定，不推更稳。
            if candidates and top_score > 0.7:
                log.info("[links] 文件名无查询词但 reranker 高分 %.3f，保留最高分 1 条", top_score)
                candidates = candidates[:1]
            else:
                log.info("[links] 所有候选文件名均不含查询词（top_score=%.3f），丢弃全部候选", top_score)
                candidates = []
        for fname, doc in candidates:
            _add(doc[0], doc[1])

    # 机制二：内联跨文档链接——source chunk 须通过 _chunk_in_reply，防止未被引用的 chunk 绕路推链接
    for ctx in contexts[:2]:
        src_m = _CHUNK_SOURCE_RE.match(ctx)
        ctx_fname = src_m.group(1).strip() if src_m else ""
        # topic guard
        if ctx_fname in _NJ_FNAMES and not nj_allowed:
            log.info("[links] topic guard（机制二，非入职查询），跳过 NJ 内联链接: %s", ctx_fname[:40])
            continue
        # source chunk 须被 LLM 引用，否则其内联链接也不推
        if reply and not _chunk_in_reply(ctx, reply):
            log.info("[links] 机制二：source chunk 未被引用，跳过内联链接扫描: %s", ctx_fname[:40])
            continue
        for m in _URL_RE.finditer(ctx):
            label, url = m.group(1).strip(), m.group(2).strip()
            if "larksuite.com" not in url:
                continue
            if not _link_context_relevant(ctx, m.start(), query):
                log.debug("[links] 跳过不相关内联链接: [%s] (query=%r)", label[:20], query[:20])
                continue
            _add(label, url)

    log.info("[links] query=%r found=%d %s", query[:30], len(links),
             [(l[:20], u[-30:]) for l, u in links])
    return links


_AI_DISCLAIMER = "以上内容AI生成，仅供参考"

# 涉及劳动权益的高风险问题，强制加更明确的提示
_HIGH_RISK_RE = re.compile(
    r"劳动合同|劳务协议|续签合同|合同.*签|签.*合同|"
    r"病假|年假|产假|陪产假|婚假|丧假|工伤|事假|"
    r"离职|辞职|解雇|裁员|终止.*合同|合同.*终止|"
    r"工资|薪资|薪酬|绩效.*奖|奖.*绩效|扣薪|扣工资|"
    r"赔偿|补偿金|经济补偿|"
    r"开除|辞退|劝退|严重违规|严重警告|"
    r"试用期.*辞|辞.*试用期"
)

_HIGH_RISK_NOTE_ZH = (
    "⚠️ 以上内容AI生成，仅供参考。"
    "**此问题涉及劳动权益，请以实际政策文件或HR团队确认为准，切勿仅凭本回答做决策。**"
)
_HIGH_RISK_NOTE_EN = (
    "⚠️ AI-generated, for reference only. "
    "**This involves employment rights — please confirm with HR or official policy documents before acting.**"
)


def _finalize_links(reply: str, links: list[tuple[str, str]], lang: str = "zh",
                    query: str = "") -> str:
    """清理 LLM 可能输出的末尾链接行；把 KB 原文找到的链接统一追加到答案末尾；加免责声明。"""
    lines = reply.splitlines()
    filtered = [l for l in lines if not _LINK_LINE_RE.match(l.strip())]
    while filtered and not filtered[-1].strip():
        filtered.pop()
    reply_clean = "\n".join(filtered)
    if links:
        link_header = "🔗 Related documents" if lang == "en" else "🔗 相关文档"
        link_lines = "\n".join(f"· [{label}]({url})" for label, url in links)
        reply_clean += "\n\n" + f"{link_header}\n{link_lines}"
    # 高风险 topic 用更强的提示，否则用通用提示
    if query and _HIGH_RISK_RE.search(query):
        disclaimer = _HIGH_RISK_NOTE_EN if lang == "en" else _HIGH_RISK_NOTE_ZH
    else:
        disclaimer = "⚠️ The above is AI-generated and for reference only." if lang == "en" else f"⚠️ {_AI_DISCLAIMER}"
    reply_clean += f"\n\n{disclaimer}"
    return reply_clean


def _classify_social(q_clean: str) -> Optional[str]:
    """短社交消息分类，返回 'thanks'/'ack'/'compliment' 或 None。"""
    if len(q_clean) > 30:
        return None
    if q_clean in _SOCIAL_THANKS:
        return "thanks"
    if q_clean in _SOCIAL_ACK:
        return "ack"
    if q_clean in _SOCIAL_COMPLIMENT:
        return "compliment"
    return None


def _strip_markdown(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"[-=]{2,}", stripped):
            continue
        if re.fullmatch(r"[|\s\-:]+", stripped) and "|" in stripped:
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
            lines.append("  ".join(cells))
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", line)
        line = re.sub(r"_{1,2}([^_\n]+)_{1,2}", r"\1", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^[-*+]\s+", "· ", line)
        lines.append(line)
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.expanduser("~/.local/log/lark-hr-bot-dept.log"),
            encoding="utf-8",
        ),
    ],
)
# lark_oapi 会往 root logger 注入自己的 handler，导致日志重复打印；提前关闭传播
logging.getLogger("lark_oapi").propagate = False
log = logging.getLogger(__name__)

_FAIL_EXIT_AFTER = 900


# WS 不稳定的几种已知错误特征串。之前只认 "NameResolutionError"（纯断网），
# 但 2026-07-16 排查发现真正高频的失败模式是 SASE 零信任客户端间歇性拦截/掐断
# 长连接，报错是 "timed out during opening handshake" / "keepalive ping timeout" /
# "no close frame received"——这些完全不含 "NameResolutionError"，导致这道最终
# 保险丝形同虚设：进程可以在这类错误里卡住 20+ 分钟也不会触发退出重启。
# 现在把这些模式也纳入监测，作为「进程内重连（见 main() 里的 ws.start() 循环）
# 也救不回来」时的最终兜底：持续失败超过阈值 → 主动退出，让 launchd KeepAlive 拉起全新进程。
_WS_FAIL_PATTERNS = (
    "NameResolutionError",
    "timed out during opening handshake",
    "keepalive ping timeout",
    "no close frame received",
)


class _FailExitHandler(logging.Handler):
    def __init__(self, max_seconds: float = _FAIL_EXIT_AFTER):
        super().__init__()
        self._first_fail = None  # type: Optional[float]
        self._max = max_seconds

    def reset_failure(self) -> None:
        self._first_fail = None

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        is_ws_fail = ("connect failed" in msg or "receive message loop exit" in msg) \
            and any(p in msg for p in _WS_FAIL_PATTERNS)
        if is_ws_fail:
            if self._first_fail is None:
                self._first_fail = _time.time()
            elif _time.time() - self._first_fail >= self._max:
                log.warning("WS 持续异常超过 %.0f 分钟（进程内重连未能恢复），主动退出等待 launchd 重启",
                            self._max / 60)
                os._exit(1)


_fail_handler = None  # type: Optional[_FailExitHandler]

_OPENID_CACHE_TTL = 86400

# qid → (question, answer_text, timestamp)  用于卡片点击时还原内容
_card_store: dict = {}
_CARD_STORE_TTL = 7200  # 2 小时


def _card_store_put(qid: str, question: str, answer_text: str,
                    chat_id: str = "", lang: str = "zh") -> None:
    _card_store[qid] = (question, answer_text, _time.time(), chat_id, lang)
    expired = [k for k, (_, _, ts, _, _) in _card_store.items()
               if _time.time() - ts > _CARD_STORE_TTL]
    for k in expired:
        del _card_store[k]


def _card_store_pop(qid: str) -> Optional[tuple]:
    entry = _card_store.get(qid)
    if entry:
        q, a, ts, chat_id, lang = entry
        del _card_store[qid]
        if _time.time() - ts < _CARD_STORE_TTL:
            return q, a, chat_id, lang
    return None


# ── open_id 查询缓存 + 速率限制 ───────────────────────────────────────────────

_openid_cache: dict = {}
_last_lark_api_call: float = 0.0
_LARK_API_MIN_INTERVAL = 0.25  # seconds between consecutive Lark contact API calls


def _lookup_open_id(name: str) -> Optional[str]:
    global _last_lark_api_call
    now = _time.time()
    if name in _openid_cache:
        cached_id, expiry = _openid_cache[name]
        if now < expiry:
            return cached_id
    # Rate-limit: ensure minimum gap between API calls to avoid 429
    gap = _LARK_API_MIN_INTERVAL - (_time.time() - _last_lark_api_call)
    if gap > 0:
        _time.sleep(gap)
    keyword = name.split(".")[0].strip()
    name_safe = re.sub(r'[^a-z0-9._\- ]', '', name.lower())
    open_id = None
    try:
        def _search(query_kw, jq):
            global _last_lark_api_call
            _last_lark_api_call = _time.time()
            r = subprocess.run(
                ["lark-cli", "contact", "+search-user", "--query", query_kw, "--as", "user", "-q", jq],
                capture_output=True, text=True, timeout=8
            )
            return [l.strip().strip('"') for l in r.stdout.strip().splitlines() if l.strip()]

        # Pass 1: exact localized_name match (e.g. "tommy.tang" in "Tommy.tang")
        lines = _search(keyword, f'.data.users[] | select((.localized_name | ascii_downcase | contains("{name_safe}")) and .is_cross_tenant == false and .is_activated == true) | .open_id')
        open_id = lines[0] if lines else None

        # Pass 2: prefix match — handles shortened handles like "Joen.w" when searching "joen.wang"
        if not open_id:
            keyword_safe = re.sub(r'[^a-z0-9]', '', keyword.lower())
            gap2 = _LARK_API_MIN_INTERVAL - (_time.time() - _last_lark_api_call)
            if gap2 > 0:
                _time.sleep(gap2)
            lines2 = _search(keyword, f'.data.users[] | select((.localized_name | ascii_downcase | startswith("{keyword_safe}.")) and .is_cross_tenant == false and .is_activated == true) | .open_id')
            open_id = lines2[0] if lines2 else None

    except Exception as e:
        log.warning("查询用户 %s 失败: %s", name, e)
        open_id = None
    _openid_cache[name] = (open_id, now + _OPENID_CACHE_TTL)
    return open_id


# ── 卡片构建 ──────────────────────────────────────────────────────────────────
#
# 清新风格：浅色 header 色带 + 干净分区（正文 / 链接 / 反馈 / 免责声明），
# 免责声明从正文里拆出来单独用小字 footer 呈现，高风险提示保留加粗单独醒目展示。

_CARD_HEADER_ANSWER   = "turquoise"  # 薄荷绿，日常回答（主色调）
_CARD_HEADER_HEALED   = "green"      # 深一点的绿，跟「知识库已更新」的积极状态呼应
_CARD_HEADER_STREAM   = "grey"       # 灰，生成中的中间态
_CARD_HEADER_POSITIVE = "green"      # 用户反馈「有帮助/满意」后
_CARD_HEADER_NEUTRAL  = "turquoise"  # 用户反馈「没帮到/不满意」后，跟主卡片薄荷绿保持一致，不刺眼


def _card_header(title: str, template: str) -> dict:
    return {"template": template, "title": {"tag": "plain_text", "content": title}}


def _split_disclaimer(text: str) -> tuple[str, Optional[str]]:
    """把 _finalize_links 追加在末尾的免责声明段落拆出来，供卡片单独渲染。"""
    if "\n\n" not in text:
        return text, None
    body, _, last = text.rpartition("\n\n")
    if last.strip().startswith("⚠️"):
        return body, last.strip()
    return text, None


def _icon_note(text: str, icon_token: str, color: str = "grey") -> dict:
    """带图标的 note footer（icon + 灰字），比纯文字更有质感——
    这是 Notion/Linear/ChatGPT 这类产品做「次要信息」时的通用模式：
    用一个小图标代替 emoji，视觉上更统一、更克制。
    仅用 standard_icon，是经典卡片 JSON 1.0 原生支持的组件，无版本兼容风险。"""
    return {
        "tag": "note",
        "elements": [
            {"tag": "standard_icon", "token": icon_token, "color": color},
            {"tag": "plain_text", "content": text},
        ],
    }


def _disclaimer_element(disclaimer: str) -> dict:
    """普通免责声明 → 带图标的灰色小字 footer；含加粗的高风险警示 → 保留醒目 div。"""
    if "**" in disclaimer:
        return {"tag": "div", "text": {"tag": "lark_md", "content": disclaimer}}
    text = disclaimer.lstrip("⚠️").strip()
    return _icon_note(text, "info_outlined")


def _resolve_mentions_for_card(text: str) -> str:
    """把 <<联系:Name>> 转成 lark_md @mention 格式 <at id="open_id"></at>。"""
    def replace(m):
        name = m.group(1).strip()
        open_id = _lookup_open_id(name)
        return f'<at id="{open_id}"></at>' if open_id else f"**{name}**"
    return re.sub(r"<<联系:([^>]+)>>", replace, text)


def _build_answer_card(display_text: str, qid: str, lang: str = "zh") -> dict:
    """构建带「有帮助/没帮助」按钮的答案卡片（清新蓝风格）。"""
    if lang == "en":
        note_text = "Was this helpful?"
        btn_yes = "👍 Helpful"
        btn_no  = "👎 Not quite"
        title   = "💬 HR Assistant"
    else:
        note_text = "这个回答有帮到你吗？"
        btn_yes = "👍 有帮助"
        btn_no  = "👎 没太帮到"
        title   = "💬 HR 小助手"

    body, disclaimer = _split_disclaimer(display_text)
    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": body},
        },
        {"tag": "hr"},
        _icon_note(note_text, "like_outlined"),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_yes},
                    "type": "primary",
                    "size": "tiny",
                    "value": {"action": "helpful", "qid": qid},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_no},
                    "size": "tiny",
                    "value": {"action": "not_helpful", "qid": qid},
                },
            ],
        },
    ]
    if disclaimer:
        elements.append(_disclaimer_element(disclaimer))
    return {
        "config": {"wide_screen_mode": True},
        "header": _card_header(title, _CARD_HEADER_ANSWER),
        "elements": elements,
    }


def _build_thanked_card(display_text: str, action: str, lang: str = "zh") -> dict:
    """点击按钮后更新成的感谢卡片（去掉按钮）。"""
    _notes = {
        "en": {
            "helpful":           "Glad it helped!",
            "not_helpful":       "Got it! Searching for a better answer — I'll send it here if I find one.",
            "satisfied":         "Great, glad the updated answer helped!",
            "still_not_helpful": "Noted, I've flagged this for the HR team to follow up.",
        },
        "zh": {
            "helpful":           "太好了，能帮上忙就好！",
            "not_helpful":       "已记录～正在重新检索，有更好的答案会在这里发给你",
            "satisfied":         "太好了，问题解决了！",
            "still_not_helpful": "已记录，HR 团队会跟进的",
        },
    }
    _titles = {
        "en": {"helpful": "✅ Thanks!", "satisfied": "✅ Thanks!",
               "not_helpful": "📝 Noted", "still_not_helpful": "📝 Noted"},
        "zh": {"helpful": "✅ 感谢反馈", "satisfied": "✅ 感谢反馈",
               "not_helpful": "📝 已记录", "still_not_helpful": "📝 已记录"},
    }
    # 图标+颜色跟随语义：满意 → 打勾/绿色；未满意 → 编辑中/灰色（暗示「正在改进」而非「到此为止」）
    _icon = "done_outlined" if action in ("helpful", "satisfied") else "edit_outlined"
    _icon_color = "green" if action in ("helpful", "satisfied") else "grey"
    note = _notes.get(lang, _notes["zh"]).get(action, "感谢反馈！")
    title = _titles.get(lang, _titles["zh"]).get(action, "✅ 感谢反馈")
    template = _CARD_HEADER_POSITIVE if action in ("helpful", "satisfied") else _CARD_HEADER_NEUTRAL

    body, disclaimer = _split_disclaimer(display_text)
    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": body},
        },
        {"tag": "hr"},
        _icon_note(note, _icon, _icon_color),
    ]
    if disclaimer:
        elements.append(_disclaimer_element(disclaimer))
    return {
        "config": {"wide_screen_mode": True},
        "header": _card_header(title, template),
        "elements": elements,
    }


def _build_healed_card(display_text: str, qid: str, lang: str = "zh") -> dict:
    """自愈后的改进回答卡片（清新青绿风格），带「满意了/还是不满意」按钮。"""
    if lang == "en":
        title    = "🔄 Knowledge base updated"
        note_txt = "Is this more helpful?"
        btn_yes  = "✅ That helps!"
        btn_no   = "😕 Still not quite"
    else:
        title    = "🔄 知识库已更新"
        note_txt = "这次有帮到你吗？"
        btn_yes  = "✅ 满意了"
        btn_no   = "😕 还是不满意"

    body, disclaimer = _split_disclaimer(display_text)
    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": body},
        },
        {"tag": "hr"},
        _icon_note(note_txt, "like_outlined"),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_yes},
                    "type": "primary",
                    "value": {"action": "satisfied", "qid": qid},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_no},
                    "type": "danger",
                    "value": {"action": "still_not_helpful", "qid": qid},
                },
            ],
        },
    ]
    if disclaimer:
        elements.append(_disclaimer_element(disclaimer))
    return {
        "config": {"wide_screen_mode": True},
        "header": _card_header(title, _CARD_HEADER_HEALED),
        "elements": elements,
    }


# ── 消息发送 ──────────────────────────────────────────────────────────────────

def _send_interactive(card: dict, chat_id: str) -> Optional[str]:
    """发送交互式卡片消息，返回 message_id。"""
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card))
            .build()
        )
        .build()
    )
    resp = _sdk_client.im.v1.message.create(req)
    if not resp.success():
        log.warning("发送卡片失败: code=%s msg=%s", resp.code, resp.msg)
        return None
    return resp.data.message_id



def _send_reply_text(text: str, chat_id: str) -> None:
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        )
        .build()
    )
    resp = _sdk_client.im.v1.message.create(req)
    if not resp.success():
        log.warning("发送消息失败: code=%s msg=%s", resp.code, resp.msg)


# ── 流式卡片支持 ──────────────────────────────────────────────────────────────

def _patch_card(message_id: str, card: dict) -> bool:
    """Patch 已有卡片消息的内容。返回是否成功。"""
    req = (PatchMessageRequest.builder()
           .message_id(message_id)
           .request_body(PatchMessageRequestBody.builder()
                         .content(json.dumps(card)).build())
           .build())
    resp = _sdk_client.im.v1.message.patch(req)
    if not resp.success():
        log.warning("patch 卡片失败: code=%s msg=%s", resp.code, resp.msg)
    return resp.success()


def _build_streaming_card(text: str, cursor: bool = True) -> dict:
    """构建流式中间卡片（灰色 header，无按钮，生成完毕后会被答案卡片替换）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": _card_header("💬 …", _CARD_HEADER_STREAM),
        "elements": [{"tag": "div", "text": {"tag": "lark_md",
                       "content": text + (" ▌" if cursor else "")}}],
    }


def _stream_reply(query: str, contexts: list, history: list, lang: str,
                  chat_id: str, sender_id: str, t_start: float, t_kb: float,
                  scores: list = None) -> None:
    """发占位卡片 → 流式 patch → 最终 patch 完整答案卡片（含反馈按钮）。"""
    qid = str(uuid.uuid4())
    placeholder = "Looking it up... ▌" if lang == "en" else "正在查询中... ▌"
    msg_id = _send_interactive(_build_streaming_card(placeholder, cursor=False), chat_id)

    if msg_id is None:
        # 初始卡片发送失败 → 降级同步流程（同样执行 self_check + grounding_check）
        raw = answer(query, contexts, history=history)
        reply_text = _strip_markdown(strip_article_citations(strip_preamble(raw)))
        if contexts and not self_check(reply_text, contexts):
            note = ("\n\n⚠️ Note: Some details may not be directly in the knowledge base — please verify with the HR team."
                    if lang == "en"
                    else "\n\n⚠️ 注意：以上部分内容未能完全对应知识库原文，请以实际政策文件或 HR 团队确认为准。")
            reply_text += note
        if contexts:
            reply_text = _grounding_check(reply_text, contexts)
        # reply 确定后才决定推哪些链接（内容从哪来就推哪里）
        links = _curate_links(contexts, query, scores=scores, reply=reply_text) if contexts and not is_guard_query(query) else []
        reply_text = _finalize_links(reply_text, links, lang, query)
        display = _resolve_mentions_for_card(reply_text)
        _card_store_put(qid, query, display, chat_id, lang)
        _send_interactive(_build_answer_card(display, qid, lang), chat_id)
        # _cache_put 已关闭
        question_log.log_question(query, len(contexts), reply_text)
        _update_history(chat_id, sender_id, query, reply_text)
        return

    buffer = ""
    last_patch_time = _time.time()
    last_patch_len = 0
    t0_llm = _time.time()

    for token in answer_stream(query, contexts, history=history):
        buffer += token
        now = _time.time()
        if now - last_patch_time >= 0.8 and len(buffer) - last_patch_len >= 30:
            _patch_card(msg_id, _build_streaming_card(buffer))
            last_patch_time = now
            last_patch_len = len(buffer)

    t_llm = _time.time() - t0_llm

    # 最终处理：strip/resolve 只在完整文本上调用
    reply_text = _strip_markdown(strip_article_citations(strip_preamble(buffer)))
    # Self-check 必须先于 grounding_check 运行——grounding_check 会在数字后插入括号注释，
    # 若 self_check 拿到修改后的文本则会找不到原字面量，导致误 FAIL
    if contexts and not self_check(reply_text, contexts):
        note = ("\n\n⚠️ Note: Some details may not be directly in the knowledge base — please verify with the HR team."
                if lang == "en"
                else "\n\n⚠️ 注意：以上部分内容未能完全对应知识库原文，请以实际政策文件或 HR 团队确认为准。")
        reply_text += note
        log.info("[self-check] FAIL — 已追加免责提示")
    if contexts:
        reply_text = _grounding_check(reply_text, contexts)
    # reply 确定后才决定推哪些链接（内容从哪来就推哪里）
    links = _curate_links(contexts, query, scores=scores, reply=reply_text) if contexts and not is_guard_query(query) else []
    reply_text = _finalize_links(reply_text, links, lang, query)

    t_total = _time.time() - t_start
    log.info("[PERF] total=%.2fs kb=%.2fs llm=%.2fs chars=%d", t_total, t_kb, t_llm, len(reply_text))

    display_text = _resolve_mentions_for_card(reply_text)
    _card_store_put(qid, query, display_text, chat_id, lang)

    ok = _patch_card(msg_id, _build_answer_card(display_text, qid, lang))
    if not ok:
        log.warning("最终 patch 失败，发送新卡片兜底")
        _send_interactive(_build_answer_card(display_text, qid, lang), chat_id)

    _cache_put(query, reply_text)
    question_log.log_question(query, len(contexts), reply_text)
    # 有意义的问题但 KB 完全没命中 → 立即通知 HR，无需等日报
    if not contexts and not is_guard_query(query):
        try:
            question_log.notify_not_helpful(query, reply_text[:200])
        except Exception as _e:
            log.warning("无命中通知失败: %s", _e)
    _update_history(chat_id, sender_id, query, reply_text)


# ── 表情回调 ─────────────────────────────────────────────────────────────────

def _add_reaction(message_id: str, emoji_type: str = "THINKING") -> Optional[str]:
    """在用户消息上加表情，返回 reaction_id。失败静默，不阻塞主流程。"""
    try:
        emoji = Emoji.builder().emoji_type(emoji_type).build()
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(emoji)
                .build()
            )
            .build()
        )
        resp = _sdk_client.im.v1.message_reaction.create(req)
        if resp.success():
            return resp.data.reaction_id
        log.warning("添加表情失败: code=%s msg=%s", resp.code, resp.msg)
    except Exception as e:
        log.warning("添加表情异常: %s", e)
    return None


def _remove_reaction(message_id: str, reaction_id: str) -> None:
    """移除之前加的表情。失败静默。"""
    try:
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = _sdk_client.im.v1.message_reaction.delete(req)
        if not resp.success():
            log.warning("删除表情失败: code=%s msg=%s", resp.code, resp.msg)
    except Exception as e:
        log.warning("删除表情异常: %s", e)


# ── KB 自愈 ───────────────────────────────────────────────────────────────────

def _heal_and_resend(chat_id: str, query: str, lang: str) -> None:
    """后台线程：同步 KB → 重试 → 在原窗口发改进回答卡片，或通知管理员。"""
    if is_guard_query(query):
        # Guard 类问题（工资/产假等）不自愈，直接通知管理员
        try:
            question_log.notify_not_helpful(query, "（guard 类问题，不自愈）")
        except Exception as e:
            log.warning("[healer] guard notify 失败: %s", e)
        return
    import kb_healer
    contexts = kb_healer.try_heal(query)
    if not contexts:
        log.info("[healer] 自愈后仍无结果，通知管理员")
        try:
            question_log.notify_not_helpful(query, "（自愈后仍无答案）")
        except Exception as e:
            log.warning("[healer] notify 失败: %s", e)
        return

    try:
        new_reply = answer(query, contexts)
        new_reply = _strip_markdown(strip_article_citations(strip_preamble(new_reply)))
        new_reply = _grounding_check(new_reply, contexts)
        links = _curate_links(contexts, query, reply=new_reply)
        new_reply = _finalize_links(new_reply, links, lang, query)
        display = _resolve_mentions_for_card(new_reply)
        new_qid = str(uuid.uuid4())
        _card_store_put(new_qid, query, display, chat_id, lang)
        _send_interactive(_build_healed_card(display, new_qid, lang), chat_id)
        log.info("[healer] 自愈成功，已发改进回答")
    except Exception as e:
        log.error("[healer] 自愈回答生成失败: %s", e, exc_info=True)
        try:
            question_log.notify_not_helpful(query, f"（自愈异常: {e}）")
        except Exception:
            pass


# ── 卡片按钮回调 ──────────────────────────────────────────────────────────────

def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    resp = P2CardActionTriggerResponse()
    try:
        action_value = data.event.action.value or {}
        action = action_value.get("action", "")
        qid = action_value.get("qid", "")

        stored = _card_store_pop(qid)
        question    = stored[0] if stored else ""
        answer_text = stored[1] if stored else ""
        chat_id_stored = stored[2] if stored else ""
        lang_stored    = stored[3] if stored else "zh"

        question_log.log_feedback(action, question, answer_text[:200])

        toast = CallBackToast()

        if action == "helpful":
            toast.type = "success"
            toast.content = "感谢反馈！" if lang_stored == "zh" else "Thanks for your feedback!"

        elif action == "not_helpful":
            toast.type = "info"
            toast.content = (
                "正在为你重新检索，稍后会在这里发新回答～"
                if lang_stored == "zh"
                else "Searching for a better answer — I'll post it here shortly."
            )
            operator_open_id = (data.event.operator.open_id
                                if data.event and data.event.operator else "")
            if chat_id_stored and question:
                threading.Thread(
                    target=_heal_and_resend,
                    args=(chat_id_stored, question, lang_stored),
                    daemon=True,
                ).start()
                # 无论自愈是否成功，立即通知 HR，让 HR 知道有人觉得回答没帮到
                try:
                    question_log.notify_not_helpful(question, answer_text[:200],
                                                    user_open_id=operator_open_id)
                except Exception as e:
                    log.warning("发送 not_helpful 通知失败: %s", e)
            elif question:
                try:
                    question_log.notify_not_helpful(question, answer_text[:200],
                                                    user_open_id=operator_open_id)
                except Exception as e:
                    log.warning("发送 not_helpful 通知失败: %s", e)

        elif action == "satisfied":
            toast.type = "success"
            toast.content = "太好了！" if lang_stored == "zh" else "Great, glad it helped!"

        elif action == "still_not_helpful":
            toast.type = "info"
            toast.content = (
                "已记录，HR 团队会跟进的"
                if lang_stored == "zh"
                else "Noted, I've flagged this for the HR team."
            )
            if question:
                operator_open_id = (data.event.operator.open_id
                                    if data.event and data.event.operator else "")
                try:
                    question_log.notify_not_helpful(question, answer_text[:200],
                                                    user_open_id=operator_open_id)
                except Exception as e:
                    log.warning("发送 still_not_helpful 通知失败: %s", e)

        else:
            toast.type = "info"
            toast.content = "感谢反馈！"

        resp.toast = toast

        if answer_text:
            card = CallBackCard()
            card.type = "raw"
            card.data = _build_thanked_card(answer_text, action, lang_stored)
            resp.card = card

    except Exception as e:
        log.error("处理卡片动作出错: %s", e, exc_info=True)
        toast = CallBackToast()
        toast.type = "error"
        toast.content = "处理出错，请稍后再试"
        resp.toast = toast

    return resp


# ── 文本解析 ──────────────────────────────────────────────────────────────────

def _extract_text(message) -> str:
    if message.message_type == "text":
        try:
            content = json.loads(message.content)
            text = content.get("text", "")
            text = re.sub(r"<at[^>]*>.*?</at>", "", text).strip()
            return text
        except Exception:
            return ""

    if message.message_type == "share_user":
        try:
            content = json.loads(message.content)
            user_id = content.get("user_id", "")
            if not user_id:
                return ""
            result = subprocess.run(
                ["lark-cli", "contact", "+get-user", "--user-id", user_id, "--as", "user",
                 "-q", ".data.user.name"],
                capture_output=True, text=True, timeout=5
            )
            name = result.stdout.strip().strip('"')
            return f"[名片] {name}" if name else ""
        except Exception as e:
            log.warning("解析名片消息失败: %s", e)
            return ""

    return ""


_processed: dict[str, float] = {}
_PROCESSED_TTL = 3600

_history: dict = {}
_history_ts: dict = {}
_history_lock = threading.Lock()
_HISTORY_MAX_TURNS = 5
_HISTORY_TTL = 1800

# ── 问题答案缓存：持久化到文件，重启后仍保留，KB更新后自动失效 ──────────────
from pathlib import Path as _Path
import json as _json

_CACHE_FILE = _Path("/tmp/lark-hr-bot-answer-cache.json")
_answer_cache: dict[str, str] = {}
_answer_cache_lock = threading.Lock()
_answer_cache_rebuilt_at: float = 0.0
_semantic_cache: list = []  # list of (np.ndarray, query_str, answer_str)
_SEMANTIC_CACHE_MAX = 200
_SEMANTIC_SIM_THRESHOLD = 0.92


def _cache_load() -> None:
    """启动时从文件加载缓存。"""
    global _answer_cache, _answer_cache_rebuilt_at
    try:
        if _CACHE_FILE.exists():
            data = _json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            _answer_cache = data.get("cache", {})
            _answer_cache_rebuilt_at = data.get("rebuilt_at", 0.0)
    except Exception:
        _answer_cache = {}


def _cache_flush() -> None:
    """写回文件（已在锁内调用）。"""
    try:
        _CACHE_FILE.write_text(
            _json.dumps({"rebuilt_at": _answer_cache_rebuilt_at, "cache": _answer_cache},
                        ensure_ascii=False, indent=None),
            encoding="utf-8",
        )
    except Exception:
        pass


def _cache_key(query: str) -> str:
    s = query.strip().lower()
    s = re.sub(r"[？?！!。，,、：:；;～~\s]+", " ", s)
    return s.strip()


def _cache_get(query: str) -> Optional[str]:
    import kb as _kb
    key = _cache_key(query)
    with _answer_cache_lock:
        global _answer_cache_rebuilt_at
        if _kb.last_rebuilt_at > _answer_cache_rebuilt_at:
            _answer_cache.clear()
            _semantic_cache.clear()
            _answer_cache_rebuilt_at = _kb.last_rebuilt_at
            _cache_flush()
        hit = _answer_cache.get(key)
        if hit is not None:
            return hit
    # 语义缓存 fallback：cosine 相似度 > 阈值时复用已有答案
    try:
        import vector_store as _vs
        if _vs._model is not None:
            q_vec = _vs._encode([query])[0]
            with _answer_cache_lock:
                for cached_vec, cached_query, cached_answer in _semantic_cache:
                    sim = float(q_vec @ cached_vec)
                    if sim >= _SEMANTIC_SIM_THRESHOLD:
                        log.info("语义缓存命中 sim=%.3f [%s]", sim, cached_query[:30])
                        return cached_answer
    except Exception:
        pass
    return None


def _cache_put(query: str, display_text: str) -> None:
    import kb as _kb
    key = _cache_key(query)
    with _answer_cache_lock:
        global _answer_cache_rebuilt_at
        _answer_cache_rebuilt_at = _kb.last_rebuilt_at
        _answer_cache[key] = display_text
        _cache_flush()
    # 同步存入语义缓存向量（模型未就绪时跳过）
    try:
        import vector_store as _vs
        if _vs._model is not None:
            q_vec = _vs._encode([query])[0]
            with _answer_cache_lock:
                _semantic_cache.append((q_vec, query, display_text))
                if len(_semantic_cache) > _SEMANTIC_CACHE_MAX:
                    _semantic_cache.pop(0)
    except Exception:
        pass


def _get_history(chat_id: str, sender_id: str) -> list:
    import kb as _kb
    key = (chat_id, sender_id)
    now = _time.time()
    with _history_lock:
        # 清理过期历史
        if key in _history_ts and now - _history_ts[key] > _HISTORY_TTL:
            _history.pop(key, None)
            _history_ts.pop(key, None)
        # KB 更新后清空历史，避免旧回答污染新对话
        if key in _history_ts and _kb.last_rebuilt_at > _history_ts[key]:
            _history.pop(key, None)
            _history_ts.pop(key, None)
            log.info("KB 已更新，清空 %s 的对话历史", sender_id[:8] if sender_id else "unknown")
        return list(_history.get(key, []))


def _update_history(chat_id: str, sender_id: str, user_msg: str, assistant_msg: str):
    key = (chat_id, sender_id)
    with _history_lock:
        hist = list(_history.get(key, []))
        hist.append({"role": "user",      "content": user_msg})
        hist.append({"role": "assistant", "content": assistant_msg})
        if len(hist) > _HISTORY_MAX_TURNS * 2:
            hist = hist[-_HISTORY_MAX_TURNS * 2:]
        _history[key] = hist
        _history_ts[key] = _time.time()


def _cleanup_processed() -> None:
    now = _time.time()
    stale = [mid for mid, ts in _processed.items() if now - ts > _PROCESSED_TTL]
    for mid in stale:
        del _processed[mid]


def _start_history_cleanup_thread() -> None:
    """每 30 分钟主动扫描并删除过期的 _history 条目，防止内存无限增长。"""
    def _loop():
        while True:
            _time.sleep(1800)
            now = _time.time()
            with _history_lock:
                expired_keys = [
                    k for k, ts in _history_ts.items()
                    if now - ts > _HISTORY_TTL
                ]
                for k in expired_keys:
                    _history.pop(k, None)
                    _history_ts.pop(k, None)
            if expired_keys:
                log.info("history 清理：删除 %d 条过期记录", len(expired_keys))

    threading.Thread(target=_loop, name="history-cleanup", daemon=True).start()


# ── 主消息处理 ────────────────────────────────────────────────────────────────

def _handle_message(msg, sender, query: str, lang: str) -> None:
    """后台线程：KB 检索 → 澄清 or 流式回答。Reaction 在此管理。"""
    sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
    reaction_id = _add_reaction(msg.message_id, "THINKING")
    t_start = _time.time()
    try:
        history = _get_history(msg.chat_id, sender_id)

        # 答案缓存已关闭（HR 政策随时更新，缓存会导致旧答案+旧链接被推送）

        # Guard 类问题（屏蔽词/离题/产假）直接走空 contexts，跳过 KB 检索和 LLM 改写
        if is_guard_query(query):
            _stream_reply(query, [], history, lang, msg.chat_id, sender_id, t_start, 0.0)
            return

        t0_kb = _time.time()
        scored = search(query, history=history, with_scores=True)
        contexts = [c for _, c in scored]
        scores   = [s for s, _ in scored]
        t_kb = _time.time() - t0_kb
        log.info("命中 %d 个知识库段落 [kb=%.2fs]", len(contexts), t_kb)

        # 模糊短查询 + 0 命中 + 意图不明 → 追问澄清；意图明确但 KB 无内容则走兜底
        if needs_clarification(query, len(contexts)) and not has_intent(query):
            clarify_q = clarify(query)
            log.info("触发澄清追问: %s", clarify_q[:60])
            _send_reply_text(clarify_q, msg.chat_id)
            # 澄清追问是正常流程，不标记为未解答（传 kb_hits=1 跳过 unanswered 标记）
            question_log.log_question(query, max(1, len(contexts)), clarify_q)
            _update_history(msg.chat_id, sender_id, query, clarify_q)
            return

        _stream_reply(query, contexts, history, lang, msg.chat_id, sender_id, t_start, t_kb,
                      scores=scores)

    except Exception as e:
        log.error("处理消息出错: %s", e, exc_info=True)
        try:
            err_msg = ("Oops, something went wrong on my end — please try again in a moment, or contact the HR team directly!"
                       if lang == "en"
                       else "哎呀，处理你的问题时出了点小问题～请稍后再试，或者联系 HR 团队吧！")
            _send_reply_text(err_msg, msg.chat_id)
        except Exception as e2:
            log.error("发送错误提示也失败: %s", e2)
    finally:
        if reaction_id:
            _remove_reaction(msg.message_id, reaction_id)




def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    msg    = data.event.message
    sender = data.event.sender

    if sender.sender_type == "app":
        return
    if msg.message_id in _processed:
        return
    _processed[msg.message_id] = _time.time()
    if len(_processed) >= 200:
        _cleanup_processed()

    query = _extract_text(msg)
    if not query:
        log.info("收到非文本消息 (type=%s)，跳过", msg.message_type)
        return

    log.info("收到问题: %s", query[:80])

    lang = _detect_lang(query)

    if _fail_handler:
        _fail_handler.reset_failure()

    sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""

    # 管理员命令：KB 重载（仅管理员可触发，同步处理）
    if sender_id == _ADMIN_OPEN_ID and query.strip().lower() in _ADMIN_RELOAD_CMDS:
        import kb as _kb_mod
        from index_manager import build_index as _build_index
        idx = _build_index(force=True)
        total_sec = sum(len(v["sections"]) for v in idx["files"].values())
        _kb_mod.mark_fresh()
        _send_reply_text(
            f"✅ KB 已强制重建：{len(idx['files'])} 文件，{total_sec} 章节\n"
            f"更新时间：{idx.get('built_at', '—')}",
            msg.chat_id,
        )
        return

    # 管理员命令：KB 同步（从 Lark 拉取最新内容，仅管理员可触发）
    if sender_id == _ADMIN_OPEN_ID and query.strip().lower() in _ADMIN_SYNC_CMDS:
        _send_reply_text("⏳ 正在同步知识库（从 Lark 拉取最新内容）...", msg.chat_id)
        try:
            result = subprocess.run(
                [sys.executable, str(_Path(__file__).parent / "lark_sync.py")],
                capture_output=True, text=True, timeout=180,
                cwd=str(_Path(__file__).parent),
            )
            # 提取关键摘要行（过滤日志时间戳行）
            key_lines = []
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                # 跳过日志时间戳行：2026-07-09 11:00:05 ...
                if re.match(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', stripped):
                    continue
                key_lines.append(stripped)
            summary = "\n".join(key_lines[-15:]) if key_lines else "（没有检测到变化）"
            _send_reply_text(
                f"✅ 同步完成\n\n{summary}"
                + ("\n\n⚠️ 输出过长已截断" if len(key_lines) > 15 else ""),
                msg.chat_id,
            )
        except subprocess.TimeoutExpired:
            _send_reply_text("⏰ 同步超时（>180s），请稍后重试", msg.chat_id)
        except Exception as e:
            _send_reply_text(f"❌ 同步失败: {e}", msg.chat_id)
        return

    # 管理员命令：确认补充知识库自愈建议（仅管理员可触发）
    if sender_id == _ADMIN_OPEN_ID and query.strip() in _ADMIN_KB_CONFIRM_CMDS:
        import kb_healer
        result = kb_healer.apply_pending_suggestion()
        if result is None:
            _send_reply_text("ℹ️ 当前没有待确认的知识库补充建议", msg.chat_id)
        elif result["added"]:
            lines = "\n".join(f"  · {t}" for t in result["added"])
            _send_reply_text(
                f"✅ 已补充并同步 {len(result['added'])} 个文档：\n{lines}",
                msg.chat_id,
            )
        else:
            _send_reply_text("ℹ️ 建议的文档已存在于知识库，无需补充", msg.chat_id)
        return

    # 管理员命令：忽略知识库自愈建议（仅管理员可触发）
    if sender_id == _ADMIN_OPEN_ID and query.strip() in _ADMIN_KB_DISMISS_CMDS:
        import kb_healer
        cleared = kb_healer.dismiss_pending_suggestion()
        _send_reply_text(
            "✅ 已忽略该建议" if cleared else "ℹ️ 当前没有待确认的知识库补充建议",
            msg.chat_id,
        )
        return

    # 打招呼 / 社交类短消息，同步快速回应，不走 KB
    q_clean = re.sub(r'[^\w\s一-鿿]', '', query).strip().lower()
    is_greeting = q_clean in _GREETINGS or (
        len(q_clean) <= 12 and any(q_clean.startswith(s) for s in _GREETING_STEMS)
    )
    if is_greeting:
        _send_reply_text(_BOT_INTRO_EN if lang == "en" else _BOT_INTRO_ZH, msg.chat_id)
        return
    social = _classify_social(q_clean)
    if social:
        if social == "thanks":
            reply = _SOCIAL_THANKS_REPLY_EN if lang == "en" else _SOCIAL_THANKS_REPLY_ZH
        elif social == "ack":
            reply = _SOCIAL_ACK_REPLY_EN if lang == "en" else _SOCIAL_ACK_REPLY_ZH
        else:
            reply = _SOCIAL_COMP_REPLY_EN if lang == "en" else _SOCIAL_COMP_REPLY_ZH
        _send_reply_text(reply, msg.chat_id)
        return

    # LLM 工作：dispatch 到后台线程，避免阻塞事件处理
    threading.Thread(
        target=_handle_message,
        args=(msg, sender, query, lang),
        daemon=True,
    ).start()


_PID_FILE = "/tmp/lark-hr-bot-dept.pid"


def _ensure_singleton() -> None:
    """PID 文件单实例保证：读旧 PID → 若进程存活则杀掉 → 写入自己的 PID。
    不依赖 pgrep / 进程名大小写，兼容 launchd caffeinate 包装和手动启动两种场景。"""
    my_pid = os.getpid()
    try:
        if os.path.exists(_PID_FILE):
            old_pid = int(open(_PID_FILE).read().strip())
            if old_pid != my_pid:
                try:
                    os.kill(old_pid, 0)  # 探测进程是否存活
                    os.kill(old_pid, 9)
                    log.info("杀掉旧 bot 进程 PID %d", old_pid)
                    _time.sleep(1)
                except ProcessLookupError:
                    pass  # 进程已不存在，正常继续
    except Exception as e:
        log.warning("单实例检测失败: %s", e)
    with open(_PID_FILE, "w") as f:
        f.write(str(my_pid))


def _init_lark_sdk() -> None:
    """延迟导入 lark_oapi（含 dispatcher_handler 的 25 个 API 处理器），填充模块级全局变量。"""
    global lark, _sdk_client
    global CreateMessageRequest, CreateMessageRequestBody
    global CreateMessageReactionRequest, CreateMessageReactionRequestBody
    global DeleteMessageReactionRequest
    global PatchMessageRequest, PatchMessageRequestBody
    global Emoji
    global P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast, CallBackCard
    import lark_oapi as _lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest as _CMR, CreateMessageRequestBody as _CMRB,
        CreateMessageReactionRequest as _CMRR, CreateMessageReactionRequestBody as _CMRRB,
        DeleteMessageReactionRequest as _DMRR,
        PatchMessageRequest as _PMR, PatchMessageRequestBody as _PMRB,
    )
    from lark_oapi.api.im.v1.model.emoji import Emoji as _Emoji
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger as _P2CAT, P2CardActionTriggerResponse as _P2CATR,
        CallBackToast as _CBT, CallBackCard as _CBC,
    )
    lark = _lark
    _sdk_client = _lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    CreateMessageRequest = _CMR; CreateMessageRequestBody = _CMRB
    CreateMessageReactionRequest = _CMRR; CreateMessageReactionRequestBody = _CMRRB
    DeleteMessageReactionRequest = _DMRR
    PatchMessageRequest = _PMR; PatchMessageRequestBody = _PMRB
    Emoji = _Emoji
    P2CardActionTrigger = _P2CAT; P2CardActionTriggerResponse = _P2CATR
    CallBackToast = _CBT; CallBackCard = _CBC
    log.info("lark SDK 初始化完成")


def main():
    os.makedirs(os.path.expanduser("~/.local/log"), exist_ok=True)
    _ensure_singleton()

    from config import KB_DIR
    all_mds_count = len(list(KB_DIR.rglob("*.md")))
    log.info("KB_DIR = %s (exists=%s, md_count=%d)", KB_DIR, KB_DIR.exists(), all_mds_count)
    log.info("构建 KB 索引...")
    from index_manager import build_index, _write_manifest
    idx = build_index(force=True)
    total = sum(len(v["sections"]) for v in idx["files"].values())
    log.info("索引完成：%d 个文件，%d 个章节", len(idx["files"]), total)
    # 同步 kb.py 的 mtime 缓存，避免启动后首次查询触发冗余增量重建
    import kb as _kb_mod
    _kb_mod.mark_fresh()

    # 加载文档级链接（kb_url_map.json）
    _url_map_path = Path(__file__).parent / "kb_url_map.json"
    if _url_map_path.exists():
        try:
            _url_map_data: dict = json.loads(_url_map_path.read_text(encoding="utf-8"))
            for _fname, _info in _url_map_data.items():
                _DOC_LINKS[_fname] = (_info["label"], _info["url"])
            log.info("已加载 %d 条文档级链接", len(_DOC_LINKS))
        except Exception as _e:
            log.warning("加载 kb_url_map.json 失败: %s", _e)

    _cache_load()            # 从文件恢复答案缓存
    _init_lark_sdk()         # 延迟导入 lark_oapi，建立 SDK 客户端

    log.info("【部门 HR 问答 Bot】启动，监听消息中...")

    def _after_daily_report(unanswered: list) -> None:
        import kb_healer
        kb_healer.suggest_and_add_docs(unanswered, question_log.send_admin_dm)

    question_log.start_reporter_thread(post_report_hook=_after_daily_report)
    _start_history_cleanup_thread()

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )

    global _fail_handler
    _fail_handler = _FailExitHandler()
    logging.getLogger().addHandler(_fail_handler)

    # _FailExitHandler 只有在「新的失败日志到达」时才有机会判断超时，但 2026-07-27
    # 实测发现僵尸态：06:24 WS 断开后 SDK 彻底静默（不再打任何日志、ws.start() 也
    # 不返回），handler 永远等不到下一次 emit，进程带着死连接挂了 4 小时。
    # 补一个独立计时线程：一旦出现失败且超过预算时间没有成功复位
    # （reset_failure），直接退出进程，不依赖新日志到达。
    # 预算按用户作息分档（2026-07-27 确认：工作日 9-10 点开盖、18-19 点合盖即睡眠，
    # 节假日不开盖）：
    #   - 工作时间（周一~周五 08:00-19:59）：900 秒，尽快自愈，保证员工能用；
    #   - 非工作时间：6 小时。合盖睡眠期间线程被冻结、不计时；开盖唤醒后第一次
    #     检查必然超预算 → 立即退出 → launchd 拉起全新进程，正好完成"早晨开工前
    #     自动获得干净连接"的效果，又不会在整个夜间/周末反复空转重启。
    # 已知取舍：reset_failure 只在收到用户消息时触发，若 WS 断开后 SDK 自行重连成功
    # 但一直没人发消息，本计时器会误杀一次健康进程——launchd 会立即拉起，
    # 代价仅一次 reranker 重载，且新进程 _first_fail 为空不会循环；可接受。
    def _fail_budget() -> float:
        lt = _time.localtime()
        if lt.tm_wday < 5 and 8 <= lt.tm_hour < 20:
            return _FAIL_EXIT_AFTER
        return 6 * 3600

    def _fail_timer():
        while True:
            _time.sleep(60)
            first = _fail_handler._first_fail
            budget = _fail_budget()
            if first is not None and _time.time() - first >= budget:
                log.warning("WS 失败后 %.0f 秒内无任何恢复迹象（SDK 静默卡死），主动退出等待 launchd 重启",
                            budget)
                os._exit(1)

    threading.Thread(target=_fail_timer, daemon=True).start()

    # 进程内重连循环（2026-07-16 新增）：
    # 背景：公司 SASE 零信任客户端会间歇性把这条 WS 长连接拦截/掐断，SDK 自身的
    # 重连逻辑有时能扛过去，但观察到过它彻底放弃、ws.start() 停止工作却又不抛异常
    # 也不返回（线程死了但主线程卡住）、或者干脆返回但没重连的情况。
    # 以前的做法是靠外部 launchctl kickstart 整个重启进程——每次都要重新加载
    # reranker 模型、重建 KB 索引检查，白白浪费 8~10 秒。
    # 现在改成：只要 ws.start() 抛异常或正常返回（这两种情况都代表这条连接已经
    # 死透），就在同一个进程里直接 new 一个全新的 ws.Client 再 start()，完全不碰
    # 模型/索引，恢复速度快得多。真正卡死不返回的极端情况，交给上面的
    # _FailExitHandler 兜底退出整个进程，靠 launchd KeepAlive 拉起。
    _ws_attempt = 0
    while True:
        _ws_attempt += 1
        try:
            ws = lark.ws.Client(
                APP_ID,
                APP_SECRET,
                event_handler=handler,
                domain=lark.LARK_DOMAIN,
                log_level=lark.LogLevel.WARNING,
            )
            log.info("WS 长连接启动（第 %d 次尝试）", _ws_attempt)
            if _ws_attempt > 1:
                try:
                    question_log.send_admin_dm(
                        "⚡ HR Bot 已重连（第 %d 次）。如刚才消息未收到回复，请重新发送。" % _ws_attempt
                    )
                except Exception:
                    pass
            ws.start()
            log.warning("ws.start() 正常返回（SDK 内部重连已放弃），5 秒后在本进程内重建连接")
        except KeyboardInterrupt:
            log.info("Bot 已停止")
            break
        except Exception as e:
            log.error("WS 客户端异常退出: %s，5 秒后在本进程内重建连接", e, exc_info=True)
        _time.sleep(5)


if __name__ == "__main__":
    main()
