"""
知识库检索 — BM25+IDF × 向量检索 双路混合召回，RRF 合并排序。

- 每 60 秒检查一次 KB 目录的最大 mtime，只有文件真正变化时才触发 build_index。
- BM25+IDF：关键词精准，速度快；向量：语义理解，处理无关键词查询。
- RRF 合并：两路排名倒数之和，兼顾两者优势，自动提权同时被两路召回的段落。
"""

import concurrent.futures
import datetime
import logging
import re
import threading
import time
from index_manager import build_index, search as _idx_search
from config import KB_DIR

log = logging.getLogger(__name__)

# 启动时在后台预加载向量模型和 Reranker（避免首次搜索阻塞）
try:
    import vector_store as _vs_preload
    _vs_preload.preload_async()
except Exception:
    pass

try:
    import reranker as _reranker_preload
    _reranker_preload.preload_async()
except Exception:
    pass

# ── 意图扩展：把自然语言表述映射到 KB 标准关键词 ────────────────────────────
# 解决"想请几天假"里 bigram 切出的"想请""请几"无法命中"请假""年假"的问题
_INTENT_MAP: list[tuple] = [
    # 请假 / 假期
    (re.compile(r"想请假|怎么请假|申请.{0,4}假|请假.{0,4}怎|请假.{0,4}流程|请几天|能不能请|请个假|休几天|想休息几天|不舒服.{0,4}请|有事.{0,4}请|请.{0,4}几号|假.{0,4}怎么申请"
                r"|how.{0,8}(apply|request|take).{0,8}leave|want.{0,8}(take|request).{0,8}(leave|time off)|applying for leave|leave application|time off request", re.IGNORECASE), ["请假", "假期", "申请"]),
    # 年假
    (re.compile(r"年假|年休|带薪.{0,2}假|假期.{0,4}剩|剩.{0,4}假|假期还有多少|多少天假"), ["年假", "年休假", "annual"]),
    # 病假
    (re.compile(r"生病.{0,4}请假|病假|身体不舒服.{0,4}假|发烧.{0,4}假|不舒服.{0,4}假"), ["病假", "sick", "医疗", "假期"]),
    # 婚假
    (re.compile(r"结婚.{0,4}假|婚假|结婚.{0,4}请假|要结婚"), ["婚假", "marriage", "假期"]),
    # 产假 / 陪产 (已有 LLM 边界，这里仍扩关键词让有内容时能命中)
    (re.compile(r"产假|怀孕.{0,4}假|生孩子.{0,4}假|哺乳"), ["产假", "158", "上海", "maternity", "生育"]),
    (re.compile(r"陪产|老婆.{0,4}生孩子|paternity"), ["陪产假", "男员工", "10天", "10 天", "生育"]),
    # 工资发放
    (re.compile(r"几号.{0,4}工资|工资.{0,4}几号|什么时候.{0,4}工资|工资.{0,4}时候|发薪|薪水.{0,4}时间|工资.{0,4}到账|发工资|发了工资|工资发了|发没发工资|工资.{0,4}(有没有|到了吗|到没到)"), ["薪资", "工资", "15日", "发放"]),
    # 试用期 / 转正
    (re.compile(r"试用期.{0,4}多久|试用期.{0,4}几个月|多久.{0,4}转正|转正.{0,4}怎么|怎么.{0,4}转正|转正.{0,4}流程|过了试用"), ["转正", "试用期", "probation"]),
    # 报销
    (re.compile(r"怎么报销|报销.{0,4}怎么|报销.{0,4}流程|申请报销|发票.{0,4}报销|出差.{0,4}报销|差旅.{0,4}怎么|怎么.{0,4}差旅"), ["报销", "差旅", "申请", "流程"]),
    # 离职
    (re.compile(r"想辞职|要辞职|不干了|想走|怎么离职|离职.{0,4}流程|提离职|辞职.{0,4}流程|离职.{0,4}手续|提前多久"), ["离职", "辞职", "流程", "手续"]),
    # 入职 / 新员工
    (re.compile(r"入职.{0,4}带什么|入职.{0,4}材料|入职.{0,4}手续|刚.{0,4}入职|新员工.{0,4}需要|刚来.{0,4}公司|需要准备什么"), ["入职", "新员工", "手续", "材料"]),
    # 打卡 / 补卡
    (re.compile(r"忘.{0,2}打卡|没打卡|漏打卡|补.{0,2}打卡|打卡.{0,4}忘|怎么补卡|没有打卡"), ["补卡", "打卡", "考勤"]),
    # 代打卡
    (re.compile(r"代打卡|帮.{0,4}打卡|替.{0,4}打卡|让.{0,4}人帮.{0,4}打卡"), ["代替", "代打", "严重违规", "解除", "考勤弄虚"]),
    # 考勤 / 迟到 / 旷工
    (re.compile(r"迟到.{0,4}怎么|迟到.{0,4}扣|迟到.{0,12}算|旷工|早退|考勤.{0,4}规定|几分钟.{0,4}迟到|\d+分钟.{0,8}迟|\d+分钟.{0,4}算"), ["迟到", "考勤", "旷工", "早退", "缺勤", "半日"]),
    # 加班 / 调休
    (re.compile(r"加班.{0,4}补贴|加班.{0,4}调休|加班.{0,4}工资|怎么.{0,4}调休|调休.{0,4}怎么|加班费"), ["加班", "调休", "overtime", "补贴"]),
    # 绩效 / KPI
    (re.compile(r"绩效.{0,4}怎么|怎么.{0,4}绩效|绩效考核|绩效.{0,4}评分|kpi|怎么评绩效|绩效.{0,4}几分"), ["绩效", "考核", "performance", "KPI"]),
    # 绩效申诉 / 绩效结果异议（单独处理，避免被信息安全申诉稀释）
    (re.compile(r"绩效.{0,6}申诉|绩效.{0,6}异议|不服.{0,4}绩效|绩效.{0,6}不服|对绩效.{0,4}有异|绩效申诉.{0,4}怎|绩效.{0,6}投诉"
                r"|appeal.{0,8}performance|disagree.{0,8}performance|performance.{0,8}appeal", re.IGNORECASE),
     ["绩效", "申诉", "LBPM", "窗口", "季度", "年度"]),
    # 社保 / 公积金
    (re.compile(r"社保.{0,4}怎么|怎么.{0,4}社保|公积金|五险一金|社保.{0,4}缴|保险.{0,4}缴"), ["社保", "保险", "公积金", "缴纳"]),
    # 福利
    (re.compile(r"有什么福利|公司.{0,4}福利|福利.{0,4}有哪些|待遇.{0,4}怎么样|有哪些.{0,4}福利|员工.{0,2}福利|福利.{0,2}制度|福利手册"
                r"|what benefits|company benefits|employee benefits|benefit package|company perks", re.IGNORECASE), ["福利", "待遇", "补贴", "benefit", "全球员工福利"]),
    # 居家 / 远程办公
    (re.compile(r"可以.{0,4}居家|居家.{0,4}办公|在家.{0,4}办公|在家.{0,4}上班|可以在家|远程.{0,4}办公"
                r"|wfh|work from home|remote work|work remotely|can i work from home", re.IGNORECASE), ["居家", "远程", "申请"]),
    # 远程响应时限
    (re.compile(r"多久.{0,4}(回复|响应|回消息)|没.{0,4}(回复|响应).{0,4}(算|多久|违纪)|响应.{0,4}时限|多久.{0,4}算违纪"
                r"|how.{0,8}(long|quickly).{0,8}(respond|reply)|response time", re.IGNORECASE),
     ["15分钟", "1小时", "响应", "回复", "未响应", "Lark"]),
    # 设备 / IT
    (re.compile(r"电脑.{0,4}坏了|设备.{0,4}坏了|电脑.{0,4}问题|电脑.{0,4}故障|电脑.{0,4}报修|IT.{0,4}问题"
                r"|computer broke|laptop (broken|not working|issue)|IT (issue|problem|request)|equipment (issue|broken)", re.IGNORECASE), ["IT", "设备", "固定资产", "报修"]),
    # 软件安装
    (re.compile(r"可以装什么软件|能安装什么|安装.{0,4}软件|软件.{0,4}安装|哪些软件可以"
                r"|what software can i install|install software|software allowed|approved software", re.IGNORECASE), ["软件", "IT", "设备", "信息安全"]),
    # 内推
    (re.compile(r"内推.{0,4}奖|奖励.{0,4}内推|推荐.{0,4}员工|员工.{0,4}推荐|内推.{0,4}多少"
                r"|employee referral|referral bonus|refer a friend|refer someone", re.IGNORECASE), ["内推", "奖励", "奖金", "referral"]),
    # 联系人 / 找谁
    (re.compile(r"找谁|联系谁|谁.{0,4}负责|谁管|问谁|找哪个|联系方式|怎么联系"
                r"|who (do i|should i|can i) (contact|ask|talk to|reach)|who handles|who is responsible|point of contact", re.IGNORECASE), ["联系", "负责人", "联系方式"]),
    # 信息安全 / 数据
    (re.compile(r"数据.{0,4}安全|信息.{0,4}安全|保密|泄露|隐私|公司.{0,4}数据"
                r"|data security|information security|data breach|confidential|privacy policy", re.IGNORECASE), ["信息安全", "保密", "数据"]),
    # 团建
    (re.compile(r"团建|团队活动|活动.{0,4}报销|团建.{0,4}费用"
                r"|team building|team event|team activity|team outing", re.IGNORECASE), ["团建", "活动", "费用"]),
    # 工资发放 — 英文
    (re.compile(r"when.{0,8}(get paid|paid|salary|paycheck|payday)|payday|payroll date|salary date", re.IGNORECASE), ["薪资", "工资", "15日", "发放"]),
    # 离职 — 英文
    (re.compile(r"(i want to|how do i|can i).{0,8}(quit|resign|leave the company)|notice period|resignation process|how to resign", re.IGNORECASE), ["离职", "辞职", "流程", "手续"]),
    # 打卡/补卡 — 英文
    (re.compile(r"forgot to (clock|punch|check) (in|out)|missed (clock|punch|check)|(clock|punch) in.{0,8}forgot|attendance (issue|problem)", re.IGNORECASE), ["补卡", "打卡", "考勤"]),
    # 转正/试用期 — 英文
    (re.compile(r"(how long|when).{0,8}probation|probation period|when do i (get confirmed|pass probation)|confirmation (letter|date)", re.IGNORECASE), ["转正", "试用期", "probation"]),
    # 报销 — 英文
    (re.compile(r"how.{0,8}(claim|get reimbursed|submit expenses)|expense (claim|reimbursement|report)|business travel (claim|reimbursement)", re.IGNORECASE), ["报销", "差旅", "申请", "流程"]),
    # 加班/调休 — 英文
    (re.compile(r"overtime (pay|compensation|allowance)|ot pay|comp.{0,4}(day|time|leave)|compensatory", re.IGNORECASE), ["加班", "调休", "overtime", "补贴"]),
    # 婚假 — 英文
    (re.compile(r"(getting married|marriage|wedding).{0,8}leave|wedding leave|marriage leave", re.IGNORECASE), ["婚假", "marriage", "假期"]),
    # 入职第一天 / 新人必做
    (re.compile(r"入职.{0,4}第一天|第一天.{0,4}入职|入职.{0,4}干什么|入职.{0,4}做什么|入职后.{0,6}|刚.{0,2}到职|入职必会|新人.*to.?do"
                r"|first day.{0,8}(work|job|joining)|what.{0,8}first day|joining.{0,8}checklist|new joiner|onboarding checklist|day one", re.IGNORECASE),
     ["入职", "新员工", "新人", "快速指引", "Welcome", "办公工具", "转正", "配置", "check", "self"]),
    # 培训录屏
    (re.compile(r"培训.{0,4}录屏|录屏.{0,4}培训|录屏学习|培训视频|training.{0,8}record|video.{0,8}training|training.{0,8}library|录屏库", re.IGNORECASE),
     ["培训", "录屏", "学习", "Training"]),
    # 劳动合同签署期限
    (re.compile(r"签合同.{0,6}(什么时候|多久|几天|最晚)|最晚.{0,4}签合同|入职.{0,6}签合同|合同.{0,6}(多久|什么时候).{0,4}签|什么时候.{0,4}签.{0,4}合同|合同.{0,4}签署.{0,4}期限"),
     ["报到之日", "一个月以内", "书面劳动合同", "订立"]),
    # 地址变更 / 搬家通知 HR
    (re.compile(r"搬家.{0,6}(通知|告诉|HR|多久)|换.{0,2}地址.{0,6}(HR|人力|通知|多久)|地址.{0,4}(变动|变更|更新).{0,6}(通知|多久|几天)|多久.{0,4}通知.{0,4}(HR|人力|地址)"),
     ["7日内", "书面通知", "人力资源部", "居住", "地址", "变动"]),
]


def _expand_query(query: str) -> str:
    """识别 HR 意图，把标准关键词拼到查询末尾，扩大召回范围。"""
    extra: list[str] = []
    for pattern, keywords in _INTENT_MAP:
        if pattern.search(query):
            extra.extend(keywords)
    if extra:
        return query + " " + " ".join(dict.fromkeys(extra))  # 去重保序
    return query

_cache_at: float = 0
_last_max_mtime: float = 0.0
CHECK_INTERVAL = 60  # 秒：检查 mtime 的间隔
last_rebuilt_at: float = 0.0  # 外部模块（bot.py）读取此值以感知 KB 更新
_lock = threading.Lock()  # 防止多线程并发重建


def _max_kb_mtime() -> float:
    try:
        return max((f.stat().st_mtime for f in KB_DIR.rglob("*.md")), default=0.0)
    except Exception:
        return _last_max_mtime  # 异常时保持上次已知值，不触发误重建


def _refresh_if_needed() -> None:
    global _cache_at, _last_max_mtime, last_rebuilt_at
    now = time.time()
    if now - _cache_at < CHECK_INTERVAL:
        return
    with _lock:
        # 双重检查：拿到锁后再判一次，防止并发重建
        if now - _cache_at < CHECK_INTERVAL:
            return
        _cache_at = now
        current_mtime = _max_kb_mtime()
        if current_mtime != _last_max_mtime:
            build_index()  # 增量扫，只处理变化的文件
            _last_max_mtime = current_mtime
            last_rebuilt_at = time.time()  # 通知 bot.py 清空对话历史


def mark_fresh() -> None:
    """bot.py 启动强制重建后调用，同步 mtime 缓存，避免首次查询触发冗余增量重建。"""
    global _cache_at, _last_max_mtime, last_rebuilt_at
    with _lock:
        _last_max_mtime = _max_kb_mtime()
        _cache_at = time.time()
        last_rebuilt_at = time.time()  # 让 bot.py 的答案缓存在重启后自动失效


_RRF_K = 60  # 标准 RRF 常数，防止头部排名过度压制尾部


def _rrf_merge(
    bm25_strs: list[str],
    vec_results: list[tuple],
    top_k: int,
) -> list[str]:
    """
    Reciprocal Rank Fusion：合并 BM25 和向量两路结果。
    score(d) = 1/(K + rank_bm25) + 1/(K + rank_vec)
    两路都召回的段落得分最高，只有一路的也能保留。
    """
    # 格式化：从 BM25 字符串里提取"内容指纹"用于去重
    # BM25 字符串格式：[来源: fname | 更新: date]\ncontent
    def _bm25_key(s: str) -> str:
        return s.split("\n", 1)[1][:120] if "\n" in s else s[:120]

    rrf: dict[str, float]  = {}
    store: dict[str, str]  = {}

    for rank, s in enumerate(bm25_strs):
        k = _bm25_key(s)
        rrf[k]   = rrf.get(k, 0.0) + 1.0 / (_RRF_K + rank + 1)
        store[k] = s

    # 向量结果需格式化成与 BM25 相同的字符串
    for rank, (score, fname, mtime, title, content) in enumerate(vec_results):
        k = content[:120]
        rrf[k]   = rrf.get(k, 0.0) + 1.0 / (_RRF_K + rank + 1)
        if k not in store:
            mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            store[k] = f"[来源: {fname} | 更新: {mtime_str}]\n{content}"

    sorted_keys = sorted(rrf, key=lambda k: -rrf[k])
    return [store[k] for k in sorted_keys[:top_k]]


_EXACT_PATTERNS = re.compile(
    r"年假.{0,4}(几天|多少天)|病假.{0,4}(几天|天数)|"
    r"(工资|薪资).{0,4}(几号|什么时候)|转正.{0,4}多久|"
    r"试用期.{0,4}多久|how many.{0,6}(days|leave)", re.IGNORECASE)


def _is_exact_query(query: str) -> bool:
    """精确事实类查询（年假几天、发薪几号等），跳过 LLM 改写和 HyDE，减少无效调用。"""
    return bool(_EXACT_PATTERNS.search(query))


_rewrite_client = None  # 延迟初始化，模块级复用，避免每次调用重建连接池
_rewrite_client_lock = threading.Lock()


def _hyde_query(query: str) -> str:
    """生成假设答案用于 HyDE 向量检索，失败静默返回空字符串。"""
    global _rewrite_client
    try:
        if _rewrite_client is None:
            with _rewrite_client_lock:
                if _rewrite_client is None:
                    from openai import OpenAI
                    from config import LLM_BASE_URL, LLM_API_KEY
                    _rewrite_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        from config import LLM_MODEL
        resp = _rewrite_client.chat.completions.create(
            model=LLM_MODEL, temperature=0, max_tokens=150,
            messages=[{"role": "user", "content":
                       "根据以下HR问题，写约100字的假设答案（无需完全准确，涵盖相关政策概念即可）：\n" + query}])
        return resp.choices[0].message.content.strip()
    except Exception:
        return ""


def _llm_rewrite(query: str) -> list[str]:
    """用 LLM 把口语问题改写成 4 个不同角度的检索变体，提升多样性召回：
    - 2 个规范 HR 术语改写（精确命中）
    - 1 个 HR 手册章节标题风格（匹配 BM25 标题行）
    - 1 个 Step-Back 抽象类别（宽泛匹配，防止具体措辞不一致导致漏召回）
    失败时静默返回空列表。"""
    global _rewrite_client
    try:
        if _rewrite_client is None:
            with _rewrite_client_lock:
                if _rewrite_client is None:
                    from openai import OpenAI
                    from config import LLM_BASE_URL, LLM_API_KEY
                    _rewrite_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        from config import LLM_MODEL
        resp = _rewrite_client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    "把下面这个HR问题改写成4行检索查询，每行一个，不加序号：\n"
                    "第1-2行：用规范人力资源术语改写，不超过15字\n"
                    "第3行：改写为HR制度手册的章节标题风格，不超过10字\n"
                    "第4行：提炼为更宽泛的政策类别（Step-Back），不超过8字\n"
                    "问题：" + query
                ),
            }],
        )
        lines = resp.choices[0].message.content.strip().splitlines()
        return [l.strip() for l in lines if l.strip() and l.strip() != query][:4]
    except Exception:
        return []


def _search_raw(expanded: str, top_k: int, min_score: int) -> tuple:
    """单次双路检索，返回 (bm25_results, vec_results)。"""
    bm25 = _idx_search(expanded, top_k=top_k, min_score=min_score)
    try:
        import vector_store as _vs
        vec = _vs.search(expanded, top_k=top_k)  # 修复：使用 expanded 而非原始 query
    except Exception:
        vec = []
    return bm25, vec


def has_intent(query: str) -> bool:
    """True if _INTENT_MAP matched this query (intent is clear, even with 0 KB hits)."""
    return _expand_query(query) != query


def _reorder_lost_in_middle(chunks: list[str]) -> list[str]:
    """最高分置首，次高分置尾，其余填中间——LLM 对首尾注意力最强。"""
    if len(chunks) <= 2:
        return chunks
    return [chunks[0]] + chunks[2:] + [chunks[1]]


def search(query: str, top_k: int = 6, min_score: int = 1,
           history: list = None, with_scores: bool = False):
    """BM25+IDF × 向量 双路混合检索 + LLM 并行改写 + HyDE，RRF 合并 + Reranker 精排。

    with_scores=True 时返回 [(reranker分数, chunk)]（分数不可用时为 None）；
    默认 False 返回 list[str]，与所有现有调用方兼容。"""
    _refresh_if_needed()
    expanded = _expand_query(query)

    # 多轮检索上下文：把上一轮用户问题（最多 50 字）拼入检索词，避免追问跑偏
    if history:
        for msg in reversed(history[-4:]):
            if msg.get("role") == "user" and msg["content"] != query:
                expanded += " " + msg["content"][:50]
                break

    is_exact = _is_exact_query(query)

    if is_exact:
        # 精确事实查询：跳过 LLM 改写和 HyDE，直接双路检索，提高 min_score 降噪
        bm25, vec = _search_raw(expanded, top_k, min_score * 2)
        rewrites = []
        hyde_doc = ""
        log.info("[router] exact query, skip rewrite+HyDE")
    else:
        # 主检索 / LLM 改写 / HyDE 三路并行，不增加用户感知延迟
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            main_fut    = pool.submit(_search_raw, expanded, top_k * 2, min_score)
            rewrite_fut = pool.submit(_llm_rewrite, query)
            hyde_fut    = pool.submit(_hyde_query, query)
            bm25, vec = main_fut.result()
            rewrites  = rewrite_fut.result()
            hyde_doc  = hyde_fut.result()

    # 对每个改写词组追加检索，候选池与主检索对齐（top_k*2），提高召回上限
    for rw in rewrites:
        rw_exp = _expand_query(rw)
        rb, rv = _search_raw(rw_exp, top_k * 2, min_score)
        bm25 = bm25 + rb
        vec  = vec  + rv

    # HyDE：用假设答案向量检索（passage: 前缀），补充语义召回
    if hyde_doc:
        try:
            import vector_store as _vs
            hyde_results = _vs.search(hyde_doc, top_k=top_k, is_hyde=True)
            vec = vec + hyde_results
        except Exception:
            pass

    if not vec:
        candidates = list(dict.fromkeys(bm25))[:top_k * 2]
    else:
        candidates = _rrf_merge(bm25, vec, top_k * 2)  # 多取候选，Reranker 再筛

    # Reranker 精排：用 expanded（含 KB 原词）做窗口选取，用原始 query 做相关性打分
    try:
        import reranker as _rr
        scored = _rr.rerank(query, candidates, window_query=expanded, return_scores=True)
    except Exception:
        scored = [(None, c) for c in candidates]

    # 信用加权：用户 👍/👎 反馈累计的文档信用分作为乘性权重作用于 reranker 分数，
    # 被踩过的来源自动降权（见 credit_store.py；只影响排序，不改动答案生成）
    import credit_store as _cs
    if _cs.enabled():
        scored = _cs.apply(scored)

    # Lost-in-the-Middle 修复：最高分置首，次高分置尾，LLM 对首尾注意力最强
    # （分数随 chunk 一起重排，供链接推送按相关性分级使用）
    scored = _reorder_lost_in_middle(scored[:top_k])
    if with_scores:
        return scored
    return [c for _, c in scored]
