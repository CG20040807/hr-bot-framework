# HR 问答 Bot（飞书）

面向企业员工的 HR 智能问答机器人：基于 RAG 检索本地知识库，**原文转述**制度内容并推送来源链接，不让大模型自由发挥，从机制上杜绝幻觉。

## 核心特性

- **原文转述 + 链接推送**：答案严格来自知识库原文，附来源文档链接，可溯源
- **RAG 检索**：向量召回 + BGE Reranker 精排，按章节命中
- **中英双语**：自动识别提问语言，双语知识库
- **流式卡片回复**：飞书卡片边生成边更新，带 👍/👎 反馈按钮
- **多轮上下文**：支持追问（"那这个呢"）
- **自愈闭环**：答不出时静默记录 → 每日日报汇总高频/未解答问题 → 管理员确认后补充知识库
- **生产级稳定性**：进程内重连 + 失败保险丝 + 外部看门狗，作息感知值守

## 快速开始

```bash
# 1. 安装依赖
pip install lark-oapi openai numpy torch transformers sentencepiece

# 2. 配置（填入飞书应用凭证 / LLM 网关 / 管理员 open_id）
cp config.example.py config.py && vi config.py

# 3. 放入知识库（.md 文件，每个文件一个主题）
cp 你的制度文档.md kb/

# 4. 启动
python3 bot.py
```

## 目录结构

| 文件 | 职责 |
|---|---|
| `bot.py` | 主程序：飞书 WS 长连接、消息分发、卡片 UI、重连与值守 |
| `llm.py` | LLM 调用：Prompt 约束、守卫、流式生成、置信自查 |
| `kb.py` / `index_manager.py` / `vector_store.py` / `reranker.py` | 知识库索引与检索链路 |
| `question_log.py` | 问题记录（sqlite）、每日日报、反馈收集 |
| `kb_healer.py` | 知识库自愈建议（管理员确认制） |
| `lark_sync.py` | 从飞书 wiki/docx 自动同步知识库内容 |
| `eval_kb.py` | 离线评测：用例集跑准确率，改动前先验证不回归 |
| `watchdog.sh` | 外部看门狗（launchd 定时调用，作息感知） |

## 运维

- `launchd` 保活（KeepAlive），`watchdog.sh` 每分钟巡检（仅工作日白天）
- 管理员在飞书对 bot 说：`重载知识库` / `同步知识库`
- 改代码前先跑 `python3 eval_kb.py` 确认评测不回归
