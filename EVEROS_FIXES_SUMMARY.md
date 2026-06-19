# EverOS Integration — Bug 修复与架构知识总结

> 日期：2026-06-20  
> 版本：v1.1.0 → fix  
> 修复人：Claude Code + czqwq

---

## 一、EverOS 记忆系统架构概览

### 1.1 三层存储

```
Markdown（真理源）  +  SQLite（状态）  +  LanceDB（向量 + BM25 + 标量索引）
```

- **Markdown**：`~/.everos/<app>/<project>/users/<owner_id>/episodes/episode-YYYY-MM-DD.md`
  - 每天一个文件，每条记忆以 `<!-- entry:ep_YYYYMMDD_NNNN -->` 标记包裹
  - 记忆条目使用 **审计表单格式**：`## 标题` → `**key**: value` → `### 分段标题`
- **SQLite**：`~/.everos/.index/sqlite/system.db` — 存储 `md_change_state` 追踪每个 markdown 文件的变更
- **LanceDB**：`~/.everos/.index/lancedb/` — 可重建的向量/BM25 索引，Dashboard 实际从这里查询

### 1.2 记忆写入流程（`POST /api/v1/memory/add`）

```
messages[] → ingest → boundary_detection → cells
    ├→ UserMemoryPipeline
    │   ├→ extract_atomic_facts → markdown writer → 每日.md
    │   ├→ extract_foresight
    │   └→ extract_user_profile
    └→ AgentMemoryPipeline (mode=agent)
        ├→ extract_agent_case
        └→ extract_agent_skill
```

关键点：
1. 消息以 `session_id` 为 key 累积在缓冲区
2. 边界检测（50 条 / 8192 token）触发后，将缓冲区切分成 `cell`
3. 每个 cell 经过 LLM 提取，生成结构化记忆写入 markdown
4. **Cascade 守护进程**监控 markdown 文件变化，同步到 LanceDB

### 1.3 owner_id 的传递链（关键路径）

这是本次调查的核心发现。`owner_id` 决定了 Dashboard 能否查到记录。

```
AstrBot 消息 sender_id（如 QQ 号 "1638501774"）
    ↓
EverOSMemorizeTool: resolved_user_id = user_id or persona_name or "default"
    ↓
POST /api/v1/memory/add: messages[].sender_id = resolved_user_id
    ↓
Ingest: CanonicalMessage.sender_id = 原始 sender_id
    ↓
Boundary: MemCell 中保留各消息的 sender_id
    ↓
UserMemoryPipeline._unique_user_senders(cell):
    提取 cell 中所有 role="user" 的消息的 sender_id
    ↓
Episode.from_algo(algo_ep, owner_id=sender_id, ...):
    每个 user sender 生成一份 Episode，owner_id = sender_id
    ↓
Markdown: frontmatter.user_id = owner_id → 写入 users/<owner_id>/episodes/episode-YYYY-MM-DD.md
    ↓
Cascade: LanceDB Episode.owner_id = owner_id
    ↓
Dashboard 查询: POST /api/v1/memory/get {user_id: candidate_uid, ...}
    → LanceDB WHERE owner_id = candidate_uid
```

**关键结论：如果 sender_id 是 "1638501774"，那么 owner_id 就是 "1638501774"，Dashboard 必须用 `user_id="1638501774"` 查询才能找到。**

### 1.4 记忆读取流程（`POST /api/v1/memory/get`）

```
GetRequest {memory_type, user_id XOR agent_id, ...}
    → GetManager.get()
    → LanceDB find_where_paginated(WHERE owner_id=X, ...)
    → GetResponse {data: {episodes: [...], total_count: N}}
```

**硬约束**：`user_id` 和 `agent_id` 互斥，必须且只能提供一个。
- `episode` / `profile` → `user_id`（user track）
- `agent_case` / `agent_skill` → `agent_id`（agent track）

这意味着**无法一次查询所有 owner 的记录**——必须按 owner_id 分别查询。

---

## 二、Dashboard 显示流程

### 2.1 组件架构

```
前端 (index.html + app.js)
    ↓ GET /api/everos/memories
独立服务器 (standalone_server.py :18766)  或  AstrBot 内嵌 (main.py)
    ↓ POST /api/v1/memory/get (每 type × uid 组合)
EverOS REST API (:8765)
    ↓ LanceDB 查询
LanceDB 索引
```

### 2.2 后端查询策略

```python
# candidate_uids 决定查询哪些 owner
candidate_uids = ["astrbot", "default", "webui", "assistant", ...tracked_ids, ...extra_user_ids]

for mtype in ("episode", "profile", "agent_case", "agent_skill"):
    for uid in candidate_uids:
        items = fetch_all(mtype, uid)  # 调用 EverOS /get，翻页到底
```

---

## 三、发现的 Bug 及修复

### 🐛 Bug #1（关键）：Dashboard 只读取前 20 条记录

**严重程度**：🔴 高  
**影响范围**：所有 Dashboard 页面（总览统计、记忆仓库、技能库）

**根因**：
EverOS `/api/v1/memory/get` 默认 `page_size=20`。Dashboard 后端只调用一次，未翻页。
如果某 `(type, owner_id)` 组合有 100 条记录，第 21-100 条**完全不可见**。

**修复方案**：
1. 在 `EverOSClient` 中新增 `memory_get_all()` 方法，自动翻页直到 `total_count` 耗尽
2. 在 `StandaloneServer` 中新增 `_fetch_all_memories_for()` 辅助方法
3. 所有获取记忆的后端 API 统一改用翻页版本

---

### 🐛 Bug #2（关键）：_get_candidate_uids 使用过期的内存缓存，不读文件

**严重程度**：🔴 高  
**影响范围**：Dashboard 无法发现 LLM 工具写入的新用户 ID 对应的记忆

**这是导致 "1638501774 记录依旧无法读取" 的直接原因：**

```
工具追踪流程:
  LLM 调用 everos_memorize(user_id="1638501774", ...)
    → _track_user_to_file("1638501774")
    → 写入 everos_known_users.json ✓
    → 但 self._known_user_ids（内存）未更新 ✗

Dashboard 查询流程:
  _get_candidate_uids()
    → 读取 self._known_user_ids（内存）→ 不包含 "1638501774" ✗
    → 从未读取 everos_known_users.json ✗
    → candidate_uids = ["astrbot", "default", "webui", "assistant"]
    → 不会用 user_id="1638501774" 查询 EverOS
    → ✗ 1638501774 的记忆完全不可见
```

**修复方案**：
`_get_candidate_uids()` 每次调用时**重新读取持久化文件**，确保 LLM 工具写入的新 ID 能被 Dashboard 立即发现，无需重启插件。

```python
def _get_candidate_uids(self):
    base = ["astrbot", "default", "webui", "assistant"]
    # 1. 从持久化文件读取（包含 LLM 工具写入的最新 ID）
    if self._known_users_path.exists():
        data = json.loads(self._known_users_path.read_text(encoding="utf-8"))
        for uid in data:
            if uid not in base:
                base.append(uid)
    # 2. 合并内存中的已知用户 ID（/everos 命令追踪的）
    for uid in sorted(self._known_user_ids):
        if uid not in base:
            base.append(uid)
    # 3. 配置中手动指定的 extra_user_ids
    ...
```

---

### 🐛 Bug #3：LLM 工具写入新用户 ID 后，只有文件更新，内存不更新

**严重程度**：🟡 中（是 Bug #2 的根源之一）  
**影响范围**：LLM 工具调用后的同一进程生命周期内

**根因**：
`_track_user_to_file()` 在 `tools/everos_tools.py` 中独立运行，只写文件。
插件主类的 `_track_user()` 方法同时更新内存 + 文件，但只在 `/everos` 命令中调用。
LLM 工具不走 `/everos` 命令，所以 `_track_user` 不被触发。

**修复方案**：
`_get_candidate_uids` 每次都从文件读取，不再依赖内存缓存（见 Bug #2 修复）。

---

### 🐛 Bug #4：Dashboard 不知道真实用户的 QQ ID

**严重程度**：🔴 高（已通过 Bug #2/#3 修复间接解决）  

**根因**：
1. `_track_user()` 只在 `/everos` **命令处理器**中调用，不在普通消息中调用
2. `_known_user_ids` 仅存内存中，插件重启即丢失
3. `candidate_uids` 是固定列表 `["astrbot", "default", "webui"]`

**修复方案**（与 Bug #2/#3 配合）：
1. LLM 工具调用时自动 `_track_user_to_file()` 持久化 user_id
2. `_get_candidate_uids` 每次都读文件
3. 新增 `extra_user_ids` 配置项供手动补充
4. `_known_user_ids` 在插件 init 时从文件恢复

---

### 🐛 Bug #5（中等）：agent_case/agent_skill 在 Dashboard 显示为原始 JSON

**严重程度**：🟡 中  
**影响范围**：Dashboard 记忆仓库中 agent_case/agent_skill 类型的卡片内容

**修复方案**：
```python
elif mtype == "agent_case":
    item["content"] = item.get("task_intent") or item.get("key_insight") or item.get("approach")
elif mtype == "agent_skill":
    item["content"] = item.get("description") or item.get("name")
```

---

### 🐛 Bug #6（低）：candidate_uids 缺少 "assistant"

**严重程度**：🟢 低  
**影响范围**：agent track 记忆（Case/Skill）的可见性

`EverOSLearnTool` 使用 `sender_id = "assistant"`，生成的 Case/Skill 的 `owner_id` 为 `"assistant"`。
添加 "assistant" 到基础 candidate_uids。

---

## 四、为什么 webui 手动导入可以正常显示？

这是本次排查的关键突破口：

| 路径 | sender_id | owner_id | candidate_uids 包含？ | 可见？ |
|------|-----------|----------|----------------------|--------|
| **WebUI 写入** | `"webui"`（硬编码） | `"webui"` | ✅ 始终在列表中 | ✅ 可见 |
| **LLM 工具（带 user_id）** | `"1638501774"`（QQ号） | `"1638501774"` | ❌ 修复前不在列表中 | ❌ 不可见 |
| **LLM 工具（不带 user_id）** | `"default"`（默认值） | `"default"` | ✅ 始终在列表中 | ⚠️ 修复前仅前20条 |
| **/everos memorize** | QQ号（通过 _get_uid） | QQ号 | ⚠️ 修复前仅内存 | ⚠️ 取决于是否追踪 |

WebUI 写入的 `sender_id` 硬编码为 `"webui"`，而 `"webui"` 始终在 `candidate_uids` 基础列表中，所以 WebUI 的记忆总能被 Dashboard 查询到。

LLM 工具如果提供了 QQ 号作为 `user_id`，`owner_id` 就是 QQ 号。修复前 `candidate_uids` 不包含 QQ 号，所以查不到。

---

## 五、修改文件清单

| 文件 | 变更 |
|------|------|
| `core/everos_client.py` | 新增 `memory_get()` 的 `page`/`page_size` 参数；新增 `memory_get_all()` 全量翻页方法 |
| `core/standalone_server.py` | 新增 `_fetch_all_memories_for()` 辅助；`api_status`/`api_memories`/`api_memories_by_type` 改用全量翻页；`_get_candidate_uids` 改为每次从文件读取 |
| `main.py` | `api_status`/`api_memories`/`api_memories_by_type` 改用 `memory_get_all()`；`_get_candidate_uids` 改为每次从文件读取；新增 `_load_known_users`/`_save_known_users` 持久化；修复 `_normalize_item` |
| `core/config_manager.py` | 新增 `extra_user_ids` 配置项及属性 |
| `tools/everos_tools.py` | 新增 `_track_user_to_file()` 持久化追踪；所有工具调用时自动记录 user_id |

---

## 六、EverOS 核心架构知识

### 6.1 owner_id 传递链（核心）

```
sender_id (AstrBot message)
    → CanonicalMessage.sender_id (Ingest)
    → MemCell item sender_id (Boundary)
    → _unique_user_senders(cell) → owner_id (UserMemoryPipeline)
    → markdown frontmatter.user_id
    → LanceDB Episode.owner_id
    → Dashboard 查询: WHERE owner_id = candidate_uid
```

### 6.2 关键约束

1. **owner 互斥**：`/get` 必须且只能提供 `user_id` 或 `agent_id` 之一
2. **app/project 隔离**：所有查询都按 `(app_id, project_id)` 过滤
3. **Cascade 异步同步**：markdown → LanceDB 有延迟（最多 30s 扫描周期）
4. **默认分页**：EverOS `/get` 默认 `page_size=20`，上限 100

### 6.3 ID 体系

| 层级 | 格式 | 示例 |
|------|------|------|
| Markdown entry ID | `<PREFIX>_YYYYMMDD_NNNN` | `ep_20250620_00000001` |
| LanceDB PK | `<owner_id>_<entry_id>` | `1638501774_ep_20250620_00000001` |
| session_id | 调用方传入 | `llm-tool-1718841600000` |
| owner_id | 来自消息 sender_id | `1638501774` (QQ号) |

---

## 七、后续建议

1. **EverOS 服务端增强**：添加 `GET /api/v1/memory/owners` 端点，返回所有存在的 owner_id 列表（通过扫描 markdown 目录），让 Dashboard 能自主发现所有用户
2. **Dashboard 实时性**：当前需手动刷新；可接入 WebSocket 或 SSE 推送 Cascade 同步事件
3. **owner 发现机制**：如果 EverOS 和 AstrBot 在同一台机器，可直接扫描 `~/.everos/` 目录获取 owner 列表
4. **统一 user_id 传递**：AstrBot 应在 LLM 工具调用时自动注入当前消息的 sender_id，而非依赖 LLM 自行填写
5. **监控 Cascade 延迟**：添加指标记录 markdown 写入到 LanceDB 可查询的端到端延迟
