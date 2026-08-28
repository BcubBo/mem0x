"""mem0x MCP Server — standalone service connecting to mem0x API via HTTP.

Provides long-term memory capabilities for coding agents (Claude Code,
MiMo Code, etc.) via the Model Context Protocol (MCP).

Environment variables:
  MEM0X_URL        mem0x API address (default http://127.0.0.1:28768)
  MEM0X_API_KEY    API key (required if mem0x has authentication enabled)
  MEM0X_AGENT_ID   Agent identifier (default "mimocode", used for memory attribution)

Usage:
  # Direct run
  python mem0x_mcp_server.py

  # Claude Code MCP config (claude_desktop_config.json or .mcp.json)
  {
    "mcpServers": {
      "mem0x": {
        "command": "python3",
        "args": ["/path/to/mem0x_mcp_server.py"],
        "env": {
          "MEM0X_URL": "http://127.0.0.1:28768",
          "MEM0X_API_KEY": "your-api-key"
        }
      }
    }
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

# -- Logging (stderr only, never pollutes stdout JSON-RPC channel) --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mem0x-mcp] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("mem0x-mcp")

# -- MCP SDK --
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# -- mem0x HTTP client --
from mem0x_client import Mem0xClient

# Agent ID: configurable via env, so Claude Code / MiMo Code / others
# can each identify themselves properly in shared memory.
DEFAULT_AGENT_ID = os.environ.get("MEM0X_AGENT_ID", "mimocode")


@asynccontextmanager
async def lifespan(server: Server):
    """Manage Mem0xClient lifecycle: create on startup, close on shutdown."""
    client = Mem0xClient()
    logger.info("mem0x client initialized (url=%s, agent_id=%s)",
                client.base_url, DEFAULT_AGENT_ID)
    try:
        yield {"client": client}
    finally:
        await client.close()
        logger.info("mem0x client closed")


# ================================================================
# Tool definitions
# ================================================================

TOOLS: list[types.Tool] = [
    types.Tool(
        name="search_memory",
        description=(
            "Search long-term memory with semantic search + Neo4j graph recall. "
            "Use this first to understand project context, historical decisions, "
            "or previous issues before writing code."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (natural language)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 100)",
                    "default": 10,
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID (default 'bo', shared with Hermes Agent)",
                    "default": "bo",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for attribution (default from MEM0X_AGENT_ID env)",
                    "default": DEFAULT_AGENT_ID,
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived memories (default false)",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="add_memory",
        description=(
            "Write a new memory. Automatically applies injection defense, "
            "PII redaction, deduplication, and conflict resolution. "
            "Use for recording code changes, test results, design decisions, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content (factual description)",
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID (default 'bo')",
                    "default": "bo",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for attribution (default from MEM0X_AGENT_ID env)",
                    "default": DEFAULT_AGENT_ID,
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata (e.g. source, tags)",
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="update_memory",
        description="Update an existing memory's content.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Memory ID (UUID format)",
                },
                "content": {
                    "type": "string",
                    "description": "New memory content",
                },
            },
            "required": ["memory_id", "content"],
        },
    ),
    types.Tool(
        name="delete_memory",
        description=(
            "Soft-delete a memory. Deleted memories are filtered from search "
            "results but data is preserved for recovery."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Memory ID (UUID format)",
                },
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="get_graph",
        description=(
            "Export the knowledge graph. Returns entities and relationships "
            "for understanding project structure and entity associations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entities (default 50)",
                    "default": 50,
                },
                "entity_type": {
                    "type": "string",
                    "description": "Filter by entity type (e.g. Person, Concept, Module)",
                },
            },
        },
    ),
    types.Tool(
        name="get_stats",
        description="Get mem0x storage statistics (Qdrant points, Neo4j node counts, etc.).",
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ================================================================
# Tool handlers (all via HTTP, with timeout + retry)
# ================================================================

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5  # seconds
REQUEST_TIMEOUT = 30.0  # seconds per HTTP call
RETRYABLE_HTTP_CODES = {429, 502, 503, 504}


async def _retry(coro_factory, attempts=RETRY_ATTEMPTS):
    """Retry a coroutine with exponential backoff on transient errors.

    Retries on: connection errors, timeouts, transient HTTP (429/502/503/504).
    Does NOT retry on: 4xx client errors, 501/505, auth failures.
    """
    import httpx
    last_err = None
    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(coro_factory(), timeout=REQUEST_TIMEOUT)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in RETRYABLE_HTTP_CODES:
                last_err = e
                if attempt < attempts - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("Retry %d/%d after %.1fs (HTTP %d): %s",
                                   attempt + 1, attempts, delay,
                                   e.response.status_code, e)
                    await asyncio.sleep(delay)
                    continue
            raise  # non-retryable HTTP error
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.WriteTimeout, httpx.PoolTimeout,
                ConnectionError, OSError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt < attempts - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("Retry %d/%d after %.1fs: %s",
                               attempt + 1, attempts, delay, e)
                await asyncio.sleep(delay)
    raise last_err  # type: ignore[misc]


async def _handle_search(
    client: Mem0xClient,
    query: str,
    limit: int = 10,
    user_id: str = "bo",
    agent_id: str | None = None,
    include_archived: bool = False,
) -> str:
    agent_id = agent_id or DEFAULT_AGENT_ID
    start = time.time()
    try:
        raw = await _retry(lambda: client.search(
            query=query, user_id=user_id, agent_id=agent_id,
            limit=limit, include_archived=include_archived,
        ))
        results = raw.get("results", [])
        # Filter soft-deleted (server-side filtering TODO: ask mem0x API)
        results = [r for r in results
                   if not (isinstance(r.get("metadata"), dict)
                           and r["metadata"].get("deleted_at"))]
        elapsed_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "results": [
                {"id": r.get("id", ""), "memory": r.get("memory", ""),
                 "score": round(r.get("score", 0), 3)}
                for r in results
            ],
            "count": len(results),
            "elapsed_ms": elapsed_ms,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("search failed: %s", e, exc_info=True)
        return json.dumps({"error": "Search failed", "detail": str(e)[:200]}, ensure_ascii=False)


async def _handle_add(
    client: Mem0xClient,
    content: str,
    user_id: str = "bo",
    agent_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    agent_id = agent_id or DEFAULT_AGENT_ID
    try:
        result = await _retry(lambda: client.add(
            content=content, user_id=user_id,
            agent_id=agent_id, metadata=metadata,
        ))
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("add failed: %s", e, exc_info=True)
        return json.dumps({"error": "Write failed", "detail": str(e)[:200]}, ensure_ascii=False)


async def _handle_update(
    client: Mem0xClient, memory_id: str, content: str,
) -> str:
    try:
        result = await _retry(lambda: client.update(
            memory_id=memory_id, content=content,
        ))
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("update failed: %s", e, exc_info=True)
        return json.dumps({"error": "Update failed", "detail": str(e)[:200]}, ensure_ascii=False)


async def _handle_delete(client: Mem0xClient, memory_id: str) -> str:
    try:
        result = await _retry(lambda: client.delete(memory_id=memory_id))
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("delete failed: %s", e, exc_info=True)
        return json.dumps({"error": "Delete failed", "detail": str(e)[:200]}, ensure_ascii=False)


async def _handle_graph(
    client: Mem0xClient, limit: int = 50, entity_type: str | None = None,
) -> str:
    try:
        result = await _retry(lambda: client.graph_export(
            limit=limit, entity_type=entity_type,
        ))
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("graph export failed: %s", e, exc_info=True)
        return json.dumps({"error": "Graph export failed", "detail": str(e)[:200]}, ensure_ascii=False)


async def _handle_stats(client: Mem0xClient) -> str:
    try:
        result = await _retry(lambda: client.stats())
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("stats failed: %s", e, exc_info=True)
        return json.dumps({"error": "Stats failed", "detail": str(e)[:200]}, ensure_ascii=False)


# Dispatch table: built once on first use, cached in module global.
_dispatch: dict[str, callable] | None = None

def _get_dispatch(client: Mem0xClient) -> dict[str, callable]:
    """Return dispatch table (cached, bound to the given client)."""
    global _dispatch
    if _dispatch is None:
        _dispatch = {
            "search_memory": lambda args: _handle_search(
                client=client,
                query=args["query"],
                limit=args.get("limit", 10),
                user_id=args.get("user_id", "bo"),
                agent_id=args.get("agent_id"),
                include_archived=args.get("include_archived", False),
            ),
            "add_memory": lambda args: _handle_add(
                client=client,
                content=args["content"],
                user_id=args.get("user_id", "bo"),
                agent_id=args.get("agent_id"),
                metadata=args.get("metadata"),
            ),
            "update_memory": lambda args: _handle_update(
                client=client,
                memory_id=args["memory_id"],
                content=args["content"],
            ),
            "delete_memory": lambda args: _handle_delete(
                client=client, memory_id=args["memory_id"],
            ),
            "get_graph": lambda args: _handle_graph(
                client=client,
                limit=args.get("limit", 50),
                entity_type=args.get("entity_type"),
            ),
            "get_stats": lambda args: _handle_stats(client=client),
        }
    return _dispatch


# ================================================================
# MCP Server
# ================================================================

async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    name = params.name
    args = params.arguments or {}
    logger.info("tool call: %s(%s)", name,
                json.dumps(args, ensure_ascii=False)[:200])

    # Get client from lifespan context
    client: Mem0xClient = ctx.lifespan_context["client"]
    dispatch = _get_dispatch(client)

    handler = dispatch.get(name)
    if not handler:
        return types.CallToolResult(
            content=[types.TextContent(type="text",
                                       text=f"Unknown tool: {name}")],
            is_error=True,
        )
    try:
        result = await handler(args)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result)],
            is_error=False,
        )
    except Exception as e:
        logger.error("tool error: %s: %s", name, e, exc_info=True)
        return types.CallToolResult(
            content=[types.TextContent(
                type="text",
                text=json.dumps({"error": "Tool execution error",
                                 "detail": str(e)[:200]},
                                ensure_ascii=False))],
            is_error=True,
        )


# ================================================================
# Entry points
# ================================================================

async def main():
    """Async main: run MCP server over stdio with lifespan-managed client."""
    server = Server(
        name="mem0x",
        version="0.2.1",
        description=(
            "mem0x MCP Server — long-term memory for coding agents "
            "via HTTP connection to mem0x API"
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        lifespan=lifespan,
    )

    logger.info("mem0x MCP Server starting (url=%s, agent_id=%s)",
                os.environ.get("MEM0X_URL", "http://127.0.0.1:28768"),
                DEFAULT_AGENT_ID)
    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


def cli():
    """Sync entry point for `pip install` console_scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
