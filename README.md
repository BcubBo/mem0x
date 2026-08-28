# mem0x v0.2.0

基于 [mem0](https://github.com/mem0ai/mem0) 的 AI 记忆增强服务，为 AI Agent 提供持久化、可检索、可进化的记忆能力。

本项目的设计思路和架构灵感来源于 [aiduMEI](https://github.com/monkey2jack/aiduMEI)，感谢其在 AI 记忆系统领域的探索和实践。

## 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Hermes Agent                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ TUI 会话  │  │ 飞书消息  │  │ Cron 任务 │  │ MiMo Code│       │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └─────┬────┘       │
│        └──────────────┴──────────────┴──────────────┘           │
│                           │ mem0x 插件                           │
└───────────────────────────┼─────────────────────────────────────┘
                            │ HTTP API (28768)
┌───────────────────────────┼─────────────────────────────────────┐
│                    mem0x API Server                              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  FastAPI (mem0x_server.py)                              │    │
│  │  ├── 认证中间件 (API Key + Redis 速率限制)                │    │
│  │  ├── 安全写入链路 (pipeline.py)                          │    │
│  │  │   ├── 注入防御 (L1 正则 + L2 Unicode 归一化)          │    │
│  │  │   ├── PII 脱敏 (身份证/手机/邮箱/密码)                │    │
│  │  │   ├── 去重拦截 (Jaccard + 语义相似度)                 │    │
│  │  │   ├── 矛盾消解 (规则 + LLM 单次判断)                 │    │
│  │  │   └── 写入补偿 (失败自动重试队列)                     │    │
│  │  ├── 搜索链路 (向量 + BM25 + Reranker + 评分)           │    │
│  │  └── 后台任务 (consolidation/evolve/reflect/auto_expire)│    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ 工作记忆层    │  │ 碎片合并      │  │ 自进化引擎    │          │
│  │ Redis+SQLite │  │ consolidation│  │ evolve_mem   │          │
│  │ (L1/L2双层)  │  │ +LLM摘要压缩 │  │ +质量分析    │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
┌───────┴───────┐  ┌───────┴───────┐  ┌───────┴───────┐
│   Qdrant      │  │   Redis       │  │   SQLite      │
│  (向量存储)    │  │  (缓存/限流)   │  │  (持久化)     │
│  :6333        │  │  :6379 (db0/1)│  │  6个数据库     │
│  12959条记忆   │  │  速率限制+WM缓存│  │  FTS5/salience│
│  1024维 bge-m3│  │              │  │  conflict/... │
└───────────────┘  └───────────────┘  └───────────────┘
        │
┌───────┴───────────────────────────────────────┐
│            Embedding 集群 (4节点)              │
│  nginx:28770 → n8/n9/n10/n11 (bge-m3 CPU)    │
│  每节点 7 workers, 总 28 并发                  │
└───────────────────────────────────────────────┘
        │
┌───────┴───────────────────────────────────────┐
│            Reranker 集群 (2节点)               │
│  nginx:28795 → n1/n2 (bge-reranker-v2-m3)    │
│  每节点 7 workers, 总 14 并发                  │
└───────────────────────────────────────────────┘
```

## 核心组件

### 服务层

| 组件 | 说明 |
|------|------|
| `mem0x_server.py` | FastAPI 主服务，暴露所有 HTTP 端点 |
| `security/pipeline.py` | 安全写入链路：注入防御 → PII脱敏 → 去重 → 矛盾消解 → 语义判重 |
| `security/conflict_resolver.py` | 矛盾消解：规则驱动 + 单次 LLM 判断（去投票优化） |
| `security/compensation.py` | 补偿队列：写入失败自动重试，SQLite 持久化 |
| `security/circuit_breaker.py` | 断路器：Qdrant/LLM 故障隔离 |
| `security/detection_guard.py` | 注入防御：L1 正则 + L2 Unicode 归一化 |
| `security/db_common.py` | 数据库公共模块：连接管理 + Schema 统一 |

### 记忆管理层

| 组件 | 说明 |
|------|------|
| `wrapper/working_memory.py` | **工作记忆层**：Redis L1 缓存 + SQLite L2 持久化（v0.2.0 新增） |
| `wrapper/consolidation.py` | 碎片合并：无 LLM 算法（pick_best/keyword_merge） + LLM 摘要压缩 |
| `wrapper/reconcile.py` | 三库对账：Qdrant ↔ SQLite 数据一致性检查 + 自动修复 |
| `wrapper/salience.py` | 显著性追踪：访问热度 + 衰减曲线 |
| `wrapper/fsrs_bridge.py` | FSRS-6 质量评估：标准间隔重复算法 |
| `wrapper/version_tracker.py` | 版本追踪：记忆修改历史 + 回滚能力 |
| `wrapper/core_memory.py` | 核心记忆：高重要性记忆独立存储 |
| `wrapper/reflect.py` | 反思引擎：定期质量分析 + 低质清理 |
| `wrapper/evolve_mem.py` | 自进化：质量分析 + 低质清理 |
| `wrapper/auto_expire.py` | 过期清理：lane TTL + expires 标记 |
| `wrapper/fetch_all.py` | 分页获取 + 多用户发现（绕过 mem0 get_all 限制） |

### 插件层

| 组件 | 说明 |
|------|------|
| `plugins/mem0x/` | Hermes Agent 插件：将 mem0x 注册为 Agent 工具 |

## 数据存储

### Redis (db=0: 限流/游标, db=1: 工作记忆缓存)

| Key 模式 | 类型 | 用途 |
|----------|------|------|
| `ratelimit:*` | Sorted Set | API 速率限制（滑动窗口） |
| `consolidation:cursor:*` | String | 碎片合并游标（30天TTL） |
| `wm1:item:{memory_id}` | Hash | 工作记忆条目（7字段） |
| `wm1:user:{user_id}` | Set | 用户工作记忆ID索引 |
| `wm1:count` | String | 工作记忆全局计数器 |

### SQLite 数据库

| 数据库 | 用途 | 关键表 |
|--------|------|--------|
| `fts5.db` | FTS5 全文索引 + BM25 IDF | `mem0_fts5` |
| `salience.db` | 显著性/热度追踪 | `salience` |
| `conflict.db` | 矛盾消解记录 | `conflicts` |
| `compensation.db` | 写入补偿队列 | `tasks` |
| `version_history.db` | 版本追踪 | `versions` |
| `core_memory.db` | 核心记忆 | `core_memories` |
| `reflect.db` | 反思日志 | `logs` |
| `delete_audit.db` | 删除审计 | `delete_audit` |
| `working_memory.db` | 工作记忆（L2持久化） | `working_memory` |

### Qdrant (向量存储)

| Collection | 维度 | 用途 |
|------------|------|------|
| `mem0` | 1024 | 主记忆存储（bge-m3 embedding） |
| `mem0_entities` | 1024 | 实体存储 |
| `mem0_bm25` | - | BM25 稀疏向量 |

## 模型与组件

### LLM

| 用途 | 模型 | 提供方 |
|------|------|--------|
| 矛盾消解 | MiMo v2.5 Pro | 小米 MiMo API |
| 碎片合并摘要 | MiMo v2.5 | 小米 MiMo API |
| 记忆提取 | MiMo v2.5 Pro | 小米 MiMo API |

### Embedding

| 模型 | 部署 | 并发 |
|------|------|------|
| BAAI/bge-m3 | 4节点 CPU (n8/n9/n10/n11) | 每节点7 workers, 共28并发 |
| 量化 | FP32 → INT8 | 内存限制 2GB/节点 |

### Reranker

| 模型 | 部署 | 并发 |
|------|------|------|
| BAAI/bge-reranker-v2-m3 | 2节点 CPU (n1/n2) | 每节点7 workers, 共14并发 |

### 向量数据库

| 组件 | 版本 | 说明 |
|------|------|------|
| Qdrant | latest | 向量存储 + BM25 稀疏搜索 |
| Redis | 7-alpine | 缓存 + 限流 + 游标 |

## 部署

### Docker Compose（独立编排）

```bash
cd ~/.mem0x

# 1. 启动 Redis
sudo docker compose -f docker-compose.redis.yml up -d

# 2. 启动 Embedding 集群（nginx + n8-n11）
sudo docker compose -f docker-compose.embedding-nginx.yml up -d

# 3. 启动 Reranker 集群
sudo docker compose -f docker-compose.reranker.yml up -d

# 4. 启动 Qdrant
sudo docker compose -f docker-compose.mem0x-qdrant.yml up -d

# 5. 启动 API 服务
sudo docker compose -f docker-compose.mem0x.yml up -d
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MEM0X_CONFIG` | 配置文件路径 | `/app/config.json` |
| `MEM0_TELEMETRY` | 禁用遥测 | `False` |
| `FASTEMBED_CACHE_PATH` | Embedding 缓存路径 | `/tmp/fastembed_cache` |
| `HF_HUB_OFFLINE` | 离线模式 | `1` |

### 配置文件

生产配置位于 `~/.mem0x/config-compose.json`，通过 Docker volume 挂载到容器内 `/app/config.json`。

关键配置项：

```json
{
  "mem0": {
    "llm": { "provider": "openai", "config": { "model": "mimo-v2.5-pro" } },
    "embedder": { "config": { "model": "BAAI/bge-m3", "openai_base_url": "http://mem0x-embedding-nginx:8775/v1" } },
    "vector_store": { "config": { "url": "http://qdrant:6333", "collection_name": "mem0", "embedding_model_dims": 1024 } }
  },
  "redis": { "host": "redis", "port": 6379, "db": 0 },
  "working_memory": { "enabled": true, "redis_cache": true, "db_wm": 1 },
  "scoring": { "weights": { "vector": 0.38, "time": 0.15, "reliability": 0.1, "heat": 0.17, "confidence": 0.2 } }
}
```

## API 端点

### 核心 CRUD

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/add` | 写入记忆（安全链路：注入防御→PII脱敏→去重→矛盾消解） |
| POST | `/search` | 搜索记忆（向量+BM25+Reranker+评分） |
| POST | `/update` | 更新记忆 |
| POST | `/delete` | 软删除（需 confirm_token 确认硬删） |

### 工作记忆

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/working-memory/list` | 获取用户工作记忆列表（Redis优先，SQLite兜底） |
| POST | `/working-memory/clear` | 清空工作记忆 |

### 后台任务

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/consolidate` | 触发碎片合并 |
| POST | `/evolve` | 触发自进化 |
| POST | `/reflect` | 触发反思 |
| POST | `/expire` | 触发过期清理 |
| POST | `/reconcile` | 触发三库对账 |

### 管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/stats` | 统计信息 |
| GET | `/openapi.json` | API 文档 |

## 安全

- **注入防御**：L1 正则匹配 + L2 Unicode 归一化，拦截 prompt injection
- **PII 脱敏**：身份证、手机号、邮箱、密码（支持中英文关键词）
- **API Key 认证**：`X-API-Key` header 或 `Authorization: Bearer` token
- **速率限制**：Redis 滑动窗口（add: 30次/分钟）
- **删除确认**：两步删除（软删 → confirm_token → 硬删），token 一次性 + api_key 绑定
- **跨库事务**：补偿队列覆盖 Qdrant/Neo4j/salience/FTS5 写入失败

## 版本历史

### v0.2.0 (2026-08-28)
- **Redis 工作记忆缓存层**：L1 Redis + L2 SQLite 双层架构，消除多进程并发锁争抢
- **LLM 摘要压缩**：consolidation 合并时用 LLM 生成摘要（替代简单拼接）
- **裸 except 修复**：22个文件的 bare except 全部加 logger.warning/debug
- **测试覆盖扩展**：4个新测试文件，120→126 tests

### v0.1.50 (2026-008-28)
- 3.2b LLM摘要压缩 + 裸except修复(22文件)

### v0.1.49 (2026-08-28)
- 工作记忆层 Phase 1 - SQLite持久化 + 搜索注入 + 删除联动

### v0.1.48 (2026-08-28)
- 动态衰减曲线（adaptive模式）+ consolidation阈值调优

### v0.1.47 (2026-08-28)
- consolidation归档修复 + reflect联动 + compensation迁移

### v0.1.46 (2026-08-28)
- sync_after_merge补偿兜底 + embedding/reranker/FTS5调用日志

### v0.1.45 (2026-08-28)
- FTS5根因修复 + 回填脚本 + reconcile自动回填

### v0.1.44 (2026-08-28)
- Sprint 2: reconcile + compensation delete + salience→FSRS + FTS5双写修复

### v0.1.43 (2026-08-27)
- Embedding本地化：4节点 bge-m3 CPU 集群 + nginx负载均衡
- 矛盾消解去投票：并行投票3次→单次LLM判断
- BM25 IDF 持久化 + FSRS card 持久化
- async改造 + P0/P1/P2/P3 全量修复（27项审计清零）

## 开发

```bash
# 本地测试
cd /home/ubuntu/workspace/mem0xAPI
python3 -m py_compile mem0x_server.py

# 运行测试
python -m pytest tests/ -v

# 构建镜像
sudo docker build -t mem0xapi:v0.2.0 .

# 部署
cd ~/.mem0x
sudo docker compose -f docker-compose.mem0x.yml up -d

# 版本号规则：patch 到 50 进位
# 0.1.50 → 0.2.0, 0.2.50 → 0.3.0
```

## License

MIT
