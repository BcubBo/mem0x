# mem0x

AI Agent 记忆增强服务。基于 [mem0](https://github.com/mem0ai/mem0) 构建，提供持久化、可检索、可进化的记忆能力。

设计灵感来源于 [aiduMEI](https://github.com/monkey2jack/aiduMEI)，感谢其在 AI 记忆系统领域的探索和实践。

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                      客户端层                                  │
│  Hermes Agent (插件)    MCP Server (stdio)    curl / SDK      │
└──────────┬──────────────────┬─────────────────┬──────────────┘
           │ HTTP             │ HTTP            │ HTTP
┌──────────┴──────────────────┴─────────────────┴──────────────┐
│                    mem0x API Server                           │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐             │
│  │ 安全写入链路 │  │ 搜索链路    │  │ 后台任务    │             │
│  │ pipeline    │  │ 向量+BM25  │  │ consolidation│            │
│  └────────────┘  │ +Reranker  │  │ evolve/reflect│           │
│                  └────────────┘  └────────────┘             │
└──────────┬──────────────────┬─────────────────┬──────────────┘
           │                  │                 │
┌──────────┴──────┐ ┌────────┴────────┐ ┌──────┴──────┐
│   Qdrant        │ │   Redis          │ │   SQLite    │
│   向量存储       │ │   缓存+限流      │ │   持久化     │
│   (bge-m3 1024d)│ │   db0:限流/游标  │ │   6个数据库   │
│                 │ │   db1:工作记忆    │ │             │
└─────────────────┘ └─────────────────┘ └─────────────┘
           │
┌──────────┴──────────────────────────────────────────┐
│              Embedding 集群 (可选，本地部署)           │
│   nginx:28770 → n8/n9/n10/n11 (bge-m3 CPU, 28并发) │
│   nginx:28795 → n1/n2 (bge-reranker-v2-m3, 14并发) │
└─────────────────────────────────────────────────────┘
```

## 仓库结构

```
mem0x/
├── mem0x_server.py              # FastAPI 主服务（所有 HTTP 端点）
├── __init__.py                  # 包初始化 + mem0 Memory 运行时配置
├── requirements.txt             # Python 依赖
├── Dockerfile                   # Docker 镜像构建
├── config.json                  # 本地开发配置（生产由 compose 挂载覆盖）
├── config-compose.json.example  # 生产配置模板
├── config.json.example          # 本地开发配置模板
│
├── security/                    # 安全层
│   ├── pipeline.py              # 写入链路编排：注入防御→PII脱敏→去重→矛盾消解→语义判重
│   ├── injection_guard.py       # 注入防御：L1 正则 + L2 Unicode 归一化
│   ├── pii.py                   # PII 脱敏：身份证/手机/邮箱/密码
│   ├── dedup.py                 # 去重：Jaccard + 语义相似度拦截
│   ├── conflict_resolver.py     # 矛盾消解：规则驱动 + LLM 单次判断
│   ├── compensation.py          # 补偿队列：写入失败自动重试（SQLite 持久化）
│   ├── circuit_breaker.py       # 断路器：Qdrant/LLM 故障隔离
│   ├── degradation.py           # 降级策略：服务不可用时的降级逻辑
│   ├── scoring.py               # 评分权重计算
│   ├── db_common.py             # SQLite 公共模块：连接管理 + Schema 统一
│   └── utils.py                 # 工具函数：配置加载、API key 管理
│
├── wrapper/                     # 记忆管理层
│   ├── working_memory.py        # 工作记忆：Redis L1 缓存 + SQLite L2 持久化
│   ├── consolidation.py         # 碎片合并：无 LLM 算法 + LLM 摘要压缩
│   ├── reconcile.py             # 三库对账：Qdrant ↔ SQLite 数据一致性检查
│   ├── salience.py              # 显著性追踪：访问热度 + 衰减曲线
│   ├── fsrs_bridge.py           # FSRS-6 质量评估：标准间隔重复算法
│   ├── version_tracker.py       # 版本追踪：记忆修改历史 + 回滚
│   ├── core_memory.py           # 核心记忆：高重要性记忆独立存储
│   ├── reflect.py               # 反思引擎：定期质量分析 + 低质清理
│   ├── evolve_mem.py            # 自进化：质量分析 + 低质清理
│   ├── auto_expire.py           # 过期清理：lane TTL + expires 标记
│   ├── fetch_all.py             # 分页获取 + 多用户发现
│   ├── index_sync.py            # 跨存储同步：删除/合并后同步 FTS5/salience
│   ├── fts5_store.py            # FTS5 全文索引 + BM25 IDF
│   ├── sparse_vector.py         # BM25 稀疏向量
│   ├── hot_archive.py           # 热归档：高质量记忆独立存储
│   ├── evolve_lock.py           # 自进化分布式锁
│   ├── spacy_ner.py             # spaCy NER 实体提取
│   ├── tags_hook.py             # 标签 hook：NER 结果存入 Qdrant payload
│   └── mem0_runtime.py          # mem0 Memory 运行时配置
│
├── plugin/                      # Hermes Agent 插件
│   ├── __init__.py              # 插件入口：注册 mem0_search/mem0_add/mem0_update/mem0_delete
│   ├── plugin.yaml              # 插件元数据
│   └── mem0x.json.example       # 插件配置模板
│
├── mcp/                         # MCP Server（Model Context Protocol）
│   ├── mem0x_mcp_server.py      # MCP 服务端：stdio JSON-RPC，6 个工具
│   ├── mem0x_client.py          # HTTP 客户端：连接 mem0x API
│   ├── pyproject.toml           # 打包配置
│   └── README.md                # MCP 配置文档
│
├── tests/                       # 测试
│   ├── test_core.py             # 基础测试：注入防御/断路器/PII脱敏
│   ├── test_working_memory.py   # 工作记忆测试：Redis缓存/SQLite/降级
│   ├── test_consolidation.py    # 碎片合并测试：算法/LLM摘要
│   ├── test_reconcile.py        # 三库对账测试
│   └── test_compensation.py     # 补偿队列测试
│
├── scripts/                     # 运维脚本（不入库）
│   ├── backfill_fts5.py         # FTS5 回填
│   └── backfill_tags.py         # 标签回填
│
└── docs/                        # 内部文档（不入库）
    ├── FINAL_PLAN.md
    ├── REFACTOR_PLAN.md
    ├── UNIFIED_API_DESIGN.md
    └── WRITE_RELIABILITY_PLAN.md
```

## 部署

### 前置条件

- Docker + Docker Compose v2
- 至少 4GB 可用内存（Embedding 集群可选，最小部署仅需 1GB）

### 最小部署（API + Redis + Qdrant）

仅需 3 个容器即可运行：

```bash
mkdir -p ~/.mem0x/data
cd ~/.mem0x

# 1. 复制配置模板并填入你的 API key
cp /path/to/mem0x/config-compose.json.example ~/.mem0x/config-compose.json
cp /path/to/mem0x/docker-compose.mem0x.yml.example ~/.mem0x/docker-compose.mem0x.yml
cp /path/to/mem0x/docker-compose.mem0x-qdrant.yml.example ~/.mem0x/docker-compose.mem0x-qdrant.yml

# 2. 编辑配置（必须修改的项）
#    - mem0.llm.config.api_key
#    - mem0.embedder.config.api_key（如果用云端 embedding）
#    - server.api_key
#    - mem0.vector_store.config.url（如果 Qdrant 不在同一 Docker 网络）

# 3. 启动
sudo docker compose -f docker-compose.redis.yml up -d
sudo docker compose -f docker-compose.mem0x-qdrant.yml up -d
sudo docker compose -f docker-compose.mem0x.yml up -d

# 4. 验证
curl http://127.0.0.1:28768/health
```

### 完整部署（含本地 Embedding 集群）

```bash
cd ~/.mem0x

# 基础服务
sudo docker compose -f docker-compose.redis.yml up -d
sudo docker compose -f docker-compose.mem0x-qdrant.yml up -d

# Embedding 集群（bge-m3，4节点 × 7 workers = 28 并发）
sudo docker compose -f docker-compose.embedding-nginx.yml up -d

# Reranker 集群（bge-reranker-v2-m3，2节点 × 7 workers = 14 并发）
sudo docker compose -f docker-compose.reranker.yml up -d

# API 服务
sudo docker compose -f docker-compose.mem0x.yml up -d
```

### Docker Compose 文件说明

| 文件 | 用途 |
|------|------|
| `docker-compose.redis.yml` | Redis：速率限制 + 工作记忆缓存 + consolidation 游标 |
| `docker-compose.mem0x-qdrant.yml` | Qdrant：向量存储 |
| `docker-compose.mem0x.yml` | mem0x API 服务 |
| `docker-compose.embedding-nginx.yml` | Embedding 集群：nginx 负载均衡 + 4 节点 bge-m3 |
| `docker-compose.reranker.yml` | Reranker 集群：nginx + 2 节点 bge-reranker-v2-m3 |

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MEM0X_CONFIG` | 配置文件路径 | `/app/config.json` |
| `MEM0X_DATA_DIR` | 数据目录 | `/app/data` |
| `MEM0_TELEMETRY` | 禁用 mem0 遥测 | `False` |
| `DO_NOT_TRACK` | 禁用遥测（通用标准） | `1` |
| `FASTEMBED_CACHE_PATH` | fastembed 模型缓存 | `/tmp/fastembed_cache` |
| `HF_HUB_OFFLINE` | HuggingFace 离线模式 | `1` |

## 配置

### 配置文件层级

```
生产环境：~/.mem0x/config-compose.json → 挂载到容器 /app/config.json
本地开发：./config.json（或环境变量 MEM0X_CONFIG 指定）
模板文件：config-compose.json.example / config.json.example
```

### 核心配置项

复制 `config-compose.json.example` 后，必须修改以下项：

```jsonc
{
  "mem0": {
    "llm": {
      "config": {
        "model": "你的LLM模型名",
        "api_key": "你的LLM API Key",
        "openai_base_url": "你的LLM API地址"
      }
    },
    "embedder": {
      "config": {
        "model": "BAAI/bge-m3",
        "api_key": "你的Embedding API Key（云端时填写）",
        "openai_base_url": "你的Embedding API地址（云端时填写）"
      }
    },
    "vector_store": {
      "config": {
        "url": "http://qdrant:6333",
        "embedding_model_dims": 1024
      }
    }
  },
  "server": {
    "port": 28768,
    "api_key": "你的服务器API Key"
  }
}
```

### 本地 Embedding 集群配置

如果使用本地 bge-m3 替代云端 Embedding API，修改 `embedder.config`：

```jsonc
{
  "mem0": {
    "embedder": {
      "config": {
        "model": "BAAI/bge-m3",
        "api_key": "not-needed",
        "openai_base_url": "http://mem0x-embedding-nginx:8775/v1"
      }
    }
  }
}
```

对应 docker-compose.embedding-nginx.yml 会启动：
- nginx 负载均衡器（端口 8775）
- 4 个 bge-m3 CPU 节点（每节点 7 workers，共 28 并发）
- 内存限制 2GB/节点

### 本地 Reranker 集群配置

```jsonc
{
  "rerank": {
    "provider": "openai_compatible",
    "config": {
      "model": "BAAI/bge-reranker-v2-m3",
      "api_key": "not-needed",
      "openai_base_url": "http://mem0x-reranker-nginx:8795/v1"
    }
  }
}
```

### 禁用 mem0 PostHog 追踪

mem0 默认通过 PostHog 收集使用数据。通过以下环境变量禁用（已在 Dockerfile 和 compose 中配置）：

```bash
MEM0_TELEMETRY=False
DO_NOT_TRACK=1
```

如果仍有追踪请求，可在代码中彻底禁用：

```python
import mem0.memory.telemetry as telemetry
telemetry.disable_telemetry()
```

### 禁止 fastembed/spaCy 首次运行下载

fastembed 和 spaCy 默认在首次使用时从网络下载模型，在生产环境可能因网络问题卡住。

**解决方案：在 Dockerfile 中预装模型**（已内置）

```dockerfile
# fastembed 模型通过 volume 挂载缓存
volumes:
  - /path/to/fastembed-cache:/tmp/fastembed_cache

# spaCy 模型在构建时安装（不从网络下载）
COPY en_core_web_sm-3.8.0.tar.gz /tmp/
RUN tar xzf /tmp/en_core_web_sm-3.8.0.tar.gz -C /usr/local/lib/python3.12/site-packages/
COPY zh_core_web_sm-3.8.0.tar.gz /tmp/
RUN pip install --no-cache-dir /tmp/zh_core_web_sm-3.8.0.tar.gz
```

**离线构建**：将 `.tar.gz` 模型文件放在仓库根目录（已在 `.gitignore` 中排除），构建镜像时不会触发网络下载。

```bash
# 首次下载模型（需要网络）
pip download fastembed -d /tmp/fastembed-pkgs
python -m spacy download zh_core_web_sm
python -m spacy download en_core_web_sm

# 离线构建镜像
sudo docker build --network=none -t mem0xapi:v0.2.0 .
```

## Hermes Agent 插件

### 安装

将 `plugin/` 目录复制到 Hermes 插件目录：

```bash
cp -r /path/to/mem0x/plugin ~/.hermes/profiles/<profile>/plugins/mem0x
```

### 配置

编辑 `~/.hermes/profiles/<profile>/plugins/mem0x/mem0x.json`：

```json
{
  "service_url": "http://127.0.0.1:28768",
  "user_id": "你的用户ID",
  "agent_id": "hermes"
}
```

### 注册的工具

| 工具名 | 说明 |
|--------|------|
| `mem0_search` | 语义搜索记忆 |
| `mem0_add` | 写入记忆（自动走安全链路） |
| `mem0_update` | 更新记忆 |
| `mem0_delete` | 软删除记忆 |

## MCP Server

mem0x-mcp 是独立的 MCP 服务，通过 JSON-RPC stdio 协议为 Claude Code、MiMo Code 等编码 Agent 提供记忆能力。

### 安装

```bash
cd mcp/
pip install .
# 或直接运行
python mem0x_mcp_server.py
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEM0X_URL` | `http://127.0.0.1:28768` | mem0x API 地址 |
| `MEM0X_API_KEY` | (空) | API Key（服务端开启认证时必填） |
| `MEM0X_AGENT_ID` | `mimocode` | Agent 标识，用于记忆归属 |

### Claude Code 配置

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "mem0x": {
      "command": "python3",
      "args": ["/path/to/mem0x/mcp/mem0x_mcp_server.py"],
      "env": {
        "MEM0X_URL": "http://127.0.0.1:28768",
        "MEM0X_API_KEY": "你的API Key",
        "MEM0X_AGENT_ID": "claude-code"
      }
    }
  }
}
```

### MiMo Code 配置

编辑 `~/.config/mimocode/mimocode.jsonc`：

```json
{
  "mcp": {
    "mem0x": {
      "type": "local",
      "command": ["python3", "/path/to/mem0x/mcp/mem0x_mcp_server.py"],
      "environment": {
        "MEM0X_URL": "http://127.0.0.1:28768",
        "MEM0X_API_KEY": "你的API Key"
      }
    }
  }
}
```

### 提供的工具

| 工具 | 说明 |
|------|------|
| `search_memory` | 语义搜索 + 知识图谱召回 |
| `add_memory` | 写入记忆（自动注入防御/PII脱敏/去重/矛盾消解） |
| `update_memory` | 更新记忆 |
| `delete_memory` | 软删除 |
| `get_graph` | 导出知识图谱（实体+关系） |
| `get_stats` | 存储统计 |

## API 端点

### 认证

所有端点（`/health` 除外）需要 API Key：

```bash
# 方式 1：X-API-Key header
curl -H "X-API-Key: YOUR_KEY" http://127.0.0.1:28768/...

# 方式 2：Authorization Bearer
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:28768/...
```

### 核心 CRUD

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/add` | 写入记忆（安全链路） |
| POST | `/search` | 搜索记忆（向量+BM25+Reranker） |
| POST | `/update` | 更新记忆 |
| POST | `/delete` | 软删除（需 confirm_token 确认硬删） |

### 工作记忆

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/working-memory/list` | 获取工作记忆（Redis 优先，SQLite 兜底） |
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
| GET | `/health` | 健康检查（免认证） |
| GET | `/stats` | 统计信息 |

## 安全机制

| 机制 | 说明 |
|------|------|
| 注入防御 | L1 正则匹配 + L2 Unicode 归一化，拦截 prompt injection |
| PII 脱敏 | 身份证号、手机号、邮箱、密码（中英文关键词） |
| API Key 认证 | X-API-Key header 或 Bearer token |
| 速率限制 | Redis 滑动窗口（默认 add: 30 次/分钟） |
| 删除确认 | 两步删除（软删 → confirm_token → 硬删） |
| 补偿队列 | 写入失败自动重试（SQLite 持久化，支持 add/delete） |
| 断路器 | Qdrant/LLM 故障隔离，自动熔断恢复 |
| 矛盾消解 | 新旧记忆冲突时自动判断，保留正确版本 |

## 存储

### Redis

| db | 用途 | Key 模式 |
|----|------|----------|
| 0 | 速率限制 | `ratelimit:*` (Sorted Set) |
| 0 | consolidation 游标 | `consolidation:cursor:*` |
| 1 | 工作记忆缓存 | `wm1:item:*` (Hash), `wm1:user:*` (Set), `wm1:count` |

AOF 持久化已启用，重启不丢数据。

### SQLite

| 数据库 | 用途 |
|--------|------|
| `fts5.db` | FTS5 全文索引 + BM25 IDF |
| `salience.db` | 显著性/热度追踪 |
| `conflict.db` | 矛盾消解记录 |
| `compensation.db` | 补偿队列 |
| `version_history.db` | 版本追踪 |
| `core_memory.db` | 核心记忆 |
| `reflect.db` | 反思日志 |
| `delete_audit.db` | 删除审计 |
| `working_memory.db` | 工作记忆（L2 持久化） |

### Qdrant

| Collection | 维度 | 说明 |
|------------|------|------|
| `mem0` | 1024 | 主记忆存储 |
| `mem0_entities` | 1024 | 实体存储 |
| `mem0_bm25` | - | BM25 稀疏向量 |

## 测试

```bash
cd /home/ubuntu/workspace/mem0xAPI

# 运行全部测试
python -m pytest tests/ -v

# 运行单个模块
python -m pytest tests/test_working_memory.py -v

# 代码检查
python -m py_compile mem0x_server.py
```

测试使用临时 SQLite 文件和 mock Qdrant，不碰真实数据库。

## 版本号规则

patch 位到 50 时进位到 minor 位：

```
0.1.50 → 0.2.0
0.2.50 → 0.3.0
```

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v0.2.0 | 2026-08-28 | Redis 工作记忆缓存层、LLM 摘要压缩、裸 except 修复、测试覆盖 |
| v0.1.43 | 2026-08-27 | Embedding 本地化、矛盾消解去投票、BM25/FSRS 持久化、P0-P3 审计修复 |
| v0.1.27 | 2026-08-26 | IndexSync 跨存储同步、consolidation 无 LLM 算法、Redis 游标 |
| v0.1.17 | 2026-08-24 | 全量 async 迁移、API Key 认证、Redis 速率限制 |

## License

MIT

## 致谢

- [mem0](https://github.com/mem0ai/mem0) — 核心记忆框架
- [aiduMEI](https://github.com/monkey2jack/aiduMEI) — 架构设计灵感
- [Qdrant](https://github.com/qdrant/qdrant) — 向量数据库
- [FSRS](https://github.com/open-spaced-repetition/fsrs-rs) — 间隔重复算法
- [fastembed](https://github.com/qdrant/fastembed) — 本地 Embedding 推理
- [spaCy](https://github.com/explosion/spaCy) — NER 实体提取
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Agent 框架
- [MiMo](https://github.com/XiaoMi/mimo) — LLM / Reranker 模型
