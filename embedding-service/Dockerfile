FROM python:3.11-slim

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 装依赖（CPU 版 torch + sentence-transformers）
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir fastapi uvicorn sentence-transformers

# 预下载模型（构建时下载，运行时零网络）
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

COPY server.py .

EXPOSE 28770

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:28770/health || exit 1

CMD ["python", "server.py"]
