# -*- coding: utf-8 -*-
"""
Jify 对话持久化存储 — SQLite 实现

与 ~/.jify/self_evolution/ 的分层记忆互补：
  ~/.jify/self_evolution/ → 自进化数据（技能建议、用户画像等），给 agent 读的上下文
  storage/                → 完整对话 (未压缩)，给人类回溯/搜索
"""

from storage.db import ConversationDB, get_db
