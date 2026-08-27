"""embedding-server — 本地 bge-m3 embedding 服务。

轻量 HTTP 接口，供 mem0x API 容器调用。
启动: python server.py
端口: 28770
"""
import os
import time
import logging
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("embedding-server")

app = FastAPI(title="bge-m3 embedding server")

# 模型配置
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
MODEL_PATH = os.environ.get("EMBEDDING_MODEL_PATH", f"/app/models/{MODEL_NAME}")

# 全局模型实例
_model = None


class EmbedRequest(BaseModel):
    texts: List[str]


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]
    model: str
    dimensions: int
    elapsed_ms: float


@app.on_event("startup")
def load_model():
    global _model
    logger.info("加载模型: %s ...", MODEL_PATH if os.path.exists(MODEL_PATH) else MODEL_NAME)
    t0 = time.time()
    _model = SentenceTransformer(MODEL_PATH if os.path.exists(MODEL_PATH) else MODEL_NAME)
    logger.info("模型加载完成，耗时 %.1fs", time.time() - t0)


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    t0 = time.time()
    embeddings = _model.encode(req.texts, normalize_embeddings=True)
    elapsed = (time.time() - t0) * 1000
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        model=MODEL_NAME,
        dimensions=embeddings.shape[1],
        elapsed_ms=round(elapsed, 1),
    )


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "loaded": _model is not None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=28770)
