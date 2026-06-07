"""LLM 工具：写入记忆到 EverOS。"""

from __future__ import annotations

import time

from ..core.config_manager import ConfigManager
from ..core.everos_client import EverOSClient


class EverOSMemorizeTool:
    """将用户的重要信息 / 偏好 / 事实写入 EverOS 长期记忆。

    EverOS 会在积累足够的消息后自动提取和结构化记忆。
    适合记录：用户个人信息、偏好、重要决策、技能学习成果。
    """

    def __init__(self, client: EverOSClient, config: ConfigManager):
        self._client = client
        self._config = config
        self.active = True
        self.handler = self.__call__

    @property
    def name(self) -> str:
        return "everos_memorize"

    @property
    def description(self) -> str:
        return (
            "将用户的重要偏好、事实、或对话关键信息写入 EverOS 长期记忆系统。"
            "适合记录：用户个人信息、偏好习惯、重要决定、技能学习。"
            "使用时需要一条自然语言描述的内容，例如「用户喜欢喝冰美式，不加糖」。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要写入记忆的自然语言内容，包含完整的上下文信息",
                },
                "user_id": {
                    "type": "string",
                    "description": "用户标识（可选），不填则自动使用当前用户ID",
                },
                "persona_name": {
                    "type": "string",
                    "description": "当前人格名称（可选）。插件配置了记忆隔离白名单时，"
                                   "不同人格的记忆会被隔离存储",
                },
            },
            "required": ["content"],
        }

    async def __call__(self, content: str, user_id: str = "", persona_name: str = "") -> str:
        # 根据人格名决定 app_id（记忆隔离）
        app_id = self._config.get_app_id_for(persona_name or None)
        project_id = self._config.project_id

        ts = int(time.time() * 1000)
        messages = [
            {
                "sender_id": user_id or persona_name or "unknown",
                "role": "user",
                "timestamp": ts,
                "content": content,
            }
        ]
        try:
            await self._client.memory_add(
                session_id=f"llm-tool-{ts}",
                messages=messages,
                app_id=app_id,
                project_id=project_id,
            )
            # 写入后立即 flush，触发记忆提取
            await self._client.memory_flush(
                session_id=f"llm-tool-{ts}",
                app_id=app_id,
                project_id=project_id,
            )
            detail = f" (app: {app_id})" if app_id != self._config.app_id else ""
            return f"✅ 已写入 EverOS 记忆：{content}{detail}"
        except Exception as e:
            return f"❌ EverOS 写入失败：{e}"


class EverOSRecallTool:
    """从 EverOS 检索相关记忆。"""

    def __init__(self, client: EverOSClient, config: ConfigManager):
        self._client = client
        self._config = config
        self.active = True
        self.handler = self.__call__

    @property
    def name(self) -> str:
        return "everos_recall"

    @property
    def description(self) -> str:
        return (
            "从 EverOS 长期记忆系统检索与查询相关的记忆。"
            "可用于：回顾用户的偏好和历史信息、查找之前的决策和案例、"
            "获取已归纳的技能和模式。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "记忆检索查询词，用自然语言描述你想查什么",
                },
                "user_id": {
                    "type": "string",
                    "description": "用户标识（可选）",
                },
                "persona_name": {
                    "type": "string",
                    "description": "当前人格名称（可选）。插件配置了记忆隔离白名单时，"
                                   "只在当前人格的独立记忆空间内检索",
                },
            },
            "required": ["query"],
        }

    async def __call__(self, query: str, user_id: str = "", persona_name: str = "") -> str:
        app_id = self._config.get_app_id_for(persona_name or None)
        project_id = self._config.project_id

        try:
            result = await self._client.memory_search(
                query=query,
                user_id=user_id or persona_name,
                app_id=app_id,
                project_id=project_id,
                top_k=5,
            )
            memories = result.get("data", {}).get("memories", [])
            if not memories:
                return "🔍 未在 EverOS 中找到相关记忆。"
            lines = ["📚 **EverOS 记忆检索结果：**", ""]
            for i, mem in enumerate(memories, 1):
                content = mem.get("content", mem.get("text", str(mem)))
                score = mem.get("score", mem.get("relevance", ""))
                if score:
                    lines.append(f"{i}. [{score:.2f}] {content}")
                else:
                    lines.append(f"{i}. {content}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ EverOS 检索失败：{e}"
