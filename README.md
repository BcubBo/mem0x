# mem0x

自托管 AI 记忆增强服务，基于 [mem0ai](https://github.com/mem0ai/mem0) 构建。为 AI Agent 提供持久化记忆能力，支持向量搜索、知识图谱、智能去重和矛盾消解。

## 架构

```
┌─────────────┐     ┌──────────────┐     ┌─────────┐
│ Hermes Agent│────▶│  mem0x API   │────▶│ Qdrant  │  向量存储
│  (插件层)   │     │  (FastAPI)   │────▶│ Neo4j   │  知识图谱
└─────────────┘     └──────────────┘     └─────────┘
                           │
                    ┌──────┴──────┐
                    │   LLM API   │  mimo-v2.5-pro (token-plan)
                    └─────────────┘
```

## 核心功能

| 功能 | 说明 |
|------|------|
| **双端同步** | Qdrant 向量存储 + Neo4j 知识图谱，写入时双端同步 |
| **智能搜索** | 6维打分（向量+BM25+时间+可靠性+热度+置信度）+ Rerank 重排序 |
| **图谱联想召回** | 搜索时自动提取实体 → Neo4j 2跳关联查询 → 补充召回 |
| **矛盾消解** | 实体对齐 + 规则收窄 + LLM并行投票（可配置轮数/投票数），旧记忆自动归档 |
| **记忆溯源** | 写入时携带 sender metadata（sender_open_id, chat_type, chat_id） |
| **核心记忆** | 区分长期稳定记忆和普通记忆 |
| **自动维护** | 过期清理、记忆整合、自进化（FSRS质量评分）、反思分析 |
| **版本追踪** | 每次更新自动保存历史版本，支持回溯 |
| **热知识归档** | 高频访问的记忆自动升级为核心记忆 |
| **安全防护** | 注入防御（L1-L4）、PII脱敏、Jaccard去重 |
| **BM25关键词搜索** | fastembed 实现，与向量搜索融合 |
| **Hermes 集成** | MemoryProvider 插件（prefetch + sync_turn + tool_call） |
| **使用维度追踪** | 追踪 search_count、update_count、last_accessed_at |

## 写入链路

```
触发入口（mem0_add / sync_turn / on_pre_compress）
    ↓
safe_add()
    ├─ 1. 注入防御（injection_guard）
    ├─ 2. PII脱敏（pipeline.redact_pii）
    ├─ 3. 搜索候选（mem0 search, top_k=5）
    ├─ 4. 矛盾消解（复用搜索结果 + LLM并行投票）
    ├─ 5. Jaccard去重（find_duplicate）
    └─ 6. 写入（mem0 add, infer=True → LLM事实提取）
```

## 目录结构

```
mem0x/
├── mem0x_server.py          # FastAPI 服务入口
├── plugin/                  # Hermes 插件
│   ├── __init__.py          # MemoryProvider 实现
│   ├── plugin.yaml          # 插件元数据
│   └── mem0x.json.example   # 插件配置示例
├── wrapper/                 # 核心模块
│   ├── mem0_runtime.py      # mem0 运行时（单例+配置+rerank）
│   ├── auto_expire.py       # 自动过期（Qdrant scroll，零 embedding）
│   ├── consolidation.py     # 记忆整合（碎片合并）
│   ├── core_memory.py       # 核心记忆管理
│   ├── evolve_mem.py        # 自进化（LLM 质量分析）
│   ├── reflect.py           # 反思引擎
│   ├── neo4j_hook.py        # Neo4j 集成（实体提取+2跳图谱联想）
│   ├── salience.py          # 显著性引擎（热度追踪）
│   ├── graph_export.py      # 图谱导出
│   ├── hot_archive.py       # 热知识归档
│   └── version_tracker.py   # 版本追踪
├── security/                # 安全模块
│   ├── pipeline.py          # 安全写入管道（PII脱敏）
│   ├── scoring.py           # 6维打分 + Ignition
│   ├── conflict_resolver.py # 矛盾消解（实体对齐+规则+LLM投票）
│   ├── dedup.py             # Jaccard 去重
│   ├── injection_guard.py   # 三层注入防御（L1-L4）
│   ├── self_edit.py         # LLM 语义判重
│   └── degradation.py       # 降级追踪器
├── Dockerfile
├── docker-compose.mem0x.yml
├── requirements.txt
├── config.json.example
└── config-compose.json.example
```

## 快速开始

### Docker 部署（推荐）

```bash
# 克隆
git clone https://github.com/BcubBo/mem0x.git
cd mem0x

# 准备配置
mkdir -p ~/.mem0x/data
cp config-compose.json.example ~/.mem0x/config-compose.json
# 编辑 ~/.mem0x/config-compose.json 填入你的 API key

# 构建并启动
docker build -t mem0xapi:0.1.15 .
cd ~/.mem0x && docker compose -f docker-compose.mem0x.yml up -d

# 验证
curl http://localhost:28768/health
```

### 本地运行

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.json.example config.json
# 编辑 config.json 填入你的 API key

python mem0x_server.py
```

## 配置

### 配置文件优先级

1. 环境变量 `MEM0X_CONFIG`
2. `~/.mem0x/config-compose.json`（Docker，挂载到容器 `/app/config.json`）
3. 项目目录 `config.json`（本地运行）

### Docker 网络

Docker 部署时服务地址必须使用 Docker 网络名称：

```json
{
  "mem0": {
    "vector_store": {
      "config": {
        "url": "http://qdrant:6333"
      }
    }
  },
  "neo4j": {
    "uri": "bolt://neo4j:7687"
  },
  "server": {
    "host": "0.0.0.0"
  }
}
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `MEM0X_CONFIG` | 配置文件路径 |
| `MEM0_TELEMETRY` | 设为 `False` 禁用 PostHog |
| `DO_NOT_TRACK` | 设为 `1` 禁用追踪 |
| `FASTEMBED_CACHE_PATH` | fastembed 模型缓存目录（Docker 设为 `/tmp/fastembed_cache`） |
| `HF_HUB_OFFLINE` | 设为 `1` 禁止 HuggingFace 联网下载 |

### 配置文件详解

#### LLM 配置（事实提取 + 矛盾消解 + 整合）

```json
{
  "mem0": {
    "llm": {
      "provider": "openai",
      "config": {
        "model": "mimo-v2.5-pro",
        "api_key": "YOUR_API_KEY",
        "openai_base_url": "https://YOUR_MIMO_API_BASE_URL/v1",
        "max_tokens": 5000
      }
    }
  },
  "conflict": {
    "llm": {
      "provider": "openai",
      "config": {
        "model": "mimo-v2.5-pro",
        "api_key": "YOUR_API_KEY",
        "openai_base_url": "https://YOUR_MIMO_API_BASE_URL/v1",
        "max_tokens": 5000,
        "max_llm_calls": 1,
        "num_votes": 1
      }
    },
    "auto_archive_threshold": 0.8,
    "notify_threshold": 0.5
  },
  "consolidation": {
    "llm": {
      "provider": "openai",
      "config": {
        "model": "mimo-v2.5-pro",
        "api_key": "YOUR_API_KEY",
        "openai_base_url": "https://YOUR_MIMO_API_BASE_URL/v1"
      }
    }
  }
}
```

#### 矛盾消解配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_llm_calls` | 1 | 最大LLM调用轮数（每轮含num_votes次并行） |
| `num_votes` | 1 | 每轮并行投票次数（多数票决定结果） |
| `max_tokens` | 5000 | LLM最大输出token数 |
| `auto_archive_threshold` | 0.8 | 置信度≥此值自动归档旧记忆 |

#### Embedder 配置

```json
{
  "mem0": {
    "embedder": {
      "provider": "openai",
      "config": {
        "model": "BAAI/bge-m3",
        "api_key": "YOUR_API_KEY",
        "openai_base_url": "https://api.siliconflow.cn/v1",
        "embedding_dims": 1024
      }
    }
  }
}
```

#### Rerank 配置

```json
{
  "rerank": {
    "provider": "siliconflow",
    "config": {
      "model": "BAAI/bge-reranker-v2-m3",
      "api_key": "YOUR_API_KEY",
      "openai_base_url": "https://api.siliconflow.cn/v1"
    }
  }
}
```

#### Neo4j 配置

```json
{
  "neo4j": {
    "enabled": true,
    "uri": "bolt://neo4j:7687",
    "username": "neo4j",
    "password": "YOUR_NEO4J_PASSWORD"
  }
}
```

#### 打分权重配置

```json
{
  "scoring": {
    "weights": {
      "vector": 0.35,
      "bm25": 0.25,
      "time": 0.15,
      "reliability": 0.15,
      "salience": 0.10
    },
    "rerank_weight": 0.4,
    "salience_weight": 0.15
  }
}
```

## API 端点

### 核心

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/add` | 写入记忆（含注入防御+去重+矛盾消解） |
| POST | `/search` | 搜索记忆（向量+BM25+Neo4j联想+salience boost+rerank） |
| POST | `/delete` | 删除记忆（软删除） |
| POST | `/update` | 更新记忆（双端同步） |

### 监控

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（mem0+neo4j状态） |
| GET | `/stats` | 数据统计（向量数+图谱节点数） |
| GET | `/degradation` | 降级状态追踪 |

### 维护

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/expire` | 手动触发过期清理 |
| POST | `/consolidate` | 记忆整合（碎片合并） |
| POST | `/evolve` | 自进化（LLM质量分析） |
| POST | `/reflect` | 系统反思 |

### 核心记忆

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/core-memory/add` | 标记为核心记忆 |
| POST | `/core-memory/remove` | 移除核心标记 |
| GET | `/core-memory/list` | 列出核心记忆 |

### 版本追踪

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/versions/{memory_id}` | 查询记忆版本历史 |
| GET | `/versions/stats` | 版本统计 |
| POST | `/versions/{memory_id}/rollback` | 回滚到指定版本 |

### 热知识归档

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/archive/candidates` | 查询归档候选 |
| POST | `/archive/run` | 手动触发归档 |
| GET | `/archive/status` | 归档线程状态 |

### 图谱可视化

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/graph/export` | 导出知识图谱（节点+边） |

## Hermes 插件部署

### 安装

```bash
# 复制插件到 Hermes profile
cp -r plugin/ ~/.hermes/profiles/your-profile/plugins/mem0x/

# 复制配置
cp plugin/mem0x.json.example ~/.hermes/profiles/your-profile/mem0x.json
# 编辑 mem0x.json 设置 service_url
```

### 配置 (mem0x.json)

```json
{
  "service_url": "http://127.0.0.1:28768",
  "user_id": "your-user-id",
  "agent_id": "hermes",
  "timeout": {
    "add": 300,
    "update": 300,
    "delete": 300,
    "search": 300
  }
}
```

### 超时配置说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `timeout.add` | 300 | 写入记忆超时（秒），含事实提取+矛盾消解 |
| `timeout.update` | 300 | 更新记忆超时（秒） |
| `timeout.delete` | 300 | 删除记忆超时（秒） |
| `timeout.search` | 300 | 搜索记忆超时（秒） |

### 启用

在 `config.yaml` 中设置：

```yaml
memory:
  memory_enabled: true
  provider: mem0x
```

### 插件功能

| 功能 | 说明 |
|------|------|
| `prefetch()` | 对话前预取记忆，注入 system prompt（含 Neo4j 图谱联想） |
| `sync_turn()` | 对话后异步写入记忆（含 sender metadata 溯源） |
| `handle_tool_call()` | 工具调用时的 add/search/update/delete |

## 数据存储

```
~/.mem0x/
├── config-compose.json     # Docker 配置
├── docker-compose.mem0x.yml
└── data/
    ├── fastembed/           # BM25 模型缓存
    │   └── bm25/            # Qdrant/bm25 模型文件
    ├── conflict.db          # 矛盾消解记录
    ├── core_memory.db       # 核心记忆元数据
    ├── reflect.db           # 反思日志
    ├── salience.db          # 热度/显著性追踪
    ├── version_history.db   # 版本历史
    └── consolidation.db     # 碎片合并历史
```

外部存储：
- **Qdrant**：向量索引（Docker 端口 6333，宿主机 26333）
- **Neo4j**：实体关系图谱（bolt 7687 / HTTP 7474，宿主机 26787 / 27474）

## 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| API框架 | FastAPI + Uvicorn | HTTP 服务 |
| 向量存储 | Qdrant | 向量索引 + BM25 融合搜索 |
| 知识图谱 | Neo4j 5.x | 实体关系存储 + 2跳联想 |
| LLM | Xiaomi mimo-v2.5-pro | 事实提取 + 矛盾消解 + 记忆整合 |
| Embedding | SiliconFlow BAAI/bge-m3 | 向量化（1024维） |
| Reranker | SiliconFlow BAAI/bge-reranker-v2-m3 | 搜索结果重排序 |
| BM25 | fastembed Qdrant/bm25 | 关键词搜索 |
| NLP | spaCy en_core_web_sm | 实体提取 + 词形还原 |

## 镜像备份

```bash
# 导出镜像
sudo docker save mem0xapi:0.1.15 | gzip > mem0xapi-0.1.15.tar.gz

# 恢复镜像
gunzip -c mem0xapi-0.1.15.tar.gz | sudo docker load
```

## 许可证

MIT License
