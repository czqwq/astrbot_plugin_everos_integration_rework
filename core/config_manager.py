"""配置管理器 — 默认值 + 类型安全访问。"""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "everos_base_url": "http://127.0.0.1:8765",
    "enable_tools": True,
    "enable_webui": True,
    "app_id": "astrbot",
    "project_id": "default",
    "standalone_webui_enabled": True,
    "standalone_webui_host": "0.0.0.0",
    "standalone_webui_port": 18766,
    "isolation_personas": "",
    "extra_user_ids": "",
    # EverOS 边界检测阈值（与 EverOS default.toml [boundary_detection] 保持一致）
    "boundary_token_limit": 65536,
    "boundary_msg_limit": 500,
}


class ConfigManager:
    def __init__(self, raw: dict[str, Any] | None = None):
        self._data = {**_DEFAULTS, **(raw or {})}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def everos_base_url(self) -> str:
        return self.get("everos_base_url")

    @property
    def enable_tools(self) -> bool:
        return self.get("enable_tools")

    @property
    def enable_webui(self) -> bool:
        return self.get("enable_webui")

    @property
    def app_id(self) -> str:
        return self.get("app_id")

    @property
    def project_id(self) -> str:
        return self.get("project_id")

    # ─── 记忆隔离 ──────────────────────────────────────────────

    @property
    def extra_user_ids(self) -> list[str]:
        """获取手动指定的额外 user_id 列表（逗号分隔）。"""
        raw = self.get("extra_user_ids", "")
        if not raw or not raw.strip():
            return []
        return [uid.strip() for uid in raw.split(",") if uid.strip()]

    @property
    def boundary_token_limit(self) -> int:
        """EverOS 边界检测的 token 硬上限（与 EverOS [boundary_detection] 一致）。"""
        return int(self.get("boundary_token_limit", 65536))

    @property
    def boundary_msg_limit(self) -> int:
        """EverOS 边界检测的消息条数硬上限（与 EverOS [boundary_detection] 一致）。"""
        return int(self.get("boundary_msg_limit", 500))

    @property
    def isolation_personas(self) -> list[str]:
        """获取隔离白名单人格列表。"""
        raw = self.get("isolation_personas", "")
        if not raw or not raw.strip():
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def is_isolated(self, persona_name: str | None) -> bool:
        """判断指定人格是否在隔离白名单中。"""
        if not persona_name:
            return False
        return persona_name in self.isolation_personas

    def get_app_id_for(self, persona_name: str | None) -> str:
        """获取指定人格应使用的 app_id。
        
        在隔离白名单中的人格 → 使用独立的 app_id（默认 app_id + 人格名）
        不在白名单中的人格  → 使用全局默认 app_id
        """
        base = self.app_id
        if persona_name and self.is_isolated(persona_name):
            return f"{base}_{persona_name}"
        return base
