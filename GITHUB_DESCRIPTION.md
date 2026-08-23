# mem0x

自托管 AI 记忆增强服务，基于 [mem0ai](https://github.com/mem0ai/mem0) 构建。

为 AI Agent 提供持久化记忆能力：向量搜索 + 知识图谱 + 矛盾消解 + 安全防护。

## 一句话

**让 AI 记住你说过的每一句话，并在需要时准确回忆。**

## 核心能力

- 🔍 **智能搜索** — 向量+BM25+Neo4j图谱联想，多维度精准召回
- ⚔️ **矛盾消解** — 新旧记忆冲突时自动判断，可配置LLM并行投票
- 🛡️ **安全防护** — 注入防御、PII脱敏、Jaccard去重
- 📊 **6维打分** — 向量相似度+时间衰减+热度+置信度
- 🔄 **自动维护** — 过期清理、记忆整合、版本追踪

## 快速开始

```bash
# 克隆
git clone https://github.com/BcubBo/mem0x.git
cd mem0x

# 配置
cp config-compose.json.example ~/.mem0x/config-compose.json
# 编辑 ~/.mem0x/config-compose.json 填入 API key

# 启动
docker build -t mem0xapi:0.1.16 .
cd ~/.mem0x && docker compose -f docker-compose.mem0x.yml up -d

# 验证
curl http://localhost:28768/health
```

## 架构

```
Agent ──▶ mem0x API ──▶ Qdrant (向量) + Neo4j (图谱)
                 │
                 ▼
           mimo-v2.5-pro (LLM)
```

## 写入链路

```
注入防御 → PII脱敏 → 搜索候选 → 矛盾消解(LLM) → Jaccard去重 → 事实提取(LLM) → 写入
```

## 配置示例

```json
{
  "conflict": {
    "llm": {
      "config": {
        "model": "mimo-v2.5-pro",
        "max_tokens": 5000,
        "max_llm_calls": 1,
        "num_votes": 1
      }
    }
  }
}
```

## 文档

📖 [完整文档](https://github.com/BcubBo/mem0x/blob/main/README.md)

## 许可证

MIT License
