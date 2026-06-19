#!/usr/bin/env python3
"""EverOS Dashboard — 独立服务器

在 port 8766 上提供与 AstrBot 插件页面完全一致的 Dashboard UI。
通过 /api/proxy/* 反向代理到 EverOS 后端。

用法:
    python server.py [--port 8766] [--everos-url http://127.0.0.1:8765]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="EverOS Dashboard (Standalone)")

# CORS — 允许 AstrBot 内嵌 iframe 跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 运行时配置
EVEROS_BASE_URL = "http://127.0.0.1:8765"
HTTP_CLIENT: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global HTTP_CLIENT
    if HTTP_CLIENT is None:
        HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    return HTTP_CLIENT


# ─── 前端静态文件 ──────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent.resolve()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard HTML."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/{filepath:path}")
async def serve_static_files(filepath: str):
    """提供 style.css / app.js 等静态文件（根路径访问）。"""
    # 跳过 API 路径，避免覆盖代理路由
    if filepath.startswith("api/") or filepath.startswith("static/"):
        return HTMLResponse(status_code=404)
    file_path = STATIC_DIR / filepath
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return HTMLResponse(status_code=404)


# ─── API 代理 ──────────────────────────────────────────────────

@app.api_route("/api/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    """代理到 EverOS 后端。"""
    client = get_client()
    target = f"{EVEROS_BASE_URL}/{path}"
    params = dict(request.query_params)

    try:
        if request.method == "GET":
            resp = await client.get(target, params=params)
        else:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else None
            resp = await client.request(request.method, target, json=body, params=params)

        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        return JSONResponse(
            content={"ok": resp.is_success, "status_code": resp.status_code},
            status_code=resp.status_code,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"无法连接 EverOS ({target})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── 内置 Dashboard API（聚合/适配层） ───────────────────────

@app.get("/api/proxy/status")
async def proxy_status():
    """聚合健康检查 + 统计。"""
    client = get_client()
    try:
        health = await client.get(f"{EVEROS_BASE_URL}/health")
        health.raise_for_status()
        health_data = health.json()
        ok = health_data.get("status") == "ok"

        # 获取统计
        stats = {}
        candidate_uids = ["default"]
        agent_kinds = {"agent_case", "agent_skill"}
        for mtype in ("episode", "profile", "agent_case", "agent_skill"):
            total = 0
            for uid in candidate_uids:
                # agent_skill / agent_case 用 agent_id，其余用 user_id
                owner_field = "agent_id" if mtype in agent_kinds else "user_id"
                try:
                    r = await client.post(
                        f"{EVEROS_BASE_URL}/api/v1/memory/get",
                        json={"memory_type": mtype, owner_field: uid},
                    )
                    data = r.json()
                    items = data.get("data", {}).get(mtype + "s", [])
                    total += len(items)
                except Exception:
                    pass
            stats[mtype] = total

        return {
            "healthy": ok,
            "base_url": EVEROS_BASE_URL,
            "latency": None,
            "app_id": health_data.get("app_id", "everos"),
            "project_id": health_data.get("project_id", "default"),
            "stats": stats,
        }
    except Exception as e:
        return {"healthy": False, "error": str(e), "base_url": EVEROS_BASE_URL, "stats": {}}


@app.post("/api/proxy/memorize")
async def proxy_memorize(request: Request):
    """写入记忆（简化的单条接口）。"""
    body = await request.json()
    content = body.get("content", "")
    memory_type = body.get("memory_type", "episode")
    user_id = body.get("user_id", "webui")

    client = get_client()
    # 通过 memory/add 接口注入
    payload = {
        "session_id": f"webui_{user_id}",
        "app_id": "astrbot",
        "project_id": "default",
        "messages": [
            {
                "sender_id": user_id,
                "role": "user",
                "timestamp": int(time.time() * 1000),
                "content": content,
            }
        ],
    }
    try:
        resp = await client.post(f"{EVEROS_BASE_URL}/api/v1/memory/add", json=payload)
        resp.raise_for_status()
        result = resp.json()

        # 触发 flush 以强制提取
        await client.post(
            f"{EVEROS_BASE_URL}/api/v1/memory/flush",
            json={"session_id": f"webui_{user_id}", "app_id": "astrbot", "project_id": "default"},
        )

        return {"ok": True, "status": "ok", "message": "记忆已写入", "data": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/proxy/memories-by-type")
async def proxy_memories_by_type(request: Request):
    """按类型获取记忆。"""
    body = await request.json()
    raw_type = body.get("memory_type", "episode")
    # 旧版兼容：atomic_fact 已合并到 episode
    _COMPAT = {"atomic_fact": "episode"}
    memory_type = _COMPAT.get(raw_type, raw_type)
    limit = body.get("limit", 20)
    page_size = min(limit, 100)

    client = get_client()
    # agent_skill / agent_case 用 agent_id，其余用 user_id
    agent_kinds = {"agent_case", "agent_skill"}
    owner_field = "agent_id" if memory_type in agent_kinds else "user_id"
    try:
        resp = await client.post(
            f"{EVEROS_BASE_URL}/api/v1/memory/get",
            json={
                "memory_type": memory_type,
                owner_field: "default",
                "page_size": page_size,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/proxy/flush")
async def proxy_flush(request: Request):
    """触发 EverOS 记忆提炼。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    session_id = body.get("session_id", "webui")
    app_id = body.get("app_id", "astrbot")
    project_id = body.get("project_id", "default")

    client = get_client()
    try:
        resp = await client.post(
            f"{EVEROS_BASE_URL}/api/v1/memory/flush",
            json={"session_id": session_id, "app_id": app_id, "project_id": project_id},
        )
        resp.raise_for_status()
        result = resp.json()
        return {"ok": True, "status": "ok", "message": "记忆提炼已触发", "data": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/proxy/search")
async def proxy_search(request: Request):
    """语义检索。"""
    body = await request.json()
    query = body.get("query", "")
    top_k = body.get("top_k", 10)

    client = get_client()
    try:
        resp = await client.post(
            f"{EVEROS_BASE_URL}/api/v1/memory/search",
            json={
                "query": query,
                "user_id": "webui",
                "app_id": "astrbot",
                "project_id": "default",
                "top_k": top_k,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── 启动 ──────────────────────────────────────────────────────

def main():
    global EVEROS_BASE_URL

    parser = argparse.ArgumentParser(description="EverOS Dashboard Standalone Server")
    parser.add_argument("--port", type=int, default=8766, help="监听端口 (默认: 8766)")
    parser.add_argument("--everos-url", type=str, default="http://127.0.0.1:8765", help="EverOS 服务地址")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    args = parser.parse_args()

    EVEROS_BASE_URL = args.everos_url.rstrip("/")
    print(f"🌿 EverOS Dashboard → {EVEROS_BASE_URL}")
    print(f"   Listening on {args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
