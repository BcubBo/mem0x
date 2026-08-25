# mem0x 写入可靠性架构方案

> 日期：2026-08-25
> 目标：为核心记忆通路（Qdrant + Neo4j）建立可靠的写入保障机制
> 状态：方案设计阶段，待审计

---

## 一、问题背景

### 当前写入路径

```
/add /update /delete 请求
    │
    ├── ① Qdrant（向量+payload）← 核心，搜索依赖
    │     失败 → 500，用户知道 ← 无重试
    │
    ├── ② salience.db（SQLite）← 辅助，重要度衰减
    │     失败 → 静默降级
    │
    ├── ③ version_tracker（SQLite）← 辅助，版本回滚
    │     失败 → 静默降级
    │
    └── ④ Neo4j（实体+关系）← 辅助，图谱联想
          失败 → 静默降级 ← 无重试
```

### 各组件失败影响

| 组件 | 临时故障 | 永久故障 | 影响 |
|------|---------|---------|------|
| Qdrant | 网络抖动、重启中、连接池耗尽 | 磁盘满、数据损坏 | 搜索不可用 |
| Neo4j | 网络抖动、重启中 | 实体提取异常、磁盘满 | 图谱联想缺失 |
| SQLite | 锁冲突（WAL已修） | 文件损坏 | salience/版本缺失 |

### 并发模型

```
FastAPI 请求线程池（默认40线程）
  ├─ /add → 同时写 Qdrant + Neo4j
  ├─ /search → 同时读 Qdrant + Neo4j
  ├─ /update → 同时写 Qdrant + Neo4j
  └─ /delete → 同时写 Qdrant + Neo4j

后台补偿线程（1个）
  └─ 定时扫描补偿队列，重试失败写入

后台业务线程（4个）
  ├─ consolidation（合并去重）
  ├─ evolve（自进化）
  ├─ reflect（反思）
  └─ auto_expire（过期清理）
```

---

## 二、设计原则

1. **核心通路（Qdrant）必须可靠** — 失败要重试，重试失败要告警，不能静默吞掉
2. **辅助通路（Neo4j）允许降级** — 但降级要有补偿机制
3. **幂等性** — 重试不能产生重复数据
4. **线程安全** — 多线程并发访问共享资源必须加锁
5. **可观测** — 每个组件的成功/失败/重试次数都要可查

---

## 三、分层架构

### 第一层：传输层重试

通用重试装饰器，指数退避，每次调用独立状态（线程安全）。

```python
@retry(max_attempts=3, backoff=[1, 2, 4], 
       retry_on=(ConnectionError, TimeoutError))
def write_to_qdrant(...): ...

@retry(max_attempts=3, backoff=[1, 2, 4],
       retry_on=(ConnectionError, TimeoutError))  
def write_to_neo4j(...): ...
```

### 第二层：应用层降级决策

```
Qdrant 写入
  ├─ 重试成功 → 继续
  ├─ 重试失败（临时） → 写入补偿队列，返回 202 Accepted
  └─ 重试失败（永久） → 返回 500，用户知道

Neo4j 写入
  ├─ 重试成功 → 继续
  └─ 重试失败 → 写入补偿队列，静默降级
```

### 第三层：补偿队列

内存队列 + 后台线程定时重试。

- 入队：写入失败时
- 出队：补偿线程每60秒扫描，重试成功则移除
- 超时：超过1小时未补偿，移到死信队列（人工处理）
- 上限：队列最大1000条，满时丢弃最旧

### 第四层：监控暴露

`/health` 端点返回写入健康状态。

```json
{
  "status": "ok",
  "write_health": {
    "qdrant": {"status": "ok", "failures_1h": 0, "retries_1h": 0},
    "neo4j": {"status": "degraded", "failures_1h": 2, "retries_1h": 5, "pending_compensation": 3},
    "compensation_queue_size": 3
  }
}
```

---

## 四、线程安全设计

### 共享资源保护

| 资源 | 并发访问者 | 保护方式 |
|------|-----------|---------|
| 补偿队列 | 所有请求线程 + 补偿线程 | `threading.Lock` + `collections.deque` |
| 健康计数器 | 所有请求线程 + 健康检查 | `threading.Lock` |
| Qdrant 客户端 | 所有请求线程 | Qdrant 内置连接池，配 `max_pool_size` |
| Neo4j 驱动 | 所有请求线程 + 后台线程 | Neo4j driver 内置线程安全，session-per-thread |
| SQLite 连接 | 所有线程 | `threading.local()` 每线程独立连接 |
| Redis 连接 | 所有请求线程 | Redis 内置连接池，配 `max_connections` |

### 重试装饰器（线程安全关键）

装饰器本身无状态，每次调用是独立的 retry 循环，不存在共享状态问题。

---

## 五、改动清单

| # | 改动 | 文件 | 复杂度 | 说明 |
|---|------|------|--------|------|
| 1 | 通用重试装饰器 | 新建 `security/retry.py` | 中 | 指数退避+异常分类+线程安全 |
| 2 | 补偿队列 | 新建 `wrapper/compensation.py` | 中 | 线程安全队列+后台补偿线程 |
| 3 | 健康指标 | 新建 `wrapper/write_health.py` | 低 | 线程安全计数器+滑动窗口 |
| 4 | Qdrant 写入加重试 | `mem0x_server.py` | 低 | 包装 memory.add/update |
| 5 | Neo4j 写入加重试 | `mem0x_server.py` | 低 | 包装 hook.write |
| 6 | /health 暴露写入指标 | `mem0x_server.py` | 低 | 调用 WriteHealth.snapshot() |
| 7 | Qdrant 连接池配置 | `wrapper/mem0_runtime.py` | 低 | max_pool_size=20 |
| 8 | Neo4j session 管理 | `wrapper/neo4j_hook.py` | 中 | 确保 session-per-thread |

---

## 六、风险评估

| 风险 | 缓解 |
|------|------|
| 补偿队列内存溢出 | max_size=1000，满时丢弃最旧 |
| 补偿线程死循环 | 每轮最多处理10项，sleep 60秒 |
| 重试风暴（大量请求同时失败） | 指数退避 + 队列上限 |
| 线程锁死锁 | 单锁设计，无嵌套锁 |
| Qdrant 连接池耗尽 | max_pool_size=20 + 超时配置 |

---

## 七、测试计划

1. **单元测试**：重试装饰器、补偿队列、健康指标的线程安全
2. **集成测试**：模拟 Qdrant/Neo4j 临时不可用，验证重试+补偿
3. **压力测试**：40并发写入，验证锁竞争和连接池
4. **故障注入**：停止 Neo4j 容器，验证降级+补偿恢复

---

## 八、待确认

1. Qdrant 写入失败时返回 202 还是 500？（用户感知 vs 可靠性权衡）
2. 补偿队列是否需要持久化到 Redis？（重启丢失 vs 简单性）
3. Neo4j 补偿的最大重试次数？（5次 vs 无限）
4. /health 的写入指标是否需要认证？（信息泄露风险）
