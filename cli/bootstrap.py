"""Jify 启动引导 —— 确保 ~/.jify 目录结构存在。"""

from pathlib import Path

JIFY_HOME = Path.home() / ".jify"

DEFAULT_CONFIG_YAML = """\
# Jify Agent 配置文件
#
# 多模型配置 —— 在 models 列表中声明所有可用模型，每个模型包含完整的
# provider / model / base_url / api_key。然后通过 /model <name> 切换。
#
# 如果 models 为空，则使用顶层 provider/model/base_url/api_key 作为默认配置。

# 多模型配置（推荐）
# models:
#   - name: gpt-4o
#     provider: openai
#     model: gpt-4o
#     base_url: https://api.openai.com/v1
#     api_key: "sk-xxx"
#
#   - name: claude
#     provider: anthropic
#     model: claude-sonnet-4-20250514
#     base_url: https://api.anthropic.com
#     api_key: "sk-ant-xxx"
#
#   - name: deepseek
#     provider: openai
#     model: deepseek-v4-pro
#     base_url: https://api.deepseek.com/v1
#     api_key: "sk-xxx"

# 默认模型配置（models 为空时生效）
# provider: openai
# model: gpt-4o
# base_url: https://api.openai.com/v1
# api_key: "sk-xxx"

# 运行时参数
# 最大Loop轮数
max_iterations: 100
# 工具调用延迟，一般为0最好，不用延迟
tool_delay: 0.0
max_workers: 8
tool_timeout: 20.0

# 网关管理员 token
admin_token: "jify-admin-2024"

# 插件系统
plugins_dir: ~/.jify/plugins
enabled_plugins:
  - hello_world
"""

DEFAULT_MCP_SERVERS_JSON = """\
{
  "servers": [
    {
      "name": "example-stdio-server",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": {},
      "timeout": 30,
      "enabled": false
    },
    {
      "name": "example-sse-server",
      "transport": "sse",
      "url": "http://localhost:8080/sse",
      "timeout": 60,
      "enabled": false
    }
  ]
}
"""


def ensure_jify_home() -> None:
    """确保 ~/.jify 目录及其基础文件结构存在。"""
    if JIFY_HOME.exists():
        return

    JIFY_HOME.mkdir(parents=True, exist_ok=True)

    # 写入默认配置文件
    (JIFY_HOME / "config.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")

    # 写入默认 MCP 服务器配置
    (JIFY_HOME / "mcp_servers.json").write_text(DEFAULT_MCP_SERVERS_JSON, encoding="utf-8")

    # 创建子目录
    for sub in ("plugins", "self_evolution", "skills"):
        (JIFY_HOME / sub).mkdir(exist_ok=True)
