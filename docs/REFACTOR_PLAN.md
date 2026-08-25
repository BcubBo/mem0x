# mem0x v0.2.0 综合重构计划

> 基于：审计报告（2026-08-25）+ 统一 API 网关设计
> 目标：修审计 P0/P1 + 统一 API + 模块拆分 + 安全加固

---

## 一、审计发现 vs 我们的方案覆盖

| 审计问题 | 级别 | 实际情况 | 我们的方案 |
|----------|------|----------|-----------|
| prefetch 缺 user_id | 🔴 P0 | ✅ 真实问题 | 统一 /api 自动注入 |
| user_id 三足鼎立 | 🔴 P0 | ✅ 真实问题 | header 统一注入 + config 单一来源 |
| 5 个端点缺 await（500 回归） | 🔴 P0 | ✅ 真实问题 | 需修 |
| /expire 参数错位 | 🔴 P0 | ✅ 真实问题 | 需修 |
| 3 份插件拷贝 + .bak | 🔴 P0 | ✅ 真实问题 | 模块拆分后只保留 plugin/ |
| API Key 配置"分裂" | 🟠 P1 | ⚠️ 夸大（实际是"分治"，两端各自配各自的 key） | 保持现状，不需要统一 |
| 默认裸奔（无 key 免认证） | 🟠 P1 | ❌ Docker 部署有认证，仅非 Docker 免认证 | 非 Docker 启动时 warning |
| compose 无 redis 服务 | 🟠 P1 | ✅ 真实问题（配置有但服务没定义） | 外部 redis 或 compose 补 redis 服务 |
| 召回侧无注入边界 | 🟠 P1 | ✅ 真实问题 | prefetch/search 加 [DATA] 包裹 |
| Dockerfile 构建必败 | 🟠 P1 | ❌ 审计错（tar.gz 被源码包排除导致误判，实际文件存在） | 无需修复 |
| 搜索 N+1 | 🟠 P2 | ✅ 真实问题 | 批量化 |
| LLM 阻塞事件循环 | 🟠 P2 | ✅ 真实问题 | asyncio.to_thread |
| self_edit 死代码 | 🟡 P3 | ✅ 真实问题（import但从未调用） | 删除或接线 |
| bMem0X 旧代号 | 🟡 P3 | ✅ 真实问题（5处） | 清理 |
| 版本号三副面孔 | 🟡 P3 | ✅ 真实问题 | 统一 |
| 0 测试 | 🔴 | ✅ 真实问题 | 补冒烟测试 |
| 插件3份拷贝 | 🔴 P0 | ⚠️ 已修复（审计时还没删） | 保持现状 |

---

## 二、统一用户 ID 解决方案

### 问题
三处默认值不一致：
- 服务端：`"default"`（env MEM0X_DEFAULT_USER）
- 插件：`"yang"`（config 默认值）
- wrapper 模块：硬编码 `"bo"`（5 处）

### 方案
**单一来源：plugin config 的 `user_id` 字段**

```
插件 config (mem0x.json) → user_id: "bo"
    ↓ 自动注入 X-User-ID header
服务端 → header.user_id → 使用
wrapper 模块 → 从 config 读取，不再硬编码
```

**改动清单：**

| 文件 | 改动 |
|------|------|
| `plugin/_config.py` | `get_user_id()` 从 config 读，默认 `"default"` |
| `plugin/_client.py` | `call()` 自动注入 `X-User-ID` header |
| `wrapper/consolidation.py:391,496` | `user_id: str = "bo"` → `user_id: str = None`，函数内 fallback 到 config |
| `wrapper/evolve_mem.py:27,175` | 同上 |
| `wrapper/reflect.py:66,137` | 同上 |
| `wrapper/auto_expire.py:85` | 同上 |
| `security/pipeline.py:146` | `filters["user_id"] = "bo"` → 从参数传入 |
| `mem0x_server.py` env fallback | `"default"` 保留（兜底），但日志 warning |

---

## 三、实施阶段

### Phase 1：紧急修复（P0，1-2天）

#### 1.1 修复 5 个 500 回归端点
```
mem0x_server.py:
  POST /consolidate    → 加 await
  POST /evolve         → 加 await
  GET  /evolve/quality → 加 await
  POST /reflect        → 加 await
  GET  /reflect/health → 加 await
```

#### 1.2 修复 /expire 参数错位
```python
# auto_expire.py:85
# 旧：def run_expire_cycle(neo4j_hook=None, user_id="bo")
# 新：调用方传参对齐
mem0x_server.py → run_expire_cycle(neo4j_hook=hook, user_id=user_id)
```

#### 1.3 清理插件冗余拷贝
```
删除：
  __init__.py（392行旧版）
  mem0x/__init__.py（392行旧版）
  __init__.py.bak
  plugin/__init__.py.bak
只保留 plugin/ 目录
```

#### 1.4 prefetch 补 user_id（统一 API 前的临时方案）
```python
# plugin/__init__.py prefetch()
body = {
    "query": query, "limit": 8, "rerank": True,
    "user_id": _get_user_id(),  # 新增
    "agent_id": _get_agent_id(),  # 新增
}
```

### Phase 2：统一 API + 模块拆分（P0-P1，3-5天）

#### 2.1 插件端模块拆分
```
plugin/
├── __init__.py      # ≤20行：register + get_provider
├── _client.py       # 线程安全 HTTP 客户端，统一 /api 出口
├── _config.py       # 配置加载，单例 + Lock
├── provider.py      # Mem0RemoteProvider 业务逻辑
├── plugin.yaml      # 不变
└── mem0x.json.example  # 补 auth.api_key 字段
```

#### 2.2 服务端模块拆分
```
mem0x/
├── __init__.py      # app 创建 + lifespan
├── models.py        # 所有 Request/Response 模型
├── auth.py          # verify_api_key + rate limiting
├── unified.py       # POST /api 统一入口
├── handlers.py      # 业务逻辑 handler
├── compat.py        # 旧端点兼容（deprecated 日志）
├── audit.py         # 审计日志
└── deletion.py      # 删除确认 token
```

#### 2.3 统一 /api 端点
- 所有请求走 POST /api
- headers 自动注入 user_id/agent_id
- 服务端统一提取，路由到 handler

#### 2.4 user_id 统一
- wrapper 模块硬编码 "bo" → 从 config 读
- pipeline.py 硬编码 "bo" → 从参数传
- config example 统一默认值

#### 2.5 配置文件重命名
- `config.json` → `mem0x-server.json`
- `config.json.example` → `mem0x-server.json.example`
- `config-compose.json.example` → `mem0x-server-compose.json.example`
- `security/utils.py` 加载逻辑更新（向后兼容旧文件名）
- 补全 `server.api_key` 和 `redis` 段

### Phase 3：安全加固（P1，2-3天）

#### 3.1 非 Docker 环境安全
- `mem0x-server.json`（非 Docker）补 `server.api_key` 和 `redis` 段（可选）
- 启动时未配置 key → 打 warning 日志
- 保持"分治"模式：服务端和插件各自配各自的 key

#### 3.2 召回侧注入边界
```python
# plugin/provider.py prefetch()
lines.append(f"[MEMORY-DATA] {mem} [/MEMORY-DATA] (score: {score:.2f})")

# plugin/provider.py handle_tool_call mem0_search
# 返回结果加前缀说明
```

#### 3.3 端口绑定安全
- compose 改为 `127.0.0.1:28768:28768`
- config example 加注释说明

### Phase 4：性能 + 代码质量（P2-P3，3-5天）

#### 4.1 搜索 N+1 批量化
- `_update_usage_stats_sync` 合并为批量 update
- `boost_salience_for_results` 改批量写

#### 4.2 LLM 调用异步化
- 矛盾消解 LLM 调用改 `asyncio.to_thread`
- OpenAI client 设 timeout
- ThreadPoolExecutor 复用（不每次新建）

#### 4.3 死代码清理
- `self_edit.py` 删除（import从未调用）
- `rate_limit` 空依赖删除
- bMem0X 旧代号清理（5处）

#### 4.4 版本号统一
- FastAPI version、compose image、README、plugin.yaml 统一

### Phase 5：测试（贯穿全程）

#### 5.1 冒烟测试
```python
tests/test_smoke.py:
  - POST /api {action: "add"} → 200
  - POST /api {action: "search"} → 200 + results
  - POST /api {action: "delete"} → 200
  - POST /api {action: "update"} → 200
  - GET /health → 200
  - 旧端点兼容 → 200 + deprecated 日志
```

#### 5.2 注入防御测试
```python
tests/test_injection.py:
  - 教科书注入 → 拦截
  - 结构性变体 → 拦截（补词表）
  - 召回侧 [MEMORY-DATA] 边界验证
```

#### 5.3 用户 ID 一致性测试
```python
tests/test_user_id.py:
  - header X-User-ID 优先
  - config fallback
  - env fallback
  - 默认值 "default"
```

---

## 四、配置项现状与补全

### 配置文件重命名

| 旧名 | 新名 | 说明 |
|------|------|------|
| `config.json` | `mem0x-server.json` | 服务端配置（非 Docker） |
| `config.json.example` | `mem0x-server.json.example` | 脱敏示例 |
| `config-compose.json.example` | `mem0x-server-compose.json.example` | compose 版示例 |

**加载逻辑改动**（`security/utils.py`）：
```python
# 旧：查找 config.json
# 新：查找 mem0x-server.json（向后兼容 config.json）
1. env MEM0X_CONFIG
2. ~/.mem0x/mem0x-server.json
3. ./mem0x-server.json
4. 回退：~/.mem0x/config.json（兼容旧版）
5. 回退：./config.json（兼容旧版）
```

### 实际配置状态（审计报告纠正）

审计报告基于脱敏源码包，结论偏保守。实际生产环境：

| 配置项 | Docker（config-compose.json） | 非 Docker（config.json） | 插件（mem0x.json） |
|--------|------------------------------|------------------------|-------------------|
| server.api_key | ✅ 已配置 | ❌ 缺失（本地开发免认证） | — |
| redis | ✅ 已配置（host: redis） | ❌ 缺失（无 Redis 降级内存） | — |
| auth.api_key | — | — | ✅ 已配置 |
| user_id | — | — | ✅ "bo" |
| agent_id | — | — | ✅ "hermes" |

**审计报告纠正**：
- "默认裸奔" → ❌ Docker 部署有认证，仅非 Docker 本地开发免认证
- "redis 限流不成立" → ⚠️ 配置有但 compose 没定义 redis 服务（需外部 redis）
- "API Key 配置分裂" → 实际是"分治"（服务端/插件各自配各自的 key）

### 需要补全的配置

| 配置文件 | 补全字段 | 默认值 | 说明 |
|----------|----------|--------|------|
| `mem0x-server.json`（非 Docker） | `server.api_key` | `""`（空=免认证，启动时 warning） | 本地开发可选 |
| `mem0x-server.json`（非 Docker） | `redis.host/port/db` | `"127.0.0.1"/6379/0` | 本地开发可选 |
| `mem0x-server.json.example` | 同上（脱敏示例） | — | 供参考 |
| `mem0x-server-compose.json.example` | 同上 | — | compose 版示例 |

---

## 五、文件变更总览

### 新建（Phase 2）
```
plugin/_client.py
plugin/_config.py
plugin/provider.py
mem0x/__init__.py
mem0x/models.py
mem0x/auth.py
mem0x/unified.py
mem0x/handlers.py
mem0x/compat.py
mem0x/audit.py
mem0x/deletion.py
tests/test_smoke.py
tests/test_injection.py
tests/test_user_id.py
```

### 删除
```
__init__.py（旧版）
mem0x/__init__.py（旧版，重建后覆盖）
__init__.py.bak
plugin/__init__.py.bak
security/self_edit.py（死代码，import从未调用）
```

### 重写
```
plugin/__init__.py（409行 → ≤20行）
mem0x_server.py（1309行 → 拆分到上述文件）
```

### 修改
```
wrapper/consolidation.py（user_id 硬编码）
wrapper/evolve_mem.py（user_id 硬编码）
wrapper/reflect.py（user_id 硬编码）
wrapper/auto_expire.py（user_id 硬编码）
security/pipeline.py（user_id 硬编码）
security/utils.py（config 加载逻辑 + 重命名兼容）
config.json → mem0x-server.json（重命名 + 补字段）
config.json.example → mem0x-server.json.example（重命名）
config-compose.json.example → mem0x-server-compose.json.example（重命名）
plugin/mem0x.json.example（补字段）
```

---

## 六、风险控制

| 风险 | 缓解 |
|------|------|
| 模块拆分引入 import 错误 | py_compile 逐文件验证 |
| 旧端点兼容期行为不一致 | compat.py 委托给同一个 handler |
| wrapper user_id 改动影响后台任务 | 先在测试环境验证 consolidation/evolve/reflect |
| Dockerfile 改动影响部署 | 改完本地 docker build 验证 |
| 注入词表补充可能误拦正常内容 | 人工审核新增规则 |

---

## 七、时间估算

| 阶段 | 工作量 | 依赖 |
|------|--------|------|
| Phase 1 紧急修复 | 1-2天 | 无 |
| Phase 2 统一 API + 模块拆分 | 3-5天 | Phase 1 |
| Phase 3 安全加固 | 2-3天 | Phase 2 |
| Phase 4 性能 + 代码质量 | 3-5天 | Phase 2 |
| Phase 5 测试 | 贯穿全程 | — |
| **总计** | **9-15天** | — |
