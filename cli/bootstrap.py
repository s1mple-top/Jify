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

# 自进化引擎模型选择，需要配置
#SelfEvolutionModel: gpt-4o
# 用户画像提取间隔轮数
#SelfEvolutionTurn: 8

# 运行时参数
# 最大Loop轮数
max_iterations: 100
# 工具调用延迟，一般为0最好，不用延迟
tool_delay: 0.0
max_workers: 8
tool_timeout: 20.0

# 网关管理员 token
admin_token: "jify-admin-2026"

# 插件系统
plugins_dir: ~/.jify/plugins
enabled_plugins:
  - hello_world
  - hook_demo
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
      "name": "amap-maps",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y","@amap/amap-maps-mcp-server"],
      "env": {
        "AMAP_MAPS_API_KEY": "xxx"
      },
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

HELLO_WORLD_PLUGIN_JSON = """\
{
  "name": "hello_world",
  "version": "1.0.0",
  "description": "示例插件：演示 Jify 插件系统的基本用法",
  "author": "s1mple",
  "type": ["tool"]
}
"""

HELLO_WORLD_TOOLS_PY = """\
# -*- coding: utf-8 -*-
\"\"\"hello_world 插件 — 演示如何用 @register_tool 注册新工具\"\"\"

from tools.registry import register_tool


@register_tool(
    name="hello_world",
    description="A demo tool from the hello_world plugin. Returns a greeting with the current time.",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name to greet (default: 'World')"
            }
        },
        "required": [],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def hello_world(name: str = "World") -> str:

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"Hello, {name}! (from hello_world plugin @ {now} created by XY)"
"""

HOOK_DEMO_PLUGIN_JSON = """\
{
  "name": "hook_demo",
  "version": "1.0.0",
  "description": "演示 hook 插件系统：在所有 8 个 hook 点打印日志",
  "author": "s1mple",
  "type": ["hook"]
}
"""

HOOK_DEMO_HOOKS_PY = """\
# -*- coding: utf-8 -*-

'''
Hook 注册函数
'''


def before_prompt_build(context):
    \"\"\"system prompt 构建前触发\"\"\"
    # print("before_prompt_build")
    return context


def after_prompt_build(context):
    \"\"\"messages 组装完毕触发\"\"\"
    # print("after_prompt_build")
    pass


def llm_input(context):
    \"\"\"每轮发送给模型前触发\"\"\"
    # print("llm_input")
    pass


def before_api_call(context):
    \"\"\"API 调用前触发\"\"\"
    # model = context.get("model", "?")
    # msg_content = len(context.get("api_messages", []))
    # msg_content = context.get("api_messages", [])
    # print("before_api_call",msg_content)
    pass


def after_api_call(context):
    \"\"\"API 调用后触发\"\"\"
    # print("after_api_call",model)
    pass


def before_tool_call(context):
    \"\"\"工具执行前触发\"\"\"
    # print("before_tool_call",name)
    # return {"block":True,"reason":"此工具调用被拦截"}
    pass


def after_tool_call(context):
    \"\"\"工具执行后触发\"\"\"
    # name = context.get("tool_name", "?")
    # result = context.get("result")
    # error = context.get("error", False)
    # status = "ERROR" if error else "OK"
    # print("after_tool_call",name,result)
    pass


def llm_output(context):
    \"\"\"最终输出前触发\"\"\"
    # resp_len = len(context.get("final_response", ""))
    # print("llm_output",resp_len)
    pass
"""

def _write_default_plugins() -> None:
    """在 ~/.jify/plugins/ 下创建内置默认插件。"""
    plugins_dir = JIFY_HOME / "plugins"

    # hello_world
    hw_dir = plugins_dir / "hello_world"
    hw_dir.mkdir(exist_ok=True)
    (hw_dir / "plugin.json").write_text(HELLO_WORLD_PLUGIN_JSON, encoding="utf-8")
    (hw_dir / "tools.py").write_text(HELLO_WORLD_TOOLS_PY, encoding="utf-8")

    # hook_demo
    hd_dir = plugins_dir / "hook_demo"
    hd_dir.mkdir(exist_ok=True)
    (hd_dir / "plugin.json").write_text(HOOK_DEMO_PLUGIN_JSON, encoding="utf-8")
    (hd_dir / "hooks.py").write_text(HOOK_DEMO_HOOKS_PY, encoding="utf-8")


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

    # 写入内置默认插件
    _write_default_plugins()
