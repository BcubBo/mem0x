# mem0x

基于 [mem0](https://github.com/mem0ai/mem0) 的记忆增强服务，为 AI Agent 提供持久化记忆能力。

本项目的设计思路和架构灵感来源于 [aiduMEI](https://github.com/monkey2jack/aiduMEI)，感谢其在 AI 记忆系统领域的探索和实践。

## 架构

```
Hermes Agent (插件层) → mem0x API (HTTP) → Qdrant (向量存储) + Neo4j (知识图谱)
                                         ↓
                                    Redis (速率限制 + 游标持久化)
                                         ↓
                                    SQLite (FTS5全文索引 + salience + version_tracker)
```

### 核心组件

| 组件 | 说明 |
|------|------|
| `mem0x_server.py` | FastAPI 主服务，暴露 `/api` `/evolve` `/consolidate` 端点 |
| `security/pipeline.py` | 安全写入链路：注入防御 → PII脱敏 → 去重 → 矛盾消解 → 语义判重 |
| `security/conflict_resolver.py` | 矛盾消解：规则驱动 + LLM 并行投票 |
| `security/compensation.py` | 补偿队列：写入失败自动重试，SQLite 持久化 |
| `security/circuit_breaker.py` | 断路器：Qdrant/Neo4j/LLM 故障隔离 |
| `wrapper/fetch_all.py` | 分页获取 + 多用户发现（绕过 mem0 get_all 限制） |
| `wrapper/index_sync.py` | 跨存储同步：Qdrant → FTS5/salience/version_tracker |
| `wrapper/fsrs_bridge.py` | FSRS-6 质量评估：标准间隔重复算法 |
| `wrapper/consolidation.py` | 碎片合并：无 LLM 算法（pick_best/keyword_merge） |
| `wrapper/evolve_mem.py` | 自进化：质量分析 + 低质清理 |
| `wrapper/auto_expire.py` | 过期清理：lane TTL + expires 标记 |

## 版本历史

### v0.1.27 (2026-08-26)
- **IndexSync 跨存储同步**：删除/合并记忆后自动同步 FTS5/salience
- **consolidation 无 LLM 算法**：cosine≥0.95 选最佳、0.88-0.95 关键词拼接
- **Redis 游标持久化**：consolidation 候选池游标存 Redis，重启不丢失
- **多用户分批处理**：按 user_id 循环，覆盖所有用户
- **分页获取**：iter_batches 生成器模式，不一次性加载全量
- **配置化**：阈值/间隔/候选数全部从 config-compose.json 读取
- **P0 修复**：compensation 精确清理、断路器 HALF_OPEN 回退
- **P1 修复**：facet API 字段名、auto_expire 共享锁、局部变量遮蔽
- **FSRS 兼容**：旧记忆用 age-based 基线分数，不再误清理
- **pytest 测试套件**：6 项基础测试全部通过
- consolidation/evolve 异步修复：`memory.add()`/`memory.delete()` 加 await

### v0.1.17.3 (2026-08-24)
- 全量 async 迁移：`Memory` → `AsyncMemory`
- API Key 认证 + Redis 速率限制
- 默认用户环境变量化（`MEM0X_DEFAULT_USER`）
- Token 增强：审计日志 + 一次性 + api_key 绑定 + 撤销

### v0.1.16 (2026-08-23)
- 矛盾消解优化：LLM 并行投票（可配置 `num_votes`）
- 矛盾消解配置化：`conflict.llm.config`

## 部署

### Docker Compose

```bash
cd ~/.mem0x
sudo docker compose -f docker-compose.mem0x.yml up -d
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MEM0X_CONFIG` | 配置文件路径 | `/app/config.json` |
| `MEM0X_DEFAULT_USER` | 默认用户 ID | `default` |
| `MEM0_TELEMETRY` | 禁用遥测 | `False` |
| `FASTEMBED_CACHE_PATH` | Embedding 缓存路径 | `/tmp/fastembed_cache` |

### 配置文件

`~/.mem0x/config-compose.json`:

```json
{
  "mem0": {
    "llm": {
      "provider": "openai",
      "config": {
        "model": "mimo-v2.5-pro",
        "max_tokens": 5000
      }
    },
    "vector_store": {
      "provider": "qdrant",
      "config": {
        "url": "http://qdrant:6333",
        "collection_name": "mem0"
      }
    }
  },
  "conflict": {
    "llm": {
      "config": {
        "num_votes": 2,
        "max_llm_calls": 1
      }
    }
  }
}
```

## API

### POST /add

写入记忆。

```json
{
  "messages": "用户说：端口是28767",
  "user_id": "bo",
  "agent_id": "hermes",
  "infer": false
}
```

### POST /search

搜索记忆。

```json
{
  "query": "端口配置",
  "user_id": "bo",
  "agent_id": "hermes",
  "limit": 10,
  "rerank": true
}
```

### POST /update

更新记忆。

```json
{
  "memory_id": "xxx",
  "content": "更新后的内容"
}
```

### POST /delete

软删除记忆（需 confirm_token 确认硬删除）。

```json
{
  "memory_id": "xxx"
}
```

## 安全

- **注入防御**：L1 正则匹配 + L2 Unicode 归一化
- **PII 脱敏**：身份证、手机、邮箱、密码
- **API Key 认证**：`X-API-Key` header
- **速率限制**：Redis 令牌桶（add: 30次/分钟）
- **Delete 确认**：两步删除（软删 → confirm_token → 硬删）

## 开发

```bash
# 本地测试
cd /home/ubuntu/workspace/mem0xAPI
python3 -m py_compile mem0x_server.py

# 构建镜像
sudo docker build --no-cache -t mem0xapi:v0.1.25 .

# 部署
cd ~/.mem0x
sudo docker compose -f docker-compose.mem0x.yml down
sudo docker compose -f docker-compose.mem0x.yml up -d
```

## License

MIT
