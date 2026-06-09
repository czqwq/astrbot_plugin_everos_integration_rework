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
        self.is_background_task = False
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

    async def __call__(self, *args, content: str = "", user_id: str = "", persona_name: str = "") -> str:
        # 根据人格名决定 app_id（记忆隔离）
        app_id = self._config.get_app_id_for(persona_name or None)
        project_id = self._config.project_id

        # 默认使用 default 作为 user_id
        resolved_user_id = user_id or persona_name or "default"
        ts = int(time.time() * 1000)
        messages = [
            {
                "sender_id": resolved_user_id,
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


class EverOSLearnTool:
    """将 AI 智能体自身的技能、操作规则、经验教训写入 EverOS 智能体轨道。

    与 everos_memorize 不同，此工具写入 role="assistant" 的消息，
    触发 Agent Track 的 Cases/Skills 提炼流程。
    适合记录：智能体工作规范、技术决策、踩坑经验、可复用技能。
    """

    def __init__(self, client: EverOSClient, config: ConfigManager):
        self._client = client
        self._config = config
        self.active = True
        self.is_background_task = False
        self.handler = self.__call__

    @property
    def name(self) -> str:
        return "everos_learn"

    @property
    def description(self) -> str:
        return (
            "将 AI 智能体自身的技能、操作规则、经验教训写入 EverOS 长期记忆的智能体轨道。"
            "与 everos_memorize（存用户信息）不同，everos_learn 存的是智能体自己学会的东西——"
            "比如工作规范、技术决策、踩坑经验、可复用的解决方案。"
            "这些内容会进入 Agent Track，被提炼为 Cases（案例记忆）和 Skills（技能记忆）。"
            "使用时需要一条自然语言描述的内容，例如「处理 EverOS user_id 不一致问题：检索时需同时搜多个 user_id」"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要学习的技能/规则/经验，自然语言描述，包含完整的上下文信息",
                },
                "persona_name": {
                    "type": "string",
                    "description": "当前人格名称（可选）。插件配置了记忆隔离白名单时，"
                                   "不同人格的记忆会被隔离存储",
                },
            },
            "required": ["content"],
        }

    async def __call__(self, *args, content: str = "", persona_name: str = "") -> str:
        # 根据人格名决定 app_id（记忆隔离）
        app_id = self._config.get_app_id_for(persona_name or None)
        project_id = self._config.project_id

        ts = int(time.time() * 1000)
        # 使用 role="assistant" 触发 Agent Track 提炼 Case/Skill
        messages = [
            {
                "sender_id": "assistant",
                "role": "assistant",
                "timestamp": ts,
                "content": content,
            }
        ]
        try:
            await self._client.memory_add(
                session_id=f"learn-tool-{ts}",
                messages=messages,
                app_id=app_id,
                project_id=project_id,
            )
            # 写入后立即 flush，触发记忆提炼（含 Agent Case/Skill 提取）
            await self._client.memory_flush(
                session_id=f"learn-tool-{ts}",
                app_id=app_id,
                project_id=project_id,
            )
            detail = f" (app: {app_id})" if app_id != self._config.app_id else ""
            return f"🧠 已学会：{content}{detail}"
        except Exception as e:
            return f"❌ EverOS 学习写入失败：{e}"


class EverOSRecallTool:
    """从 EverOS 检索相关记忆。"""

    def __init__(self, client: EverOSClient, config: ConfigManager):
        self._client = client
        self._config = config
        self.active = True
        self.is_background_task = False
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

    async def __call__(self, *args, query: str = "", user_id: str = "", persona_name: str = "") -> str:
        app_id = self._config.get_app_id_for(persona_name or None)
        project_id = self._config.project_id
        # EverOS API 要求 user_id 至少 1 个字符
        resolved_user_id = user_id or persona_name or "default"

        try:
            # 同时搜多个 user_id，覆盖不同来源的存储
            candidate_uids = [resolved_user_id, "default"]
            memories = []
            seen_contents = set()
            for uid in candidate_uids:
                try:
                    result = await self._client.memory_search(
                        query=query,
                        user_id=uid,
                        app_id=app_id,
                        project_id=project_id,
                        top_k=5,
                    )
                    raw_data = result.get("data", {}) if isinstance(result, dict) else {}
                    for category in ("episodes", "profiles", "agent_cases", "agent_skills", "memories"):
                        items = raw_data.get(category, [])
                        if items:
                            for item in items:
                                if isinstance(item, dict):
                                    item["_category"] = category
                                    cid = item.get("id", item.get("content", ""))
                                    if cid and str(cid) not in seen_contents:
                                        seen_contents.add(str(cid))
                                        memories.append(item)
                except Exception:
                    continue
            if not memories:
                return "🔍 未在 EverOS 中找到相关记忆。"
            lines = ["📚 **EverOS 记忆检索结果：**", ""]
            for i, mem in enumerate(memories, 1):
                content = mem.get("content", mem.get("text", str(mem)))
                score = mem.get("score", mem.get("relevance", ""))
                track = mem.get("_track", "")
                mtype = mem.get("_type", mem.get("memory_type", "memory"))
                tag = f"[{track}/{mtype}]" if track else f"[{mtype}]"
                if score:
                    lines.append(f"{i}. {tag} [{score:.2f}] {content}")
                else:
                    lines.append(f"{i}. {tag} {content}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ EverOS 检索失败：{e}"
