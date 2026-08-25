#!/bin/bash
# Phase 2 统一 API 端到端测试
set -e
BASE="http://127.0.0.1:28767"
API_KEY="e2e01e109edc79a3932f1583ec884122564b203f1dd5584bd15ae6d5052e2c21"
PASS=0
FAIL=0

test_api() {
    local name="$1"
    local expected_status="$2"
    local method="$3"
    local path="$4"
    local body="$5"
    shift 5
    local extra_headers=("$@")

    local args=(-s -w "\n%{http_code}" -H "Content-Type: application/json" -H "X-API-Key: $API_KEY")
    if [ ${#extra_headers[@]} -gt 0 ]; then
        args+=("${extra_headers[@]}")
    fi

    local response
    if [ "$method" = "GET" ]; then
        response=$(curl "${args[@]}" "$BASE$path" 2>/dev/null)
    else
        response=$(curl "${args[@]}" -X POST -d "$body" "$BASE$path" 2>/dev/null)
    fi

    local http_status=$(echo "$response" | tail -1)
    local resp_body=$(echo "$response" | sed '$d')

    if [ "$http_status" = "$expected_status" ]; then
        echo "✅ $name (HTTP $http_status)"
        PASS=$((PASS + 1))
    else
        echo "❌ $name — expected HTTP $expected_status, got $http_status"
        echo "   Response: $resp_body" | head -3
        FAIL=$((FAIL + 1))
    fi
}

echo "═══════════════════════════════════════"
echo "Phase 2 统一 API 测试"
echo "═══════════════════════════════════════"
echo ""

# ── 1. 健康检查 ──
echo "── 1. 健康检查 ──"
test_api "GET /health" "200" "GET" "/health" ""

# ── 2. 旧端点兼容 ──
echo ""
echo "── 2. 旧端点兼容 ──"
test_api "POST /add (旧)" "200" "POST" "/add" \
    '{"messages":"test-old-endpoint","user_id":"test","agent_id":"hermes","infer":false}'
test_api "POST /search (旧)" "200" "POST" "/search" \
    '{"query":"test","user_id":"test","agent_id":"hermes","limit":1}'

# ── 3. 新统一端点 /api ──
echo ""
echo "── 3. 统一端点 /api ──"
test_api "POST /api action=add" "200" "POST" "/api" \
    '{"action":"add","params":{"messages":"test-unified-api-add","infer":false}}'
test_api "POST /api action=search" "200" "POST" "/api" \
    '{"action":"search","params":{"query":"test","limit":1}}'
test_api "POST /api action=delete (invalid uuid)" "400" "POST" "/api" \
    '{"action":"delete","params":{"memory_id":"invalid"}}'
test_api "POST /api action=unknown" "400" "POST" "/api" \
    '{"action":"unknown_action","params":{}}'

# ── 4. Header 身份注入 ──
echo ""
echo "── 4. Header 身份注入 ──"
test_api "POST /api with headers" "200" "POST" "/api" \
    '{"action":"add","params":{"messages":"test-header-identity","infer":false}}' \
    -H "X-User-ID: header-test-user" \
    -H "X-Agent-ID: test-agent" \
    -H "X-Platform: feishu" \
    -H "X-Chat-ID: oc_test123" \
    -H "X-Chat-Type: dm" \
    -H "X-Source: plugin" \
    -H "X-Request-ID: req-test-001"

test_api "POST /api search with headers" "200" "POST" "/api" \
    '{"action":"search","params":{"query":"test","limit":1}}' \
    -H "X-User-ID: header-test-user" \
    -H "X-Platform: feishu"

# ── 5. 认证测试 ──
echo ""
echo "── 5. 认证测试 ──"
test_api "POST /api without API key" "401" "POST" "/api" \
    '{"action":"search","params":{"query":"test"}}'

# ── 6. /expire ──
echo ""
echo "── 6. /expire 端点 ──"
test_api "POST /expire" "200" "POST" "/expire" ""

# ── 汇总 ──
echo ""
echo "═══════════════════════════════════════"
echo "结果: ✅ $PASS 通过  ❌ $FAIL 失败"
echo "═══════════════════════════════════════"

exit $FAIL
