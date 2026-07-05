#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wiki Search Skill - 维基百科搜索技能
使用维基百科API搜索词条信息并返回结构化结果
"""

import json
import urllib.request
import urllib.parse
import urllib.error


def handler(query, lang='en'):
    """
    使用维基百科API搜索词条信息
    
    Args:
        query: 要搜索的词条名称
        lang: 语言版本，'en' 或 'zh'，默认 'en'
    
    Returns:
        dict: 包含状态和信息的字典
    """
    if not query or not query.strip():
        return {
            "success": False,
            "error": "查询词条不能为空"
        }
    
    try:
        # 基础URL和参数
        base_url = f"https://{lang}.wikipedia.org/w/api.php"
        
        params = {
            "action": "query",
            "format": "json",
            "prop": "extracts|pageimages",
            "titles": query.strip(),
            "exintro": "true",
            "explaintext": "true",
            "exsectionformat": "plain",
            "piprop": "thumbnail",
            "pithumbsize": "200"
        }
        
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        
        # 创建请求，添加必要的头
        request = urllib.request.Request(url)
        request.add_header('User-Agent', 'JifyBot/1.0 (practical assistant; https://github.com/jify)')
        request.add_header('Accept', 'application/json')
        
        # 发送请求（5秒超时）
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        pages = data.get('query', {}).get('pages', {})
        
        if not pages or '-1' in pages:
            return {
                "success": False,
                "error": f"未找到词条: {query}"
            }
        
        # 获取页面数据
        page_data = list(pages.values())[0]
        page_id = page_data.get('pageid', 0)
        title = page_data.get('title', query)
        extract = page_data.get('extract', '')
        thumbnail = page_data.get('thumbnail', {})
        
        # 构建结果
        result = {
            "success": True,
            "data": {
                "pageid": page_id,
                "title": title,
                "lang": lang,
                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                "extract": extract,
                "thumbnail": thumbnail.get('source') if thumbnail else None
            }
        }
        
        return result
        
    except TimeoutError:
        return {
            "success": False,
            "error": "访问超时"
        }
    except urllib.error.HTTPError as e:
        return {
            "success": False,
            "error": f"HTTP错误: {e.code}"
        }
    except urllib.error.URLError as e:
        # 判断是否为超时错误
        if "timed out" in str(e.reason).lower() or isinstance(e.reason, TimeoutError):
            return {
                "success": False,
                "error": "访问超时"
            }
        return {
            "success": False,
            "error": f"网络请求失败: {str(e)}"
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"数据解析失败: {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"未知错误: {str(e)}"
        }


def format_markdown(result):
    """
    将搜索结果格式化为Markdown
    
    Args:
        result: handler返回的字典
    
    Returns:
        str: 格式化的Markdown字符串
    """
    if not result.get('success'):
        return f"❌ {result.get('error', '未知错误')}"
    
    data = result.get('data', {})
    title = data.get('title', '')
    extract = data.get('extract', '')
    url = data.get('url', '')
    thumbnail = data.get('thumbnail', '')
    
    md = f"## {title}\n\n"
    
    if thumbnail:
        md += f"![{title}]({thumbnail})\n\n"
    
    # 截取前500个字符
    if len(extract) > 500:
        extract = extract[:500] + "..."
    
    md += f"{extract}\n\n"
    md += f"🔗 [阅读更多]({url})"
    
    return md


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        query = sys.argv[1]
        lang = sys.argv[2] if len(sys.argv) > 2 else 'en'
        result = handler(query, lang)
        print(format_markdown(result))
    else:
        print("用法: python handler.py <词条名称> [语言(en/zh)]")