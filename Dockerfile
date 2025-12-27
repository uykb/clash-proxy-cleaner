# 使用轻量级 Python 镜像
FROM python:3.10-slim

WORKDIR /app

# 安装必要的系统工具 (curl用于下载mihomo, ca-certificates用于https)
RUN apt-get update && apt-get install -y curl ca-certificates gzip && rm -rf /var/lib/apt/lists/*

# 下载 Mihomo 内核 (Clash.Meta)
# 注意：这里使用 GitHub Releases
RUN curl -L -o mihomo.gz https://github.com/MetaCubeX/mihomo/releases/download/v1.17.0/mihomo-linux-amd64-v1.17.0.gz \
    && gzip -d mihomo.gz \
    && chmod +x mihomo

# 下载 Country.mmdb (地理位置库，Clash运行必需)
RUN curl -L -o Country.mmdb https://github.com/Dreamacro/maxmind-geoip/releases/latest/download/Country.mmdb

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
