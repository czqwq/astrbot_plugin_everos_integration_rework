"""
EverOS 独立 WebUI 服务器。

插件加载时自动启动，用户访问配置的端口即可看到 Dashboard。
参考主动消息插件的 WebAdminServer 实现。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.middleware.cors import CORSMiddleware
    import httpx

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.warning(
        "[EverOS] FastAPI 未安装，独立 WebUI 不可用。请安装: pip install fastapi uvicorn httpx"
    )


def _normalize_item(item: dict, mtype: str = "episode") -> dict:
    """统一记忆条目的字段名（将 EverOS 各类型字段映射为 content）。"""
    if not item.get("content"):
        if mtype == "episode":
            item["content"] = (
                item.get("episode")  # 完整内容优先
                or item.get("summary")
                or item.get("subject")
                or json.dumps(item, ensure_ascii=False)[:200]
            )
        elif "profile_data" in item:
            pd = item["profile_data"]
            if isinstance(pd, dict):
                item["content"] = pd.get("summary", json.dumps(pd, ensure_ascii=False)[:200])
            else:
                item["content"] = str(pd)[:200]
        else:
            item["content"] = json.dumps(item, ensure_ascii=False)[:200]
    return item


class StandaloneServer:
    """EverOS 独立 WebUI 服务器。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.config = plugin.config
        self.app: FastAPI | None = None
        self.server = None
        self.server_task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._running = False

        if FASTAPI_AVAILABLE:
            try:
                self._setup_app()
            except Exception as e:
                self.app = None
                logger.error(f"[EverOS] 独立 WebUI 初始化失败: {e}，已自动禁用")

    def _get_everos_url(self) -> str:
        """从插件配置获取 EverOS 后端地址。"""
        return self.config.get("everos_base_url", "http://127.0.0.1:8765")

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0, verify=False)
        return self._http_client

    def _setup_app(self) -> None:
        self.app = FastAPI(title="EverOS Dashboard (Standalone)")

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # 静态文件（挂载到 /static 下，避免和 API 路由冲突）
        pages_dir = Path(__file__).resolve().parent.parent / "pages" / "everos-dashboard"
        self._pages_dir = pages_dir
        if pages_dir.exists():
            self.app.mount(
                "/static",
                StaticFiles(directory=str(pages_dir)),
                name="everos-dashboard",
            )
        else:
            logger.warning(f"[EverOS] Dashboard 静态目录不存在: {pages_dir}")

        self._register_routes()

    def _register_routes(self) -> None:
        if not self.app:
            return

        # ─── ⚠️ API 路由必须优先注册，避免被 catch-all 拦截 ────

        self._register_api_routes()

        # ─── 首页 ────────────────────────────────────────────────

        @self.app.get("/")
        async def index():
            """返回 Dashboard HTML。"""
            html_path = self._pages_dir / "index.html"
            if not html_path.exists():
                return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
            html = html_path.read_text(encoding="utf-8")
            return HTMLResponse(html)

        @self.app.get("/{filename:path}")
        async def serve_static(filename: str):
            """提供 style.css / app.js 等根路径静态文件请求。"""
            file_path = self._pages_dir / filename
            if file_path.exists() and file_path.is_file():
                return FileResponse(str(file_path))
            return HTMLResponse(status_code=404)

    def _register_api_routes(self) -> None:
        """注册 API 路由（必须在 catch-all 之前注册）。"""
        if not self.app:
            return

        @self.app.get("/api/everos/status")
        async def api_status():
            """聚合健康检查 + 统计。"""
            client = self._get_client()
            base_url = self._get_everos_url()
            try:
                health = await client.get(f"{base_url}/health")
                health.raise_for_status()
                health_data = health.json()
                ok = health_data.get("status") == "ok"

                stats = {}
                # 尝试多个可能的 user_id 以覆盖不同用户写入的记忆
                candidate_uids = [
                    self.config.get("app_id", "astrbot"),
                    "default", "webui",
                ]
                agent_kinds = {"agent_case", "agent_skill"}
                for mtype in ("episode", "profile", "agent_case", "agent_skill"):
                    total = 0
                    seen_ids = set()
                    for uid in candidate_uids:
                        # agent_skill / agent_case 用 agent_id，其余用 user_id
                        owner_field = "agent_id" if mtype in agent_kinds else "user_id"
                        try:
                            r = await client.post(
                                f"{base_url}/api/v1/memory/get",
                                json={
                                    "memory_type": mtype,
                                    owner_field: uid,
                                    "app_id": "astrbot",
                                    "project_id": "default",
                                },
                            )
                            data = r.json()
                            d = data.get("data", {})
                            items = d.get(mtype + "s", [])
                            for item in items:
                                mid = item.get("id", "")
                                if mid and mid not in seen_ids:
                                    seen_ids.add(mid)
                                    total += 1
                            # 如果 API 返回了 total_count，用最大值
                            tc = d.get("total_count", 0)
                            if tc > total:
                                total = tc
                        except Exception:
                            continue
                    stats[mtype] = total

                return {
                    "healthy": ok,
                    "base_url": base_url,
                    "latency": None,
                    "app_id": health_data.get("app_id", "everos"),
                    "project_id": health_data.get("project_id", "default"),
                    "stats": stats,
                }
            except Exception as e:
                return {"healthy": False, "error": str(e), "base_url": base_url, "stats": {}}

        @self.app.get("/api/everos/memories")
        async def api_memories():
            """获取各类型记忆（最近活动）。"""
            client = self._get_client()
            base_url = self._get_everos_url()
            candidate_uids = [
                self.config.get("app_id", "astrbot"),
                "default", "webui",
            ]
            try:
                all_items = []
                seen_ids = set()
                agent_kinds = {"agent_case", "agent_skill"}
                for mtype in ("episode", "profile", "agent_case", "agent_skill"):
                    for uid in candidate_uids:
                        # agent_skill / agent_case 用 agent_id，其余用 user_id
                        owner_field = "agent_id" if mtype in agent_kinds else "user_id"
                        try:
                            r = await client.post(
                                f"{base_url}/api/v1/memory/get",
                                json={
                                    "memory_type": mtype,
                                    owner_field: uid,
                                    "app_id": "astrbot",
                                    "project_id": "default",
                                },
                            )
                            data = r.json()
                            d = data.get("data", {})
                            items = d.get(mtype + "s", [])
                            for item in items:
                                if isinstance(item, dict):
                                    mid = item.get("id", "")
                                    if mid and mid in seen_ids:
                                        continue
                                    seen_ids.add(mid)
                                    item["memory_type"] = item.get("memory_type") or mtype
                                    item = _normalize_item(item, mtype)
                                    all_items.append(item)
                        except Exception:
                            continue
                return {"ok": True, "data": {"items": all_items}}
            except Exception as e:
                return {"ok": False, "error": str(e), "data": {"items": []}}

        @self.app.post("/api/everos/memorize")
        async def api_memorize(request: Request):
            """写入记忆。"""
            body = await request.json()
            content = body.get("content", "")
            memory_type = body.get("memory_type", "episode")
            user_id = body.get("user_id", "webui")
            client = self._get_client()
            base_url = self._get_everos_url()

            payload = {
                "session_id": f"webui_{user_id}",
                "app_id": self.config.get("app_id", "astrbot"),
                "project_id": self.config.get("project_id", "default"),
                "messages": [{
                    "sender_id": user_id,
                    "role": "user",
                    "timestamp": int(time.time() * 1000),
                    "content": content,
                }],
            }
            try:
                resp = await client.post(f"{base_url}/api/v1/memory/add", json=payload)
                resp.raise_for_status()
                result = resp.json()
                await client.post(
                    f"{base_url}/api/v1/memory/flush",
                    json={
                        "session_id": f"webui_{user_id}",
                        "app_id": self.config.get("app_id", "astrbot"),
                        "project_id": self.config.get("project_id", "default"),
                    },
                )
                return {"ok": True, "status": "ok", "message": "记忆已写入", "data": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        @self.app.post("/api/everos/memories-by-type")
        async def api_memories_by_type(request: Request):
            """按类型获取记忆。"""
            body = await request.json()
            raw_type = body.get("memory_type", "episode")
            # 旧版兼容：atomic_fact 已合并到 episode
            _COMPAT = {"atomic_fact": "episode"}
            memory_type = _COMPAT.get(raw_type, raw_type)
            candidate_uids = [
                self.config.get("app_id", "astrbot"),
                "default", "webui",
            ]
            client = self._get_client()
            base_url = self._get_everos_url()
            agent_kinds = {"agent_case", "agent_skill"}
            try:
                all_items = []
                seen_ids = set()
                for uid in candidate_uids:
                    # agent_skill / agent_case 用 agent_id，其余用 user_id
                    owner_field = "agent_id" if memory_type in agent_kinds else "user_id"
                    try:
                        r = await client.post(
                            f"{base_url}/api/v1/memory/get",
                            json={
                                "memory_type": memory_type,
                                owner_field: uid,
                                "app_id": "astrbot",
                                "project_id": "default",
                            },
                        )
                        r.raise_for_status()
                        data = r.json()
                        d = data.get("data", {})
                        items = d.get(memory_type + "s", [])
                        for item in items:
                            if isinstance(item, dict):
                                mid = item.get("id", "")
                                if mid and mid in seen_ids:
                                    continue
                                seen_ids.add(mid)
                                item["memory_type"] = item.get("memory_type") or memory_type
                                item = _normalize_item(item, memory_type)
                                all_items.append(item)
                    except Exception:
                        continue
                return {"ok": True, "data": {"items": all_items}}
            except Exception as e:
                return {"ok": False, "error": str(e), "data": {"items": []}}

        @self.app.post("/api/everos/flush")
        async def api_flush(request: Request):
            """触发记忆提炼。"""
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
            session_id = body.get("session_id", "default_dialog")
            client = self._get_client()
            base_url = self._get_everos_url()
            try:
                resp = await client.post(
                    f"{base_url}/api/v1/memory/flush",
                    json={
                        "session_id": session_id,
                        "app_id": self.config.get("app_id", "astrbot"),
                        "project_id": self.config.get("project_id", "default"),
                    },
                )
                resp.raise_for_status()
                return {"ok": True, "status": "ok", "message": "记忆提炼已触发"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        @self.app.post("/api/everos/search")
        async def api_search(request: Request):
            """语义检索。"""
            body = await request.json()
            query = body.get("query", "")
            top_k = body.get("top_k", 10)
            candidate_uids = [
                self.config.get("app_id", "astrbot"),
                "default", "webui",
            ]
            client = self._get_client()
            base_url = self._get_everos_url()
            try:
                all_items = []
                seen_ids = set()
                per_uid = max(1, top_k // len(candidate_uids))
                for uid in candidate_uids:
                    try:
                        resp = await client.post(
                            f"{base_url}/api/v1/memory/search",
                            json={
                                "query": query,
                                "user_id": uid,
                                "app_id": self.config.get("app_id", "astrbot"),
                                "project_id": self.config.get("project_id", "default"),
                                "top_k": per_uid,
                            },
                        )
                        raw = resp.json()
                        rd = raw.get("data", {})
                        for key in ("episodes", "profiles", "agent_cases", "agent_skills"):
                            for item in rd.get(key, []):
                                if isinstance(item, dict):
                                    mid = item.get("id", "")
                                    if mid and mid in seen_ids:
                                        continue
                                    seen_ids.add(mid)
                                    item["memory_type"] = item.get("memory_type") or key.rstrip("s")
                                    item = _normalize_item(item, key.rstrip("s"))
                                    all_items.append(item)
                    except Exception:
                        continue
                return {"ok": True, "data": {"items": all_items}}
            except Exception as e:
                return {"ok": False, "error": str(e), "data": {"items": []}}

        @self.app.post("/api/everos/forget")
        async def api_forget(request: Request):
            """删除指定 ID 的记忆。"""
            body = await request.json()
            memory_id = body.get("id", "")
            memory_type = body.get("memory_type", "episode")

            if not memory_id:
                return {"ok": False, "error": "缺少 id 参数"}

            try:
                from everos.infra.persistence.lancedb import (
                    episode_repo, atomic_fact_repo,
                    agent_case_repo, agent_skill_repo,
                )
                repo_map = {
                    "episode": episode_repo,
                    "atomic_fact": atomic_fact_repo,
                    "agent_case": agent_case_repo,
                    "agent_skill": agent_skill_repo,
                }
                repo = repo_map.get(memory_type)
                if not repo:
                    return {"ok": False, "error": f"不支持的记忆类型: {memory_type}"}

                predicate = f"id = '{memory_id}'"
                await repo.delete(predicate)
                logger.info(f"🗑️ 已删除记忆: {memory_id} ({memory_type})")
                return {"ok": True, "data": {"deleted": memory_id}}
            except Exception as e:
                logger.error(f"删除记忆失败: {e}")
                return {"ok": False, "error": str(e)}

        @self.app.get("/api/everos/server-info")
        async def api_server_info():
            """返回服务器自身信息（端口等）。"""
            port = self.config.get("standalone_webui", {}).get("port", 18766)
            return {
                "port": port,
                "mode": "standalone",
                "everos_url": self._get_everos_url(),
                "app_id": self.config.get("app_id", "astrbot"),
                "project_id": self.config.get("project_id", "default"),
            }

    async def start(self) -> None:
        if not FASTAPI_AVAILABLE:
            logger.error("[EverOS] 无法启动独立 WebUI: FastAPI 未安装")
            return

        if self._running:
            return

        if not self.config.get("standalone_webui_enabled", True):
            logger.info("[EverOS] 独立 WebUI 未启用")
            return

        host = self.config.get("standalone_webui_host", "0.0.0.0")
        port = int(self.config.get("standalone_webui_port", 18766))

        uv_cfg = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uv_cfg)
        self._running = True

        async def _serve():
            try:
                await self.server.serve()
            except OSError as e:
                # 端口被占用（常见于热重载时旧 socket 尚未释放）
                logger.warning(
                    f"[EverOS] 独立 WebUI 端口 {port} 被占用，"
                    f"等待旧连接释放后将在下次状态刷新时自动恢复: {e}"
                )
            except asyncio.CancelledError:
                # 正常关闭流程（stop() 触发）
                pass
            except Exception as e:
                logger.error(f"[EverOS] 独立 WebUI 运行异常: {e}")
            finally:
                self._running = False
                self.server_task = None
                if self._http_client:
                    await self._http_client.aclose()
                    self._http_client = None

        self.server_task = asyncio.create_task(_serve())
        logger.info(f"🌿 EverOS Dashboard → http://{host}:{port}")

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True

        if self.server_task and not self.server_task.done():
            try:
                # 等待 uvicorn 优雅关闭（让 socket 正确释放）
                await asyncio.wait_for(self.server_task, timeout=3.0)
            except asyncio.TimeoutError:
                # 3 秒未退出则强制取消
                logger.warning("[EverOS] WebUI 优雅关闭超时，强制终止")
                self.server_task.cancel()
                try:
                    await self.server_task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                pass
            self.server_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._running = False
        logger.info("[EverOS] 独立 WebUI 已停止")
