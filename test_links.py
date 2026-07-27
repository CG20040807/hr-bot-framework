#!/usr/bin/env python3
"""Link curation test — 验证推链接行为无乱推。"""
import sys, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
logging.disable(logging.CRITICAL)

import bot, kb

# Load _DOC_LINKS (normally done in main())
with open('kb_url_map.json') as f:
    _url_map = json.load(f)
for _fname, _info in _url_map.items():
    bot._DOC_LINKS[_fname] = (_info["label"], _info["url"])

def run(q):
    results = kb.search(q, with_scores=True)
    if not results:
        return [], ""
    scores = [s for s, _ in results]
    contexts = [c for _, c in results]
    reply = bot.answer(q, contexts[:5])
    reply = bot._strip_markdown(bot.strip_article_citations(bot.strip_preamble(reply)))
    links = bot._curate_links(contexts[:5], q, scores=scores[:5], reply=reply)
    return links, reply

# (query, expect_link: True=must have, False=must NOT have, None=don't care)
TEST_CASES = [
    ("绩效怎么打分",               True),
    ("绩效考核评级标准是什么",         True),
    ("Q3 OKR怎么设定",              False),
    ("季度绩效奖金怎么发",             True),
    ("年假有多少天",                 True),
    ("病假怎么申请",                 None),
    ("工资什么时候发",               False),
    ("薪资构成是什么",               False),
    ("差旅费报销流程",               True),
    ("餐饮补贴怎么报销",              None),   # 差旅政策里有餐费补贴条款，推差旅链接是正确的
    ("团建费用申请",                True),
    ("员工有哪些福利",               None),
    ("医疗保险怎么用",               False),
    ("新员工入职第一天做什么",         True),
    ("试用期多长时间",               None),
    ("离职流程是什么",               True),
    ("辞职需要提前多少天",            False),
    ("辞职信怎么写",                False),
    ("违反信息安全规定怎么处罚",        None),
    ("What is the annual leave policy", None),
    ("How to apply for reimbursement",  None),
    ("What benefits do employees get",  None),
    ("你好",                        False),
    ("帮我写一首诗",                  False),
]

print(f"{'Query':<30} {'推出链接':<50} {'状态'}")
print("─" * 95)
ok = fail = 0
for q, expect in TEST_CASES:
    links, _ = run(q)
    label_str = " | ".join(l[:35] for l, u in links)[:48] if links else "(无)"
    if expect is None:
        status = "✅"; ok += 1
    elif expect and links:
        status = "✅"; ok += 1
    elif not expect and not links:
        status = "✅"; ok += 1
    elif not expect and links:
        status = "❌ 乱推"; fail += 1
    else:
        status = "⚠️ 无链接"; fail += 1
    print(f"{q:<30} {label_str:<50} {status}")

print()
print(f"总计 {len(TEST_CASES)} 个查询，{ok} 通过，{fail} 乱推")
