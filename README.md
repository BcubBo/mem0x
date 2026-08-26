# mem0x

基于 [mem0](https://github.com/mem0ai/mem0) 的记忆增强服务，为 AI Agent 提供持久化记忆能力。

本项目的设计思路和架构灵感来源于 [aiduMEI](https://github.com/monkey2jack/aiduMEI)，感谢其在 AI 记忆系统领域的探索和实践。

## 架构

```
Hermes Agent (插件层) → mem0x API (HTTP) → Qdrant (向量存储) + Neo4j (知识图谱)
                                         ↓
                                    Redis (速率限制)
```

### 核心组件

| 组件 | 说明 |
|------|------|
| `mem0x_server.py` | FastAPI 主服务，暴露 `/add` `/search` `/update` `/delete` 端点 |
| `security/pipeline.py` | 安全写入链路：注入防御 → PII脱敏 → 去重 → 矛盾消解 → 语义判重 |
| `security/conflict_resolver.py` | 矛盾消解：规则驱动 + LLM 并行投票 |
| `security/dedup.py` | Jaccard 去重 + 语义判重 |
| `security/injection_guard.py` | 注入防御（L1正则 + L2归一化） |
| `wrapper/` | mem0 异步封装、consolidation、evolve 等扩展 |

## 版本历史

### v0.1.18.1 (2026-08-25)
- 修复 `/update` 端点缺少 `request: Request` 参数导致的 NameError
- 搜索端点日志增强：打印解析后的 user_id 和请求体
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
