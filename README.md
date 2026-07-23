# Jify


Jify Agent（Self-evolving harness Agent）是一款通用智能体，运行在您自己的设备上的个人 AI 助手。它可以帮你完成您需要的编程任务、安全排查、代码分析、CTF


https://github.com/user-attachments/assets/68095fbc-9af2-4019-b8d9-512a754b2d6d


## 安装

```bash
# 从源码安装
git clone https://github.com/s1mple-top/Jify.git
cd Jify
pip install -e .
jify

# 或使用 uv（推荐）
git clone https://github.com/s1mple-top/Jify.git
cd Jify
uv sync
source .venv/bin/activate
jify
```

依赖 Python >= 3.11。

## 快速开始

```bash
# 启动 CLI 交互式对话
jify

# 单次提问（非交互模式）
jify -q "介绍下你自己"

# 启动 Web UI 
jify gateway --port 9090

# 开启think流式输出，增强使用体感
jify --think-stream
```

首次运行会自动创建 `~/.jify/` 目录并生成默认配置。

你可以在任何目录下执行jify启动jify

## CLI 命令

在对话中输入 `/` 可触发自动补全：

| 命令 | 说明 |
|------|------|
| `/model <name>` | 切换模型 |
| `/resume <id>` | 恢复历史对话 |
| `/sessions` | 列出最近对话会话 |
| `/clear` | 清除对话历史 |
| `/help` | 显示帮助信息 |
| `/hook` | 显示已加载的 Hook |
| `/skill` | 列出可用 Skill |
| `/jify` | 分析当前工作目录(cwd)下的项目，生成 Jify.md |
| `/exit` | 退出程序 |


### 自进化引擎

Jify 不是一成不变的工具，它会随着你的使用持续「生长」：                                             

 • 越聊越懂你：用得越久，它就越像「你自己」。                                                                                             
 • 踩过的坑不再踩：每次对话中的关键决策、踩坑经验都会被Jify自沉淀，后续遇到相似场景Jify会自动避坑。                                                                                       
 • 越用越顺手：Jify 会主动识别并提议固化为Skill，你只需点个头，下次它就能一键搞定。


### webUI

```bash
# 启动网关
jify gateway --port 9090
```

### 插件系统

通过 Hook 机制扩展 Agent 行为。插件放置在 `~/.jify/plugins/`，支持的生命周期钩子包括 `before_prompt_build`、`after_prompt_build`、`llm_input`、`before_api_call`、`after_api_call`、`before_tool_call`、`after_tool_call`、`llm_output` 等。

亦可通过插件系统注册自定义Tool

### MCP 支持

内置 MCP (Model Context Protocol) 客户端，通过 `~/.jify/mcp_servers.json` 配置文件集成外部工具服务。

### 模型配置

初次启动会自动构建 ~/.jify 目录，请在其下的 config.yaml 里配置需要的模型

## License

MIT
