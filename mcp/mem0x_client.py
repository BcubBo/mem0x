"""mem0x HTTP Client — thin adapter between MCP Server and mem0x API.

All calls go through HTTP to the mem0x API. No direct import of mem0x internals.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("mem0x-mcp.client")

# Default configuration
DEFAULT_BASE_URL = "http://127.0.0.1:28768"
DEFAULT_TIMEOUT = 30.0


class Mem0xClient:
    """HTTP client for mem0x API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (
            base_url or os.environ.get("MEM0X_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("MEM0X_API_KEY", "")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def close(self):
        """Close the HTTP client. Safe to call multiple times."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("HTTP client closed")

    # -- Search --

    async def search(
        self,
        query: str,
        user_id: str = "bo",
        agent_id: str = "mimocode",
        limit: int = 10,
        include_archived: bool = False,
    ) -> dict:
        client = await self._get_client()
        resp = await client.post("/search", json={
            "query": query,
            "user_id": user_id,
            "agent_id": agent_id,
            "limit": limit,
            "rerank": True,
            "include_archived": include_archived,
        })
        resp.raise_for_status()
        return resp.json()

    # -- Write --

    async def add(
        self,
        content: str,
        user_id: str = "bo",
        agent_id: str = "mimocode",
        metadata: dict | None = None,
    ) -> dict:
        client = await self._get_client()
        resp = await client.post("/add", json={
            "messages": content,
            "user_id": user_id,
            "agent_id": agent_id,
            "metadata": metadata or {},
        })
        resp.raise_for_status()
        return resp.json()

    # -- Update --

    async def update(self, memory_id: str, content: str) -> dict:
        client = await self._get_client()
        resp = await client.post("/update", json={
            "memory_id": memory_id,
            "content": content,
        })
        resp.raise_for_status()
        return resp.json()

    # -- Delete --

    async def delete(self, memory_id: str) -> dict:
        client = await self._get_client()
        resp = await client.post("/delete", json={
            "memory_id": memory_id,
        })
        resp.raise_for_status()
        return resp.json()

    # -- Graph --

    async def graph_export(
        self, limit: int = 50, entity_type: str | None = None,
    ) -> dict:
        client = await self._get_client()
        params: dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        resp = await client.get("/graph/export", params=params)
        resp.raise_for_status()
        return resp.json()

    # -- Stats --

    async def stats(self) -> dict:
        client = await self._get_client()
        resp = await client.get("/stats")
        resp.raise_for_status()
        return resp.json()

    # -- Health check --

    async def health(self) -> dict:
        client = await self._get_client()
        resp = await client.get("/health")
        resp.raise_for_status()
        return resp.json()
