# 使用已有的镜像作为基础
FROM ghcr.io/uykb/clash-proxy-cleaner:latest

WORKDIR /app

# 下载兼容版 Mihomo (v1.17.0 compatible) 以支持旧型号 CPU (缺乏 v3 指令集)
# 覆盖基础镜像中的 /app/mihomo
# 先删除旧的 mihomo 防止 gzip 报错 "already exists"
RUN apt-get update && apt-get install -y curl gzip && \
    rm -f mihomo && \
    curl -L -o mihomo.gz https://github.com/MetaCubeX/mihomo/releases/download/v1.17.0/mihomo-linux-amd64-compatible-v1.17.0.gz \
    && gzip -d mihomo.gz \
    && chmod +x mihomo \
    && rm -rf /var/lib/apt/lists/*
    # mihomo 已经在 /app 目录下，无需移动

# 复制依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码
COPY . .

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
