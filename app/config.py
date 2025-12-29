import os
from typing import Optional
from pydantic_settings import BaseSettings # type: ignore

class Settings(BaseSettings):
    # 允许通过环境变量覆盖
    SOCKS5_PROXY: Optional[str] = os.getenv("SOCKS5_PROXY") # 例如 socks5://127.0.0.1:1080
    PROXY_URLS: Optional[str] = os.getenv("PROXY_URLS") # Comma separated URLs
    CRON_INTERVAL: int = int(os.getenv("CRON_INTERVAL", "3600")) # 默认1小时测速一次
    MAX_LATENCY: int = int(os.getenv("MAX_LATENCY", "1500")) # 最大延迟 ms
    MIHOMO_API_PORT: int = 9090
    MIHOMO_API_SECRET: str = "clash-cleaner-secret"

settings = Settings()