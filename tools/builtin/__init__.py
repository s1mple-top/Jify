# -*- coding: utf-8 -*-
"""内置工具聚合 — 导入即注册到 registry"""

# @register_tool 装饰器，自动注册所有工具
from tools.builtin.exec_tool import exec_tool
from tools.builtin.file_tools import read_file, write_file
from tools.builtin.patch_file import patch_file
from tools.builtin.load_skill import load_skill
from tools.builtin.static_analysis import static_analysis
# from tools.builtin.p2p_tools import p2p_send, get_all_peer_names    # P2P tools 暂时注销 Jify间的交互一期暂时不上
from tools.builtin.mcp_tools import mcp_reload, mcp_list
from tools.builtin.skill_create import skill_create
# from tools.builtin.workflow_tool import run_workflow # workflow 暂时摒弃
from tools.builtin.subagent import subagent_run
from tools.builtin.team_tools import team_delegate, team_broadcast, team_delegate_parallel, team_status, team_add_worker, team_remove_worker
from tools.builtin.todo_tool import update_todos
# from tools.builtin.memory_tool import memory_search    # 长期记忆暂不启用
