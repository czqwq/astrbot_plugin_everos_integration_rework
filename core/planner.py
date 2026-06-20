"""
EverOS 决策规划器 — 在 LLM 思索链之前完成 recall/save 决策。

提供两个轻量级决策：
1. recall_decision: 判断用户消息是否需要检索记忆
2. save_decision:   判断对话内容是否值得保存到记忆

两个决策都在 OnLLMRequestEvent 中完成（思索链之前），
使用配置的专门 LLM 提供商（或回退到主提供商），
通过极简 system prompt 约束仅返回 JSON，超时 5 秒兜底。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from astrbot.api import logger

RECALL_PLANNER_SYSTEM = """\
你是一个记忆检索决策助手。分析用户消息，判断是否需要从长期记忆中检索信息。

判断标准：
- 如果用户的问题需要关于他/她个人信息、偏好、历史、之前讨论过的话题 — 需要检索
- 如果只是简单问候、闲聊、或者不依赖记忆就能回答的问题 — 不需要检索
- 如果用户明确提到"之前"、"上次"、"还记得"、"我的" — 需要检索

严格按以下 JSON 格式回复（不要包含其他内容）：
{"action": "recall", "query": "一句话概括需要检索的内容"}
或
{"action": "skip", "query": ""}"""

SAVE_PLANNER_SYSTEM = """\
你是一个记忆保存决策助手。分析用户消息，判断是否有值得保存到长期记忆的信息。

判断标准：
- 用户透露的个人信息（姓名、偏好、习惯、计划、经历）— 需要保存
- 用户表达的重要观点、决定、或请求未来参考的事项 — 需要保存
- 简单问候、闲聊、或不含个人信息的问题 — 不需要保存

严格按以下 JSON 格式回复（不要包含其他内容）：
{"action": "save", "content": "一句话概括要保存的内容（中文，包含上下文）"}
或
{"action": "skip", "content": ""}"""

PLANNER_TIMEOUT = 5.0  # 决策超时秒数


def _parse_planner_json(text: str) -> dict[str, str]:
    """从 planner LLM 的回复中提取 JSON。容错处理。"""
    text = (text or "").strip()
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取 JSON 块
    import re

    m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"action": "skip", "query": ""}


class Planner:
    """轻量级决策规划器 — 在思索链之前运行。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    # ── Recall 决策 ──────────────────────────────────────────────

    async def recall_decision(self, user_query: str) -> tuple[str, str]:
        """返回 (action, query)。action ∈ {"recall", "skip"}。

        使用配置的 recall planner provider 或回退到主 provider。
        """
        provider_id = self._plugin.config.get("recall_planner_provider", "")
        return await self._planner_call(
            provider_id, RECALL_PLANNER_SYSTEM, user_query, "recall"
        )

    # ── Save 决策 ────────────────────────────────────────────────

    async def save_decision(self, user_query: str) -> tuple[str, str]:
        """返回 (action, content)。action ∈ {"save", "skip"}。

        使用配置的 save planner provider 或回退到主 provider。
        """
        provider_id = self._plugin.config.get("save_planner_provider", "")
        return await self._planner_call(
            provider_id, SAVE_PLANNER_SYSTEM, user_query, "save"
        )

    # ── 内部 ─────────────────────────────────────────────────────

    async def _planner_call(
        self, provider_id: str, system: str, user_query: str, mode: str
    ) -> tuple[str, str]:
        """通用 planner 调用。返回 (action, content)。"""
        if not user_query.strip():
            return ("skip", "")

        try:
            result = await asyncio.wait_for(
                self._do_llm_call(provider_id, system, user_query),
                timeout=PLANNER_TIMEOUT,
            )
            parsed = _parse_planner_json(result)
            action = parsed.get("action", "skip")
            content = parsed.get("query", "") or parsed.get("content", "")
            logger.info(
                f"[EverOS] Planner({mode}): action={action}, "
                f"content={content[:60]!r}"
            )
            return (action, content)
        except asyncio.TimeoutError:
            logger.warning(f"[EverOS] Planner({mode}): 超时 ({PLANNER_TIMEOUT}s)，回退到 skip")
            return ("skip", "")
        except Exception as e:
            logger.warning(f"[EverOS] Planner({mode}): 调用失败: {e}")
            return ("skip", "")

    async def _do_llm_call(
        self, provider_id: str, system: str, user_query: str
    ) -> str:
        """执行一次轻量 LLM 调用。"""
        # 回退到主 provider
        actual_provider = provider_id or self._get_main_provider_id()

        response = await self._plugin.context.llm_generate(
            chat_provider_id=actual_provider,
            prompt=user_query,
            system_prompt=system,
        )
        return response.completion_text or ""

    def _get_main_provider_id(self) -> str:
        """获取当前会话的主 provider ID。"""
        try:
            # 尝试从插件上下文获取
            return self._plugin.context.get_using_provider().meta().id
        except Exception:
            pass
        # 尝试从 provider_manager 获取第一个可用 provider
        try:
            providers = self._plugin.context.get_all_providers()
            if providers:
                return providers[0].meta().id
        except Exception:
            pass
        return ""
