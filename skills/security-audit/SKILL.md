---
name: security-audit
description: 对 Python Web/Agent 项目进行系统化安全审计。覆盖架构理解、多维漏洞扫描、PoC 编写、本地部署与黑盒实测验证、依赖链追踪的全流程方法论。适用于 AI Agent 框架、FastAPI/Django 应用、多组件架构项目。
license: MIT
---

# Security Audit Skill

对 Python Web/Agent 项目进行系统化安全审计的方法论。

## 审计阶段

### 1. 架构理解（先读后审）
- 先通读项目顶层目录结构（`find / tree / ls`），理解包边界
- 识别核心执行链路（Agent 生命周期、请求处理链、配置加载流程）
- 输出架构文档，标注关键模块和数据流
- 原则：先读后审，绝不盲猜

### 2. 多维漏洞扫描（按攻击面分类）

| 攻击面 | 检查要点 |
|--------|---------|
| 命令注入 | `subprocess.Popen(shell=True)`、`os.system()`、`exec()` / `eval()` — 用户/LLM 可控参数是否进入 |
| 反序列化 | `pickle.load()` / `yaml.unsafe_load()` / `marshal.load()` — 数据来源是否可信 |
| 路径穿越 | `os.path.join()` / `Path.joinpath()` — 用户输入是否直接拼文件路径 |
| SSRF | `httpx.get()` / `requests.get()` / `urllib` — URL 是否用户/LLM 可控 |
| 认证缺失 | REST API 端点是否有 auth middleware，管理面是否裸奔 |
| 信息泄露 | API key 存储方式、日志是否输出敏感数据、错误消息是否返回堆栈 |
| 动态代码加载 | `importlib.import_module()` / `exec_module()` / `__import__()` — module 路径是否可控 |
| 文件上传 | 文件名校验、类型校验、存储路径是否在沙箱内 |

### 3. 深度探查高危漏洞
- 对每个 P0 漏洞：追踪完整数据流（输入 → 中间处理 → 危险函数）
- 写 PoC（curl 命令或 Python 脚本，标注触发的源码行号）
- 分析攻击链：组合多个中低危漏洞达成高危效果

### 4. 依赖链追踪
- 检查 `requirements.txt` / `pyproject.toml` / `setup.cfg`
- 定位 `site-packages` 中的关键依赖源码
- 不能停在项目边界——依赖里的漏洞也是漏洞
- 重点关注：文件上传处理、配置加载、路由注册

### 5. 本地部署 + 阶梯式实测验证（必须执行）

**核心原则：静态分析只是线索，未经验证的漏洞一律视为「疑似」，不得写入正式报告。**
每个嫌疑点必须推进到能落地的一级验证，按「成本从低到高」逐级尝试：白盒单元级（code_sandbox）→ 黑盒 HTTP（vuln_verify）。能起服务走黑盒最接近真实，但很多场景（第三方项目依赖重、起服务成本高、sink 可独立驱动）用白盒单元级更高效且足以证明确实可被驱动。

#### 三级验证总览

| 层级 | 工具 | 方式 | 何时用 | 判定证据 |
|------|------|------|--------|---------|
| **L1 静态** | `read_file` / `static_analysis` / grep | 源码阅读 + AST + 模式匹配 | 所有嫌疑点（必经） | 仅有代码形状，只算「疑似」 |
| **L2 白盒单元级** | `code_sandbox` | 抽 sink 片段 + mock 请求对象 + 语言解释器直接执行 | 能定位到可独立执行的 sink，且不想/不能起服务 | 运行时特征（uid= / SQL 报错 / 敏感文件内容） |
| **L3 黑盒** | `vuln_verify` | 本地起完整服务，从 HTTP 打 payload | 服务能跑起来、链路能打通 | 回显 / 时序 / 回连 / 状态码差异 |

**优先级建议**：先看 L2 是否可行（成本最低、无需起服务）；L2 无法驱动（环境缺失/依赖严重）再评估 L3 起服务。两者任一拿到确定性特征即视为 validate 通过；L2 的可靠度略低于真实 HTTP 链路，写入报告时注明验证方式。

#### L2 白盒单元级动态验证（code_sandbox）

**思想**：把「source→sink 链路」中的上游 HTTP 层用 mock 请求对象代替，直接对可独立执行的最小 sink 片段喂恶意输入，看其在真实解释器里的运行反应。**mock 的是「输入从哪来」，真实的是「输入进来之后会发生什么」**——sink 目标和中间过滤逻辑都是真实执行，能直接证伪「被静态误判但实际有过滤保护」的点。

**执行步骤**：
1. Analysis 已定位到 sink，确定其消费的输入对象（`$_GET` / `request.args` / `req.query` / `params`）
2. 抽出到 sink 为止的最小可执行代码片段（含中间过滤逻辑）
3. 调 `code_sandbox(language, code, mock_params)`，工具自动注入对应语言的 mock 请求对象，用解释器 `-c/-r/-e` 或临时文件执行
4. 看结构化返回 `{exit_code, stdout, stderr, duration_ms, timed_out, error}` 判定

**按漏洞类型的 L2 单元级判定特征**

| 漏洞类型 | 喂养的 mock_params | 判定特征（运行时输出） |
|---------|-------------------|----------------------|
| 命令注入 (rce) | `{"cmd": "id"}` / `{"cmd": "echo VULN_TEST"}` | stdout 含 `uid=` / `root` / `www-data`，或测试串被原样回显 |
| SQL 注入 (sqli) | `{"id": "1' OR '1'='1"}` | 开启报错后 stdout/stderr 含 `SQL syntax` / `PostgreSQL.*ERROR` / `sqlite3.OperationalError` |
| XSS | `{"q": "<script>alert(1)</script>"}` | payload 原样反射到输出（未 HTML 编码） |
| 路径穿越 | `{"file": "../../../etc/passwd"}` | 输出含 `/etc/passwd` 的 `root:.*:0:0:` 格式 |
| SSTI | `{"name": "{{7*7}}"}` | 输出出现 `49`（模板表达式被求值） |

**L2 判定与误判规避**
- **漏洞成立**：stdout/stderr 出现预期运行时特征，且该特征可归因于恶意输入（不是环境自身输出）
- **环境缺失（env_gap）**：`import` 失败 / 连库失败 / 解释器缺失 → **不得标记 confirmed**，降级为「疑似」，回退到 L3 或 `out of scope`
- **防御生效**：输入被过滤/编码（payload 未透出）→ 正确判为**不可利用**，这恰恰是单元级验证的价值
- **环境噪音**：解释器启动警告（如 PHP 缺扩展）混入 stderr 属正常，忽略噪音只依赖 stdout 的漏洞特征 <-> 所以优先解析 stdout，stderr 仅作参考

**本地部署目标环境**
- 优先用项目自带方式启动：`uvicorn app:app` / `python main.py` / `docker-compose up -d`
- 先读 README / 启动脚本，确定启动命令、监听端口、外部依赖
- 外部依赖（数据库/缓存）用 docker 或本地 mock 隔离
- 记录启动命令、监听地址、端口，供后续验证复用

**按漏洞类型的黑盒验证手法（统一走 vuln_verify，payload 按注入点构造并 URL 编码嵌入 target）**

| 漏洞类型 | 黑盒验证手法 |
|--------|-----------|
| 命令注入 (rce) | 注入 `; id` / `; uname -a` 等无副作用命令，观察回显；注入 `; sleep 3` 对比响应时延 |
| SQL 注入 (sqli) | 报错注入 `'` 看报错回显；布尔盲注 `1' AND '1'='1` vs `1' AND '1'='2` 对比响应差异；时间盲注 `SLEEP(3)` 测时延 |
| XSS (xss) | 注入 `<script>alert(1)</script>` 或 `<img src=x onerror=alert(1)>`，确认响应反射；必要时在渲染端/无头浏览器确认执行 |
| SSRF (ssrf) | target/payload 中写 `{{CALLBACK}}` 占位符，工具自动替换为本地监听地址，确认收到回连 |
| 路径穿越 (path_traversal) | 请求 `../../../etc/passwd`，确认响应包含文件内容（root 行等） |
| 反序列化 (deserialization) | 构造恶意 pickle/yaml 序列化 payload，观察报错/超时/命令回显 |
| CSRF (csrf) | 构造跨站请求（模拟带 Cookie）确认关键动作被执行 |
| 越权/IDOR (idor) | 用低权限账号请求高权限资源，确认返回了不应看到的数据 |

**验证证据与判定**
- 每次验证必须记录：请求原文、响应、时间差、回显内容（`vuln_verify` 返回 status_code / response_body / elapsed_sec / signals）
- 判定标准：
  - `confirmed` — 请求实际生效，有明确证据（回显 / 时序 / 回连 / 状态码差异）
  - `not confirmed` — 多次尝试无效应，标注为低置信，**不得**写入正式报告
  - `out of scope` — 依赖条件不满足，标注所需条件后跳过
- 测试载荷必须无副作用：禁止删除/写入脏数据、禁止真实外呼、禁止对第三方系统发包（`vuln_verify` 会拦截非本地目标与破坏性 payload）

### 6. 输出规范
- 每个问题标注：优先级（P0/P1/P2）、源码位置、一句话描述、攻击方式、验证证据（confirmed 附请求/响应/时序）、修复建议
- 先按验证结果分层：confirmed > 疑似（未验证）> 已排除
- 按危害程度排序，最严重的放最前面
- 用表格给出「速览」摘要，再用详细章节展开

### 7. 经验沉淀（审计收尾必须执行）
把**经过确认可行的思路与经验**结构化沉淀进经验库，供下次审计同类项目复用。调用 `skill_experience_add(skill_name="security-audit", ...)`，逐条添加。

**落盘门槛（必须满足至少一条，否则一律不落盘）：**
- 漏洞已经 `vuln_verify` 黑盒实测，返回 confirmed 证据（回显 / 时序 / 回连 / 状态码差异）
- 用户已明确确认该漏洞正确（如「对」「确认」「是的」）

**以下情况一律不落盘：**
- 仅静态分析线索、未经黑盒验证
- `vuln_verify` 返回 not confirmed / 误报
- 用户否定或未表态

字段填法：
- **confirmed 漏洞** → `vuln_type` 填漏洞类型（sqli/rce/ssrf/...），`source_pattern` 写源码特征根因（如「f-string 直接拼接 SQL，参数来自用户输入」），`payload` 写验证载荷，`evidence` 写验证证据（响应差异/时序/回连），`approach` 写本次挖洞思路/定位方法，`framework_hint` 写框架栈提示
- **高发攻击面**（未必有具体洞）→ `vuln_type="attack_surface"`，`source_pattern` 写攻击面特征
- **绕过技巧 / 探测手法** → `vuln_type="bypass_technique"`，`approach` 或 `note` 写具体手法

思路（`approach`）与经验同门槛，一起落盘。去重由工具按 `vuln_type + source_pattern` 自动完成，重复条目不重复写。

## 工具使用顺序

```
exec(find/tree)          → 了解结构
read_file(关键文件)       → 理解链路
exec(grep 危险函数)       → 批量扫描
read_file(命中文件)       → 逐条确认
exec(grep 下一批)         → 继续扫描
code_sandbox             → L2 白盒单元级验证（抽 sink + mock 参数 + 直接执行）
exec(启动本地实例)        → L3 起完整服务（L2 无法驱动时）
vuln_verify              → L3 黑盒验证每个漏洞（强制审批，展示类型/目标/payload/预期）
write_file               → 输出报告
```

> L2 白盒单元级优先尝试（无需起服务）；仅当 L2 无法驱动（环境缺失/依赖过重）才推进 L3 起服务。

## 常见危险函数 grep 列表

```
subprocess.*shell=True
pickle.loads?\(
yaml.load\(
os\.system\(
exec\(
eval\(
importlib\.import_module
exec_module
requests\.(get|post|put|delete)\(
httpx\.(get|post|put|delete)\(
urllib\.request
os\.path\.join\(
\.joinpath\(
open\(
aiofiles\.open
```

## 关键原则

- 每个结论必须有源码行号佐证
- 依赖代码放在 `site-packages` 里也是攻击面
- 攻击链比孤立漏洞更危险
- 先读后审，绝不盲猜
- 静态分析结论只是线索，未经验证的漏洞不算数（L1 仅「疑似」）
- 验证按阶梯推进：优先 L2 白盒单元级（code_sandbox），L2 无法驱动再评估 L3 黑盒（vuln_verify）
- L2 出现 env_gap（import/连库/解释器失败）不得标 confirmed，降级疑似回退 L3
- 每个 confirmed 漏洞必须附验证证据（L2 运行时特征 / L3 请求原文、响应、时序、回连记录），并注明验证方式
- 验证测试先本地部署，对真实环境发包前必须确认无副作用、无第三方系统牵连
