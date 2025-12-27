from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from fastapi.responses import PlainTextResponse
import yaml
import base64
from apscheduler.schedulers.background import BackgroundScheduler
from .config import settings
from .cleaner import cleaner_service, CLEANED_PROXIES, LAST_UPDATE_TIME

app = FastAPI(title="Clash Proxy Cleaner")

# 启动定时任务
scheduler = BackgroundScheduler()

@app.on_event("startup")
def start_scheduler():
    from datetime import datetime
    # 立即执行一次 (next_run_time=datetime.now())，之后按 interval 循环
    scheduler.add_job(cleaner_service.run_test, 'interval', seconds=settings.CRON_INTERVAL, id='proxy_check', next_run_time=datetime.now())
    scheduler.start()

@app.get("/")
def health_check():
    from .cleaner import LAST_UPDATE_TIME, CLEANED_PROXIES
    return {
        "status": "running",
        "last_update": LAST_UPDATE_TIME,
        "pool_size": len(CLEANED_PROXIES)
    }

@app.post("/trigger")
def trigger_update(background_tasks: BackgroundTasks, token: str = ""):
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    background_tasks.add_task(cleaner_service.run_test)
    return {"message": "Update triggered in background"}

@app.get("/subscribe")
def get_subscription(token: str = ""):
    """返回 Clash 格式的 YAML"""
    from .cleaner import CLEANED_PROXIES
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    result = {
        "proxies": CLEANED_PROXIES,
        "proxy-groups": [
            {
                "name": "Proxy",
                "type": "select",
                "proxies": [p['name'] for p in CLEANED_PROXIES]
            }
        ],
        "rules": [
            "MATCH,Proxy"
        ]
    }
    return PlainTextResponse(yaml.dump(result, allow_unicode=True))

@app.get("/subscribe/base64")
def get_subscription_base64(token: str = ""):
    """返回 Base64 编码的 YAML (兼容部分客户端)"""
    # 注意：这只是 YAML 文件的 Base64，不是 vmess:// 链接列表的 Base64
    # 大多数客户端导入 Base64 都会尝试按 YAML 解析或按节点列表解析
    # 为了兼容性，这里我们直接返回 YAML 内容，客户端通常能自动识别
    # 如果必须 Base64，则对 YAML 进行编码
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    # 获取 YAML 内容
    from .cleaner import CLEANED_PROXIES
    result = {
        "proxies": CLEANED_PROXIES
    }
    yaml_str = yaml.dump(result, allow_unicode=True)
    b64_str = base64.b64encode(yaml_str.encode('utf-8')).decode('utf-8')
    return PlainTextResponse(b64_str)
