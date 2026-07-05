# -*- coding: utf-8 -*-
"""
PluginLoader — 插件发现、校验、加载
"""

import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from event_bus import UIEvent, event_bus


class PluginLoader:
    """插件加载器：发现 → 校验 → 加载 → 注册"""

    def __init__(self, plugins_dir: str = "plugins"):
        # Jify 项目根目录（始终固定），插件可依赖它来做 from tools.registry import ...
        self._project_root = Path(__file__).parent.parent

        # 展开 ~ / $HOME 后解析为绝对路径，不再自动拼接项目根目录
        self.plugins_dir = Path(os.path.expanduser(plugins_dir)).resolve()

        self.loaded: Dict[str, dict] = {}  # name → {path, manifest}
        self.failed: Dict[str, str] = {}   # name → error_message


    # 发现
    def discover(self) -> List[Path]:
        """扫描 plugins/ 下所有包含 plugin.json 的子目录，按名称排序"""
        if not self.plugins_dir.exists() or not self.plugins_dir.is_dir():
            return []

        candidates = []
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "plugin.json"
            if manifest.exists() and manifest.is_file():
                candidates.append(entry)
        return candidates


    # 校验
    def validate(self, manifest: dict) -> Optional[str]:
        """校验 manifest 必填字段，返回错误信息或 None"""
        if not isinstance(manifest, dict):
            return "manifest is not a valid JSON object"

        if "name" not in manifest or not manifest["name"]:
            return "missing required field: 'name'"

        if "version" not in manifest or not manifest["version"]:
            return "missing required field: 'version'"

        plugin_type = manifest.get("type", [])
        if not isinstance(plugin_type, list) or len(plugin_type) == 0:
            return "'type' must be a non-empty list (e.g. ['tool'])"

        valid_types = {"tool", "skill", "hook", "middleware"}
        for t in plugin_type:
            if t not in valid_types:
                return f"unknown plugin type: '{t}' (valid: {', '.join(sorted(valid_types))})"

        return None  # 校验通过


    # 加载
    def load(self, plugin_path: Path) -> bool:
        """加载单个插件：读取 manifest → 校验 → import → 触发注册"""
        manifest_path = plugin_path / "plugin.json"

        # 1. 读取 manifest
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError) as e:
            self.failed[plugin_path.name] = f"failed to read plugin.json: {e}"
            event_bus.put(UIEvent("ERROR", f"[ Plugin ] {plugin_path.name}: {self.failed[plugin_path.name]}"))
            return False

        # 2. 校验
        error = self.validate(manifest)
        if error:
            self.failed[plugin_path.name] = error
            event_bus.put(UIEvent("ERROR", f"[ Plugin ] {plugin_path.name}: {error}"))
            return False

        name = manifest["name"]
        plugin_type = manifest.get("type", [])

        # 3. 确保 Jify 项目根目录始终在 sys.path（插件内的 from tools.registry import ... 依赖它）
        project_root = str(self._project_root)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        # 4. 根据类型分发加载
        loaded_any = False

        if "tool" in plugin_type:
            loaded_any |= self._load_tool_plugin(plugin_path, name)

        if "hook" in plugin_type:
            loaded_any |= self._load_hook_plugin(plugin_path, name)

        # 其他类型
        for t in plugin_type:
            if t not in ("tool", "hook"):
                event_bus.put(
                    UIEvent("TEXT", f"[ Plugin ] {name}: type '{t}' not yet supported (Phase 2+)")
                )

        if loaded_any:
            self.loaded[name] = {"path": str(plugin_path), "manifest": manifest}
            # event_bus.put(UIEvent("TEXT", f"[ Plugin ] ✓ loaded '{name}' v{manifest['version']}"))
            return True
        else:
            self.failed[name] = "no loadable modules found"
            return False

    def _load_tool_plugin(self, plugin_path: Path, name: str) -> bool:
        """加载 tool 类型插件：import tools.py 或 __init__.py 触发 @register_tool"""

        # 判断插件目录是否在 Jify 项目内
        try:
            self.plugins_dir.relative_to(self._project_root)
            # 项目内插件：使用标准包路径 plugins.<name>.tools
            rel = self.plugins_dir.relative_to(self._project_root)
            prefix = ".".join(rel.parts)  # e.g. "plugins"
            module_paths = [
                f"{prefix}.{plugin_path.name}.tools",
                f"{prefix}.{plugin_path.name}",
            ]
        except ValueError:
            # 外部目录：把插件目录加入 sys.path，平级 import
            parent = str(self.plugins_dir)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            module_paths = [
                f"{plugin_path.name}.tools",
                f"{plugin_path.name}",
            ]

        for module_path in module_paths:
            try:
                importlib.import_module(module_path)
                return True
            except ImportError:
                continue
            except Exception as e:
                self.failed[name] = f"error importing {module_path}: {e}"
                event_bus.put(UIEvent("ERROR", f"[ Plugin ] {name}: {self.failed[name]}\n{traceback.format_exc()}"))
                return False

        # 两个路径都找不到
        self.failed[name] = f"neither tools.py nor __init__.py found in {plugin_path}"
        return False

    def _load_hook_plugin(self, plugin_path: Path, name: str) -> bool:
        """加载 hook 类型插件：import hooks.py → 自动注册 hook 函数

        hooks.py 中定义与 hook 点同名的函数（如 before_api_call, after_tool_call 等），
        HookManager 会自动扫描并注册。
        """

        # 判断插件目录是否在 Jify 项目内
        try:
            self.plugins_dir.relative_to(self._project_root)
            # 项目内插件：使用标准包路径
            rel = self.plugins_dir.relative_to(self._project_root)
            prefix = ".".join(rel.parts)  # e.g. "plugins"
            module_path = f"{prefix}.{plugin_path.name}.hooks"
        except ValueError:
            # 外部目录：把插件目录加入 sys.path，平级 import
            parent = str(self.plugins_dir)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            module_path = f"{plugin_path.name}.hooks"

        try:
            module = importlib.import_module(module_path)
        except ImportError:
            self.failed[name] = f"hooks.py not found in {plugin_path}"
            event_bus.put(UIEvent("ERROR", f"[ Plugin ] {name}: {self.failed[name]}"))
            return False
        except Exception as e:
            self.failed[name] = f"error importing {module_path}: {e}"
            event_bus.put(UIEvent("ERROR", f"[ Plugin ] {name}: {self.failed[name]}\n{traceback.format_exc()}"))
            return False

        # 自动发现并注册 hook 函数
        from plugins.hook_manager import hook_manager
        count = hook_manager.auto_register_from_module(module, plugin_name=name)
        if count == 0:
            self.failed[name] = f"no hook functions found in {plugin_path}/hooks.py"
            event_bus.put(UIEvent("TEXT", f"[ Plugin ] {name}: {self.failed[name]}"))
            return False

        return True


    # 批量加载
    def load_all(self, enabled_only: Optional[List[str]] = None) -> Dict[str, bool]:
        """
        发现并加载所有插件。

        Args:
            enabled_only: 如果提供，只加载列表中的插件；None 则加载全部

        Returns:
            {plugin_name: success}
        """
        candidates = self.discover()

        if not candidates:
            # event_bus.put(UIEvent("TEXT", "[ Plugin ] no plugins found"))
            return {}

        # event_bus.put(UIEvent("TEXT", f"[ Plugin ] discovered {len(candidates)} plugin(s)"))

        results = {}
        for plugin_path in candidates:
            name = plugin_path.name
            if enabled_only is not None and name not in enabled_only:
                continue
            results[name] = self.load(plugin_path)

        # loaded_count = sum(1 for v in results.values() if v)
        # event_bus.put(
        #     UIEvent("TEXT", f"[ Plugin ] done: {loaded_count}/{len(results)} loaded")
        # )
        return results


    # 查询
    def list_loaded(self) -> List[str]:
        """返回已加载插件名称列表"""
        return list(self.loaded.keys())

    def list_failed(self) -> Dict[str, str]:
        """返回加载失败的插件及原因"""
        return dict(self.failed)

    def is_loaded(self, name: str) -> bool:
        return name in self.loaded
