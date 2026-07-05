# Wiki Search Skill

维基百科搜索技能 - 使用维基百科 API 获取词条信息。

## 基本信息

| 字段 | 值 |
|------|-----|
| name | wiki_search |
| version | 1.0.0 |
| author | Jify |
| tags | wikipedia, api, search, information |
| parallel_safe | true |

## 能力描述

本技能封装了维基百科 API 的调用，提供以下能力：

- 按词条名称搜索维基百科条目
- 支持中英文语言版本
- 返回结构化的摘要内容
- 包含缩略图信息

## 参数定义

```json
{
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "要搜索的词条名称"
        },
        "lang": {
            "type": "string",
            "description": "语言版本",
            "enum": ["en", "zh"],
            "default": "en"
        }
    },
    "required": ["query"]
}
```

## 返回值

成功情况：

```json
{
    "success": true,
    "data": {
        "pageid": 832774,
        "title": "SpaceX",
        "lang": "en",
        "url": "https://en.wikipedia.org/wiki/SpaceX",
        "extract": "Space Exploration Technologies Corporation...",
        "thumbnail": "https://..."
    }
}
```

错误情况：

```json
{
    "success": false,
    "error": "未找到词条: xxx"
}
```

或超时情况：

```json
{
    "success": false,
    "error": "访问超时"
}
```

## 使用示例

### Python 代码调用

```python
from wiki_search.handler import handler, format_markdown

# 基本搜索
result = handler("SpaceX")
print(result)

# 中文搜索
result = handler("SpaceX", lang="zh")
print(format_markdown(result))
```

### 命令行调用

```bash
python handler.py SpaceX en
python handler.py 人工智能 zh
```

## 文件结构

```
wiki_search/
├── SKILL.md      # 本文档 - 技能说明文档
├── skill.json    # 技能元数据
├── __init__.py   # 包初始化文件
└── handler.py    # 处理函数实现
```

## API 端点

使用维基百科 MediaWiki API：

- 英文：`https://en.wikipedia.org/w/api.php`
- 中文：`https://zh.wikipedia.org/w/api.php`

主要参数：

| 参数 | 说明 |
|------|------|
| action | query |
| format | json |
| prop | extracts, pageimages |
| titles | 词条名称 |
| exintro | true (仅返回导言) |
| explaintext | true (纯文本) |

## 注意事项

- **请求超时设置**：5 秒
- 词条名称大小写敏感
- 部分词条可能不存在，应检查 `success` 字段
- 建议对长文本进行适当截断处理