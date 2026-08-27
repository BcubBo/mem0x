#!/usr/bin/env bash
# ============================================================
# mem0x 一键部署脚本 —— 修复仓库原有 bug，真正"一条命令部署"
# 原 bug:
#   1. README 命令指向 docker-compose.mem0x.yml(不存在)，实际叫 .example
#   2. Dockerfile COPY 的两个 spaCy 模型 .tar.gz 文件仓库里不存在 → build 必挂
#   3. compose 里 image 是本地镜像 mem0xapi:v0.1.28，需本地 build
#   4. 前置依赖多(network/mkdir/先起 qdrant/redis/cp 配置填 key)
#
# 用法:
#   ./setup.sh                          # 交互式最简部署(留占位符，需手动填 key)
#   MEM0_LLM_API_KEY=sk-xxx ./setup.sh  # 用环境变量注入 key，一键到底
#
# 支持的环境变量(可选，对应 config-compose.json 里的占位符):
#   MEM0_LLM_MODEL / MEM0_LLM_API_KEY / MEM0_LLM_BASE_URL
#   MEM0_EMBEDDER_API_KEY / MEM0_EMBEDDER_BASE_URL
#   MEM0_QDRANT_API_KEY
#   MEM0_RERANK_API_KEY / MEM0_RERANK_BASE_URL
#   MEM0_SERVER_API_KEY
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

on_error() {
  printf '\033[1;31m[setup]\033[0m 部署失败，请检查上方日志（错误发生在第 %s 行附近）。\n' "${BASH_LINENO[0]}" >&2
}
trap on_error ERR

# ── 0. 前置检查 ─────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || fail "未找到 docker，请先安装 Docker。"
docker info >/dev/null 2>&1 || fail "Docker daemon 未运行，请先启动 Docker。"

COMPOSE=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    fail "未找到 docker compose / docker-compose。"
  fi
fi

# ── 1. 校验仓库根目录 ───────────────────────────────────────
[[ -f Dockerfile ]] || fail "当前目录不是 mem0x 仓库根目录（缺少 Dockerfile）。"
[[ -f docker-compose.mem0x.yml.example ]] || fail "缺少 docker-compose.mem0x.yml.example。"
[[ -f docker-compose.mem0x-qdrant.yml.example ]] || fail "缺少 docker-compose.mem0x-qdrant.yml.example。"
[[ -f config-compose.json.example ]] || fail "缺少 config-compose.json.example。"

# ── 2. 复制 .example 配置(幂等，已存在则跳过) ────────────────
log "复制 .example 配置文件（已存在则跳过）"
for pair in \
  "docker-compose.mem0x.yml.example:docker-compose.mem0x.yml" \
  "docker-compose.mem0x-qdrant.yml.example:docker-compose.mem0x-qdrant.yml" \
  "config-compose.json.example:config-compose.json"; do
  src="${pair%%:*}"; target="${pair##*:}"
  if [[ -e "$target" ]]; then
    log "  跳过 $target（已存在）"
  else
    cp "$src" "$target"
    log "  已生成 $target"
  fi
done

# ── 3. 用环境变量替换 config-compose.json 里的占位符 ────────
log "注入环境变量到 config-compose.json（有则替换，无则留占位符）"
MEM0_LLM_MODEL="${MEM0_LLM_MODEL:-}" \
MEM0_LLM_API_KEY="${MEM0_LLM_API_KEY:-}" \
MEM0_LLM_BASE_URL="${MEM0_LLM_BASE_URL:-}" \
MEM0_EMBEDDER_API_KEY="${MEM0_EMBEDDER_API_KEY:-}" \
MEM0_EMBEDDER_BASE_URL="${MEM0_EMBEDDER_BASE_URL:-}" \
MEM0_QDRANT_API_KEY="${MEM0_QDRANT_API_KEY:-}" \
MEM0_RERANK_API_KEY="${MEM0_RERANK_API_KEY:-}" \
MEM0_RERANK_BASE_URL="${MEM0_RERANK_BASE_URL:-}" \
MEM0_SERVER_API_KEY="${MEM0_SERVER_API_KEY:-}" \
python3 - <<'PY'
import json, os, sys

path = "config-compose.json"
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

# YOUR_XXX 占位符 → MEM0_XXX 环境变量 的映射
mapping = {
    "YOUR_LLM_MODEL":        "MEM0_LLM_MODEL",
    "YOUR_LLM_API_KEY":      "MEM0_LLM_API_KEY",
    "YOUR_LLM_BASE_URL":     "MEM0_LLM_BASE_URL",
    "YOUR_EMBEDDING_API_KEY":"MEM0_EMBEDDER_API_KEY",
    "YOUR_EMBEDDING_BASE_URL":"MEM0_EMBEDDER_BASE_URL",
    "YOUR_QDRANT_API_KEY":   "MEM0_QDRANT_API_KEY",
    "YOUR_RERANK_API_KEY":   "MEM0_RERANK_API_KEY",
    "YOUR_RERANK_BASE_URL":  "MEM0_RERANK_BASE_URL",
    "YOUR_SERVER_API_KEY":   "MEM0_SERVER_API_KEY",
}

def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v in mapping:
                env_val = os.environ.get(mapping[v])
                if env_val:
                    obj[k] = env_val
            else:
                walk(v)
    elif isinstance(obj, list):
        for it in obj:
            walk(it)

walk(cfg)

with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

# 报告仍残留的占位符
unfilled = []
def collect(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str) and v.startswith("YOUR_"):
                unfilled.append(v)
            else:
                collect(v)
    elif isinstance(obj, list):
        for it in obj:
            collect(it)
collect(cfg)
if unfilled:
    print("[setup] 警告：以下配置仍为占位符，需手动填：", file=sys.stderr)
    for u in sorted(set(unfilled)):
        print(f"    - {u}", file=sys.stderr)
else:
    print("[setup] 全部占位符已由环境变量注入。")
PY

# ── 4. 确保 network / 数据目录存在(幂等) ─────────────────────
log "确保 Docker network mem0x-net 存在"
if docker network inspect mem0x-net >/dev/null 2>&1; then
  log "  跳过 mem0x-net（已存在）"
else
  docker network create mem0x-net >/dev/null
  log "  已创建 mem0x-net"
fi

mkdir -p data qdrant-data redis-data
log "已确保数据目录存在：data、qdrant-data、redis-data"

# ── 5. 构建本地镜像 ─────────────────────────────────────────
log "构建本地镜像 mem0xapi:v0.1.28"
docker build -t mem0xapi:v0.1.28 .

# ── 6. 依次启动 qdrant → redis → mem0x ─────────────────────
log "启动 Qdrant（向量库）"
"${COMPOSE[@]}" -f docker-compose.mem0x-qdrant.yml up -d

log "启动 Redis"
"${COMPOSE[@]}" -f docker-compose.mem0x.yml up -d redis

log "启动 mem0x 本体"
"${COMPOSE[@]}" -f docker-compose.mem0x.yml up -d mem0x

echo
log "部署流程完成。"
log "健康检查："
log "  curl -fsS http://127.0.0.1:28768/health"
log "访问地址："
log "  http://127.0.0.1:28768"
log "查看状态："
log "  ${COMPOSE[*]} -f docker-compose.mem0x.yml ps"
