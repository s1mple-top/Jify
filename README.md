# Jify


Jify（Self-evolving harness）是一款运行在您自己的设备上的个人 AI 助手。它可以帮你完成您需要的编程任务、安全排查


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
| `/jify` | 分析当前项目，生成 Jify.md |
| `/exit` | 退出程序 |


### 自进化引擎

- **用户画像提取**：自动识别用户偏好（代码风格、交互习惯等），注入系统提示词
- **经验提取**：从多轮对话中总结最佳实践和踩坑记录，持久化到 `experiences.json`
- **Skill 检测**：识别可复用的行为模式，自动生成 Skill 草稿，经用户确认后沉淀


### webUI

```bash
# 启动网关
jify gateway --port 9090
```

### 插件系统

通过 Hook 机制扩展 Agent 行为。插件放置在 `~/.jify/plugins/`，支持的生命周期钩子包括 `before_prompt_build`、`after_prompt_build`、`llm_input`、`before_api_call`、`after_api_call`、`before_tool_call`、`after_tool_call`、`llm_output` 等。

### MCP 支持

内置 MCP (Model Context Protocol) 客户端，通过 `mcp_servers.json` 配置文件集成外部工具服务。


## License

MIT
