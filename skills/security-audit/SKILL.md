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

### 5. 本地部署 + 黑盒实测验证（必须执行）

**核心原则：静态分析只是线索，未经验证的漏洞一律视为「疑似」，不得写入正式报告。**
发现漏洞后，必须在本地把目标跑起来，用 `vuln_verify` 工具黑盒确认——每次验证都强制经过用户审批，审批面板会展示验证类型 / 目标 / Payload / 预期结果。

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

## 工具使用顺序

```
exec(find/tree)          → 了解结构
read_file(关键文件)       → 理解链路
exec(grep 危险函数)       → 批量扫描
read_file(命中文件)       → 逐条确认
exec(grep 下一批)         → 继续扫描
exec(启动本地实例)        → 部署目标环境
vuln_verify              → 黑盒验证每个漏洞（强制审批，展示类型/目标/payload/预期）
write_file               → 输出报告
```

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
- 静态分析结论只是线索，未经黑盒实测的漏洞不算数
- 每个 confirmed 漏洞必须附带验证证据（请求原文 / 响应 / 时序 / 回连记录）
- 验证测试先本地部署，对真实环境发包前必须确认无副作用、无第三方系统牵连
