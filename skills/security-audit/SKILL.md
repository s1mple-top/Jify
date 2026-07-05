---
name: security-audit
description: 对 Python Web/Agent 项目进行系统化安全审计。覆盖架构理解、多维漏洞扫描、PoC 编写、依赖链追踪的全流程方法论。适用于 AI Agent 框架、FastAPI/Django 应用、多组件架构项目。
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

### 5. 输出规范
- 每个问题标注：优先级（P0/P1/P2）、源码位置、一句话描述、攻击方式、修复建议
- 按危害程度排序，最严重的放最前面
- 用表格给出「速览」摘要，再用详细章节展开

## 工具使用顺序

```
exec(find/tree)     → 了解结构
read_file(关键文件)  → 理解链路
exec(grep 危险函数)  → 批量扫描
read_file(命中文件)  → 逐条确认
exec(grep 下一批)    → 继续扫描
write_file          → 输出报告
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
