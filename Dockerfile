FROM python:3.12-slim

# 使用清华镜像源（pip + apt）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装spaCy英文小模型（mem0硬编码依赖，本地tar避免GitHub超时）
COPY en_core_web_sm-3.8.0.tar.gz /tmp/
RUN tar xzf /tmp/en_core_web_sm-3.8.0.tar.gz -C /usr/local/lib/python3.12/site-packages/ && rm -rf /tmp/*.tar.gz

# 复制代码
COPY wrapper/ ./wrapper/
COPY security/ ./security/
COPY mem0x_server.py .

# 创建数据目录
RUN mkdir -p /app/data

EXPOSE 28768

# 环境变量
ENV MEM0X_HOME=/app
ENV MEM0X_CONFIG=/app/config.json
ENV MEM0X_DATA_DIR=/app/data

# 安装 curl（健康检查用）
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:28768/health || exit 1

CMD ["python", "mem0x_server.py"]
