import os
from pydantic import BaseSettings # type: ignore

class Settings:
    # 允许通过环境变量覆盖，默认值如下
    API_TOKEN: str = os.getenv("API_TOKEN", "123456") # 简单的接口保护
    SOURCE_URLS: str = os.getenv("SOURCE_URLS", "") # 逗号分隔的订阅地址
    CRON_INTERVAL: int = int(os.getenv("CRON_INTERVAL", "3600")) # 默认1小时测速一次
    MAX_LATENCY: int = int(os.getenv("MAX_LATENCY", "1500")) # 最大延迟 ms
    MIHOMO_API_PORT: int = 9090
    MIHOMO_API_SECRET: str = "clash-cleaner-secret"

settings = Settings()
