# mem0x-mcp

mem0x MCP Server — standalone memory service for coding agents via [Model Context Protocol](https://modelcontextprotocol.io).

Connects to a mem0x API instance over HTTP. No dependency on mem0x source code.

## Features

- **Standalone**: only needs a reachable mem0x API URL
- **Standard MCP**: JSON-RPC 2.0 over stdio, compatible with Claude Code, MiMo Code, etc.
- **6 tools**: search / add / update / delete / graph / stats
- **Resilient**: auto-retry with exponential backoff, 30s timeout per call
- **Configurable agent ID**: each agent identifies itself for memory attribution

## Install

```bash
# Recommended: pipx (isolated environment)
pipx install .

# Or: pip (user install)
pip install --user .

# Or: run directly
python mem0x_mcp_server.py
```

Requires Python >= 3.10.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEM0X_URL` | `http://127.0.0.1:28768` | mem0x API address |
| `MEM0X_API_KEY` | (empty) | API key (required if auth is enabled) |
| `MEM0X_AGENT_ID` | `mimocode` | Agent identifier for memory attribution |

## Claude Code Configuration

Add to `.mcp.json` in your project root (or `~/.claude/claude_desktop_config.json` for Claude Desktop):

```json
{
  "mcpServers": {
    "mem0x": {
      "command": "python3",
      "args": ["/path/to/mem0x_mcp_server.py"],
      "env": {
        "MEM0X_URL": "http://127.0.0.1:28768",
        "MEM0X_API_KEY": "your-api-key",
        "MEM0X_AGENT_ID": "claude-code"
      }
    }
  }
}
```

If installed via `pip`/`pipx`, you can use the CLI directly:

```json
{
  "mcpServers": {
    "mem0x": {
      "command": "mem0x-mcp",
      "env": {
        "MEM0X_URL": "http://127.0.0.1:28768",
        "MEM0X_API_KEY": "your-api-key"
      }
    }
  }
}
```

## MiMo Code Configuration

Add to `~/.config/mimocode/mimocode.jsonc`:

```json
{
  "mcp": {
    "mem0x": {
      "type": "local",
      "command": ["python3", "/path/to/mem0x_mcp_server.py"],
      "environment": {
        "MEM0X_URL": "http://127.0.0.1:28768",
        "MEM0X_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `search_memory` | Semantic search + Neo4j graph recall |
| `add_memory` | Write memory (auto: injection defense, PII redaction, dedup, conflict resolution) |
| `update_memory` | Update existing memory |
| `delete_memory` | Soft-delete (preserved for recovery) |
| `get_graph` | Export knowledge graph (entities + relationships) |
| `get_stats` | Storage statistics (Qdrant points, Neo4j nodes) |

## Architecture

```
Claude Code ─MCP stdio─┐
                       ├──▶ mem0x-mcp ─HTTP─▶ mem0x API ◀──▶ Qdrant + Neo4j
MiMo Code ──MCP stdio─┘         ↑
                           Hermes Agent ◀─HTTP─▶ mem0x API
```

多个 agent 通过 mem0x 共享同一份记忆数据。每个 agent 通过 `MEM0X_AGENT_ID` 标识自己。

## Troubleshooting

**Server won't start / "command not found" after pip install**
- Ensure Python >= 3.10: `python3 --version`
- If using `pip install --user`, check `~/.local/bin` is in `$PATH`
- Try running directly: `python3 /path/to/mem0x_mcp_server.py`

**Search returns empty results**
- Check `MEM0X_AGENT_ID`: memories are attributed to an agent. Search with the same agent_id that wrote them.
- Check `MEM0X_URL`: verify mem0x API is running: `curl http://127.0.0.1:28768/health`

**Tool calls timeout**
- Default timeout is 30s per call with 3 retries
- Check mem0x API health and Qdrant/Neo4j connectivity
- Increase `MEM0X_URL` accessibility (firewall, Docker network)

**"Unknown tool" errors**
- Ensure MCP SDK >= 1.0: `pip show mcp`
- Restart the MCP client after config changes

## Development

```bash
cd mem0x-mcp
pip install -e .
MEM0X_URL=http://127.0.0.1:28768 MEM0X_API_KEY=your-key python mem0x_mcp_server.py
```

## License

MIT
