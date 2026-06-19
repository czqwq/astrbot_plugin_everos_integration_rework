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
        elif mtype == "agent_case":
            item["content"] = (
                item.get("task_intent", "")
                or item.get("key_insight", "")
                or item.get("approach", "")
                or json.dumps(item, ensure_ascii=False)[:200]
            )
        elif mtype == "agent_skill":
            item["content"] = (
                item.get("description", "")
                or item.get("name", "")
                or json.dumps(item, ensure_ascii=False)[:200]
            )
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

    async def _fetch_all_memories_for(
        self, client: httpx.AsyncClient, base_url: str,
        memory_type: str, uid: str, owner_field: str,
        app_id: str, project_id: str, page_size: int = 100,
    ) -> list[dict]:
        """分页获取某 (type, uid) 下的全部记忆条目。

        自动翻页直到 total_count 耗尽，修复 Dashboard 只读到前 20 条的 bug。
        """
        all_items: list[dict] = []
        seen_ids: set[str] = set()
        page = 1
        max_pages = 50  # 安全上限

        while page <= max_pages:
            body: dict = {
                "memory_type": memory_type,
                owner_field: uid,
                "app_id": app_id,
                "project_id": project_id,
                "page": page,
                "page_size": page_size,
            }
            try:
                r = await client.post(f"{base_url}/api/v1/memory/get", json=body)
                r.raise_for_status()
                data = r.json()
                d = data.get("data", {})
                items = d.get(memory_type + "s", [])
                if not items:
                    break
                for item in items:
                    if isinstance(item, dict):
                        mid = item.get("id", "")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            all_items.append(item)
                total = d.get("total_count", 0)
                if len(items) < page_size or page * page_size >= total:
                    break
                page += 1
            except Exception:
                break

        return all_items

    def _get_candidate_uids(self) -> list[str]:
        """获取用于 EverOS 查询的候选 user_id/agent_id 列表。

        包含：配置的 app_id、常见默认值、持久化的已知用户 ID、
        以及配置中手动指定的 extra_user_ids。

        每次调用都会重新读取持久化文件，确保 LLM 工具写入的新 ID
        能被 Dashboard 立即发现。
        """
        base = [
            self.config.get("app_id", "astrbot"),
            "default",
            "webui",
            "assistant",
        ]
        # 1. 从持久化文件读取（包含 LLM 工具写入的最新 ID）
        try:
            known_file = Path(self.plugin.data_dir) / "everos_known_users.json"
            if known_file.exists():
                data = json.loads(known_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for uid in data:
                        if uid not in base:
                            base.append(uid)
        except Exception:
            pass
        # 2. 合并内存中的已知用户 ID（/everos 命令追踪的）
        try:
            tracked = getattr(self.plugin, "_known_user_ids", None)
            if tracked:
                for uid in sorted(tracked):
                    if uid not in base:
                        base.append(uid)
        except Exception:
            pass
        # 3. 配置中手动指定的额外 user_id
        extra = self.config.get("extra_user_ids", "")
        if extra and isinstance(extra, str) and extra.strip():
            for uid in extra.split(","):
                uid = uid.strip()
                if uid and uid not in base:
                    base.append(uid)
        return base

    def _get_app_id(self) -> str:
        return self.config.get("app_id", "astrbot")

    def _get_project_id(self) -> str:
        return self.config.get("project_id", "default")

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
            app_id = self._get_app_id()
            project_id = self._get_project_id()
            t0 = time.monotonic()
            try:
                health = await client.get(f"{base_url}/health")
                health.raise_for_status()
                health_data = health.json()
                ok = health_data.get("status") == "ok"
                latency_ms = int((time.monotonic() - t0) * 1000)

                stats = {}
                candidate_uids = self._get_candidate_uids()
                agent_kinds = {"agent_case", "agent_skill"}
                for mtype in ("episode", "profile", "agent_case", "agent_skill"):
                    total = 0
                    for uid in candidate_uids:
                        owner_field = "agent_id" if mtype in agent_kinds else "user_id"
                        try:
                            items = await self._fetch_all_memories_for(
                                client, base_url, mtype, uid, owner_field,
                                app_id, project_id,
                            )
                            total += len(items)
                        except Exception:
                            continue
                    stats[mtype] = total

                return {
                    "healthy": ok,
                    "base_url": base_url,
                    "latency": latency_ms,
                    "app_id": health_data.get("app_id", app_id),
                    "project_id": health_data.get("project_id", project_id),
                    "stats": stats,
                }
            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return {
                    "healthy": False,
                    "error": str(e),
                    "base_url": base_url,
                    "latency": latency_ms,
                    "stats": {},
                }

        @self.app.get("/api/everos/memories")
        async def api_memories():
            """获取全部记忆（翻页直到耗尽，避免只读到前 20 条的 bug）。"""
            client = self._get_client()
            base_url = self._get_everos_url()
            app_id = self._get_app_id()
            project_id = self._get_project_id()
            candidate_uids = self._get_candidate_uids()
            try:
                all_items = []
                seen_ids = set()
                agent_kinds = {"agent_case", "agent_skill"}
                for mtype in ("episode", "profile", "agent_case", "agent_skill"):
                    for uid in candidate_uids:
                        owner_field = "agent_id" if mtype in agent_kinds else "user_id"
                        try:
                            items = await self._fetch_all_memories_for(
                                client, base_url, mtype, uid, owner_field,
                                app_id, project_id,
                            )
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
            app_id = self._get_app_id()
            project_id = self._get_project_id()

            payload = {
                "session_id": f"webui_{user_id}",
                "app_id": app_id,
                "project_id": project_id,
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
                        "app_id": app_id,
                        "project_id": project_id,
                    },
                )
                return {"ok": True, "status": "ok", "message": "记忆已写入", "data": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        @self.app.post("/api/everos/memories-by-type")
        async def api_memories_by_type(request: Request):
            """按类型获取全部记忆（翻页直到耗尽）。"""
            body = await request.json()
            raw_type = body.get("memory_type", "episode")
            # 旧版兼容：atomic_fact 已合并到 episode
            _COMPAT = {"atomic_fact": "episode"}
            memory_type = _COMPAT.get(raw_type, raw_type)
            candidate_uids = self._get_candidate_uids()
            client = self._get_client()
            base_url = self._get_everos_url()
            app_id = self._get_app_id()
            project_id = self._get_project_id()
            agent_kinds = {"agent_case", "agent_skill"}
            try:
                all_items = []
                seen_ids = set()
                for uid in candidate_uids:
                    owner_field = "agent_id" if memory_type in agent_kinds else "user_id"
                    try:
                        items = await self._fetch_all_memories_for(
                            client, base_url, memory_type, uid, owner_field,
                            app_id, project_id,
                        )
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
            app_id = self._get_app_id()
            project_id = self._get_project_id()
            try:
                resp = await client.post(
                    f"{base_url}/api/v1/memory/flush",
                    json={
                        "session_id": session_id,
                        "app_id": app_id,
                        "project_id": project_id,
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
            candidate_uids = self._get_candidate_uids()
            app_id = self._get_app_id()
            project_id = self._get_project_id()
            client = self._get_client()
            base_url = self._get_everos_url()
            try:
                all_items = []
                seen_ids = set()
                per_uid = max(1, top_k // max(len(candidate_uids), 1))
                for uid in candidate_uids:
                    try:
                        resp = await client.post(
                            f"{base_url}/api/v1/memory/search",
                            json={
                                "query": query,
                                "user_id": uid,
                                "app_id": app_id,
                                "project_id": project_id,
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
                # 正常关闭流程（stop() 触发）—— 确保 uvicorn 清理资源
                try:
                    self.server.should_exit = True
                    # 给 uvicorn 一个事件循环周期释放 socket
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
                raise  # 重新抛出，让任务进入已取消状态
            except Exception as e:
                logger.error(f"[EverOS] 独立 WebUI 运行异常: {e}")
            finally:
                self._running = False
                self.server_task = None
                if self._http_client:
                    try:
                        await self._http_client.aclose()
                    except Exception:
                        pass
                    self._http_client = None

        self.server_task = asyncio.create_task(_serve())
        logger.info(f"🌿 EverOS Dashboard → http://{host}:{port}")

    async def stop(self) -> None:
        """停止独立 WebUI 服务器，确保端口正确释放。"""
        if self.server:
            try:
                self.server.should_exit = True
            except Exception:
                pass

        if self.server_task and not self.server_task.done():
            try:
                # 等待 uvicorn 优雅关闭（让 socket 正确释放）
                await asyncio.wait_for(self.server_task, timeout=5.0)
            except asyncio.TimeoutError:
                # 5 秒未退出则强制取消
                logger.warning("[EverOS] WebUI 优雅关闭超时，强制终止")
                self.server_task.cancel()
                try:
                    await asyncio.wait_for(self.server_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self.server_task = None

        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self._running = False
        logger.info("[EverOS] 独立 WebUI 已停止")
