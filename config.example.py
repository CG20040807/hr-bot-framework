"""配置模板。

使用方法：复制本文件为 config.py，填入真实值即可运行。
config.py 已被 .gitignore 排除，永远不会被提交上传。
也可全部用环境变量提供（环境变量优先于文件内写的值）。
"""
import os
from pathlib import Path

_env = os.environ

# 飞书自建应用凭证（开放平台 → 应用凭证）
APP_ID     = _env.get("LARK_APP_ID", "cli_在此填入")
APP_SECRET = _env.get("LARK_APP_SECRET", "在此填入")

# LLM 网关（任何 OpenAI 兼容接口均可：公司网关 / OpenAI / 其他）
LLM_BASE_URL = _env.get("LLM_BASE_URL", "https://your-llm-gateway.example.com") + "/v1"
LLM_API_KEY  = _env.get("LLM_API_KEY", "在此填入")
LLM_MODEL    = _env.get("LLM_MODEL", "claude-sonnet")

# 管理员的飞书 open_id（ou_ 开头）：用于管理员指令（KB 重载/同步）
# 和每日问题日报的接收人。获取方式：飞书开放平台 → 用户 ID 查询。
ADMIN_OPEN_ID    = _env.get("HR_ADMIN_OPEN_ID", "ou_在此填入")
REPORT_RECIPIENT = _env.get("HR_REPORT_RECIPIENT", ADMIN_OPEN_ID)

# 项目根目录（docs/ 索引缓存相对此路径）
PROJECT_DIR = Path(__file__).parent
DOCS_DIR    = PROJECT_DIR / "docs"

# 知识库目录：把制度文档的 .md 文件放进去即可
KB_DIR = PROJECT_DIR / "kb"
