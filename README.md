# Jify


Jify Agent（Self-evolving harness Agent）是一款通用智能体，运行在您自己的设备上的个人 AI 助手。它可以帮你完成您需要的编程任务、安全排查（漏洞挖掘【黑白盒分析】）、代码分析、CTF、渗透测试

Jify Agent (Self-evolving harness Agent) is a general-purpose intelligent agent — a personal AI assistant that runs on your own device. It helps you with programming tasks, security audits (vulnerability discovery via black-box/white-box analysis), code analysis, CTF challenges, and penetration testing.

Star ++


https://github.com/user-attachments/assets/68095fbc-9af2-4019-b8d9-512a754b2d6d


## 安装
## Installation

```bash
# 从源码安装
# Install from source
git clone https://github.com/s1mple-top/Jify.git
cd Jify
pip install -e .
jify

# 或使用 uv（推荐）
# Or use uv (recommended)
git clone https://github.com/s1mple-top/Jify.git
cd Jify
uv sync
source .venv/bin/activate
jify
```

依赖 Python >= 3.11。
Requires Python >= 3.11.

## 快速开始
## Quick Start

```bash
# 启动 CLI 交互式对话
# Start interactive CLI chat
jify

# 单次提问（非交互模式）
# One-shot query (non-interactive)
jify -q "介绍下你自己"

# 启动 Web UI
# Launch the Web UI
jify gateway --port 9090

# 开启think流式输出，增强使用体验
# Enable streaming think output for a better experience
jify --think-stream

# 启用 exec 命令白名单安全模式，拦截危险命令
# Enable exec command allowlist safe mode to block dangerous commands
jify --safe-exec

# 查看jify版本
# Check the jify version
jify --version
```

首次运行会自动创建 `~/.jify/` 目录并生成默认配置。
On first run, `~/.jify/` is created automatically with a default config.

你可以在任何目录下执行jify启动jify
You can run `jify` from any directory to start it.

## CLI 命令
## CLI Commands

在对话中输入 `/` 可触发自动补全：
Type `/` in a chat to trigger autocompletion:

| 命令 | 说明 |
|------|------|
| `/model <name>` | 切换模型 |
| `/resume <id>` | 恢复历史对话 |
| `/sessions` | 列出最近对话会话 |
| `/clear` | 清除对话历史 |
| `/help` | 显示帮助信息 |
| `/hook` | 显示已加载的 Hook |
| `/skill` | 列出可用 Skill |
| `/learn` | 学习当前对话，沉淀为 Skill |
| `/jify` | 分析当前工作目录(cwd)下的项目，生成 Jify.md |
| `/exit` | 退出程序 |

| Command | Description |
|------|------|
| `/model <name>` | Switch model |
| `/resume <id>` | Resume a history session |
| `/sessions` | List recent sessions |
| `/clear` | Clear chat history |
| `/help` | Show help |
| `/hook` | Show loaded hooks |
| `/skill` | List available skills |
| `/learn` | Learn from current chat and distill into a Skill |
| `/jify` | Analyze the project in cwd and generate Jify.md |
| `/exit` | Exit |


### 自进化引擎
### Self-evolving Engine

Jify 不是一成不变的工具，它会随着你的使用持续「生长」：

Jify is not a fixed tool — it keeps "growing" with your usage:

 • 越聊越懂你：用得越久，它就越像「你自己」。

 • It understands you better over time: the longer you use it, the more it feels like "you".

 • 踩过的坑不再踩：每次对话中的关键决策、踩坑经验都会被Jify自沉淀，后续遇到相似场景Jify会自动避坑。

 • Avoid pitfalls: key decisions and hard-won lessons are automatically distilled, so Jify avoids the same traps in similar situations. 

• 越用越顺手：Jify 会主动识别并建议固化,为Skill，你只需点个头，下次它就能一键搞定。

 • It gets smoother: Jify proactively identifies and suggests consolidating behaviors into Skills — just approve, and next time it's one-click.


### 漏洞挖掘
### Vulnerability Discovery

Jify 针对漏洞挖掘 / 安全审计提供专项能力，形成「发现 → 验证 → 沉淀 → 复用」闭环。

Jify offers dedicated capabilities for vulnerability discovery / security audits, forming a "discover → verify → distill → reuse" closed loop.


### webUI

```bash
# 启动网关
# Start the gateway
jify gateway --port 9090
```

### 插件系统
### Plugin System

通过 Hook 机制扩展 Agent 行为。插件放置在 `~/.jify/plugins/`，支持的生命周期钩子包括 `before_prompt_build`、`after_prompt_build`、`llm_input`、`before_api_call`、`after_api_call`、`before_tool_call`、`after_tool_call`、`llm_output` 等。

Extend Agent behavior via hooks. Plugins live in `~/.jify/plugins/`, supporting lifecycle hooks such as `before_prompt_build`, `after_prompt_build`, `llm_input`, `before_api_call`, `after_api_call`, `before_tool_call`, `after_tool_call`, `llm_output`, etc.

亦可透过插件系统注册自定义Tool
Custom tools can also be registered through the plugin system.

### MCP 支持
### MCP Support

内置 MCP (Model Context Protocol) 客户端，通过 `~/.jify/mcp_servers.json` 配置文件集成外部工具服务。

A built-in MCP (Model Context Protocol) client integrates external tool services via the `~/.jify/mcp_servers.json` config file.

### 模型配置
### Model Configuration

首次启动会自动构建 ~/.jify 目录，请在其下的 config.yaml 里配置需要的模型

On first launch, the `~/.jify` directory is built automatically; configure your models in `config.yaml` under it.

## License
