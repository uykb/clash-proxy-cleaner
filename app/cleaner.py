import os
import yaml
import requests
import base64
import time
import subprocess
import signal
import json
from datetime import datetime, timedelta, timezone
from loguru import logger
from .config import settings

# 全局存储清洗后的结果
CLEANED_PROXIES = []
LAST_UPDATE_TIME = "Never"

class ProxyCleaner:
    def __init__(self):
        self.mihomo_process = None
        self.working_dir = "/app/data"
        if not os.path.exists(self.working_dir):
            os.makedirs(self.working_dir)

    def get_beijing_time(self):
        """获取北京时间"""
        utc_now = datetime.now(timezone.utc)
        beijing_tz = timezone(timedelta(hours=8))
        return utc_now.astimezone(beijing_tz)

    def get_dynamic_urls(self):
        """动态生成订阅地址（参考 clash_worker.js 逻辑）"""
        base_url_prefix = "https://raw.githubusercontent.com/free-nodes/clashfree/refs/heads/main/clash"
        base_url_suffix = ".yml"
        
        now = self.get_beijing_time()
        
        # 生成今天和昨天的日期字符串 YYYYMMDD
        date_str_today = now.strftime("%Y%m%d")
        date_str_yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        
        urls = [
            f"{base_url_prefix}{date_str_today}{base_url_suffix}",
            f"{base_url_prefix}{date_str_yesterday}{base_url_suffix}"
        ]
        return urls

    def fetch_and_parse(self):
        """下载并解析动态订阅源"""
        proxies = []
        target_urls = self.get_dynamic_urls()
        
        # 配置代理
        request_proxies = None
        if settings.SOCKS5_PROXY:
            request_proxies = {
                "http": settings.SOCKS5_PROXY,
                "https": settings.SOCKS5_PROXY
            }
            logger.info(f"Using proxy: {settings.SOCKS5_PROXY}")

        success = False
        for url in target_urls:
            try:
                logger.info(f"Attempting to fetch from: {url}")
                # 模拟 Clash 客户端或浏览器 UA
                headers = {"User-Agent": "Clash/1.0.0"}
                
                resp = requests.get(
                    url, 
                    headers=headers, 
                    timeout=15, 
                    proxies=request_proxies
                )
                
                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch {url}: Status {resp.status_code}")
                    continue

                content = resp.text
                
                # 尝试解析 YAML
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict) and 'proxies' in data:
                        proxies.extend(data['proxies'])
                        success = True
                        logger.info(f"Successfully loaded proxies from {url}")
                        break # 成功获取一个即可，参考 worker 逻辑是优先取最新的
                except Exception as e:
                    logger.error(f"YAML parse error for {url}: {e}")
                    pass
                
                # 如果 YAML 解析失败，尝试 Base64 (虽然这个源通常是 YAML，为了健壮性保留)
                if not success:
                    try:
                        # 补全 padding
                        missing_padding = len(content) % 4
                        if missing_padding:
                            content += '=' * (4 - missing_padding)
                        decoded = base64.b64decode(content).decode('utf-8')
                        data = yaml.safe_load(decoded)
                        if isinstance(data, dict) and 'proxies' in data:
                            proxies.extend(data['proxies'])
                            success = True
                            break
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
        
        if not success:
            logger.error("All dynamic sources failed.")
            return []

        # 去重 (基于 name 和 server)
        unique_proxies = {}
        for p in proxies:
            key = f"{p.get('server')}:{p.get('port')}"
            # 确保名字不重复，防止 Clash 报错
            if key not in unique_proxies:
                # 清理名字中的非法字符，这里保留部分原名信息可能更好，但为了安全统一重命名或保留原名
                # 既然是动态获取，原名可能包含推广信息，这里重置为 Node-X 比较稳妥，或者 clean 一下
                p['name'] = f"Node-{len(unique_proxies)}-{p['type']}" 
                unique_proxies[key] = p
        
        logger.info(f"Total unique proxies fetched: {len(unique_proxies)}")
        return list(unique_proxies.values())

    def generate_test_config(self, proxies):
        """生成用于测试的 Clash 配置"""
        config = {
            "log-level": "info",
            "external-controller": f"0.0.0.0:{settings.MIHOMO_API_PORT}",
            "secret": settings.MIHOMO_API_SECRET,
            "mode": "global",
            "proxies": proxies
        }
        config_path = os.path.join(self.working_dir, "test_config.yaml")
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True)
        return config_path

    def start_mihomo(self, config_path):
        """启动 Mihomo 内核"""
        self.stop_mihomo()
        cmd = ["/app/mihomo", "-d", self.working_dir, "-f", config_path]
        # Allow Mihomo output to flow to docker logs for debugging
        self.mihomo_process = subprocess.Popen(cmd)
        # 等待启动
        for _ in range(10):
            try:
                requests.get(f"http://127.0.0.1:{settings.MIHOMO_API_PORT}/version", 
                             headers={"Authorization": f"Bearer {settings.MIHOMO_API_SECRET}"})
                return True
            except:
                time.sleep(1)
        return False

    def stop_mihomo(self):
        """停止内核"""
        if self.mihomo_process:
            self.mihomo_process.terminate()
            self.mihomo_process.wait()
            self.mihomo_process = None

    def run_test(self):
        """执行完整测试流程"""
        global CLEANED_PROXIES, LAST_UPDATE_TIME
        
        logger.info("Starting proxy cleanup job...")
        proxies = self.fetch_and_parse()
        if not proxies:
            logger.warning("No proxies found to test.")
            return

        config_path = self.generate_test_config(proxies)
        
        if not self.start_mihomo(config_path):
            logger.error("Failed to start Mihomo core.")
            return

        logger.info("Mihomo started. Testing connectivity...")
        
        tested_proxies = [] # List of tuples: (proxy_dict, delay_int)
        base_url = f"http://127.0.0.1:{settings.MIHOMO_API_PORT}"
        headers = {"Authorization": f"Bearer {settings.MIHOMO_API_SECRET}"}
        
        for proxy in proxies:
            name = proxy['name']
            try:
                test_url = f"{base_url}/proxies/{name}/delay?timeout=2000&url=http://www.gstatic.com/generate_204"
                resp = requests.get(test_url, headers=headers, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    delay = data.get('delay', 9999)
                    if delay < settings.MAX_LATENCY:
                        tested_proxies.append((proxy, delay))
            except Exception:
                pass 

        self.stop_mihomo()
        
        # 按延迟排序
        tested_proxies.sort(key=lambda x: x[1])
        
        final_proxies = []
        name_counts = {}
        
        for p, delay in tested_proxies:
            p_type = p.get('type', 'Unknown').upper()
            base_name = f"{p_type} {delay}ms"
            
            # 处理重名
            if base_name in name_counts:
                name_counts[base_name] += 1
                new_name = f"{base_name} {name_counts[base_name]}"
            else:
                name_counts[base_name] = 0
                new_name = base_name
                
            p['name'] = new_name
            final_proxies.append(p)
        
        CLEANED_PROXIES = final_proxies
        LAST_UPDATE_TIME = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Cleanup finished. Retained {len(final_proxies)}/{len(proxies)} proxies.")

cleaner_service = ProxyCleaner()