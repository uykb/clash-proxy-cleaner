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
    # 立即执行一次
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
def trigger_update(background_tasks: BackgroundTasks):
    # Removed token check
    background_tasks.add_task(cleaner_service.run_test)
    return {"message": "Update triggered in background"}

@app.get("/sub")
def get_subscription():
    """返回 Clash 格式的 YAML"""
    from .cleaner import CLEANED_PROXIES
    # Removed token check
    
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

@app.get("/sub/base64")
def get_subscription_base64():
    """返回 Base64 编码的 YAML"""
    # Removed token check
    from .cleaner import CLEANED_PROXIES
    result = {
        "proxies": CLEANED_PROXIES
    }
    yaml_str = yaml.dump(result, allow_unicode=True)
    b64_str = base64.b64encode(yaml_str.encode('utf-8')).decode('utf-8')
    return PlainTextResponse(b64_str)