FROM python:3.12-slim

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

COPY requirements.txt .
# 降级 numpy 兼容 spacy-pkuseg
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "numpy>=1.26,<2.0"

# spaCy 英文模型（tar 解压）
COPY en_core_web_sm-3.8.0.tar.gz /tmp/
RUN tar xzf /tmp/en_core_web_sm-3.8.0.tar.gz -C /usr/local/lib/python3.12/site-packages/ && rm -rf /tmp/*.tar.gz

# spaCy 中文模型（pip install tar.gz，自动处理路径）
COPY zh_core_web_sm-3.8.0.tar.gz /tmp/
RUN pip install --no-cache-dir /tmp/zh_core_web_sm-3.8.0.tar.gz && rm -rf /tmp/*.tar.gz

COPY wrapper/ ./wrapper/
COPY security/ ./security/
COPY plugin/ ./plugin/
COPY mem0x_server.py .

RUN mkdir -p /app/data
EXPOSE 28768

ENV MEM0X_HOME=/app
ENV MEM0X_CONFIG=/app/config.json
ENV MEM0X_DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:28768/health || exit 1

CMD ["python", "mem0x_server.py"]
