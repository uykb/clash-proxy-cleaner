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
        # Allow configuring data directory via env, default to Docker path
        self.working_dir = os.getenv("DATA_DIR", "/app/data")
        if not os.path.exists(self.working_dir):
            os.makedirs(self.working_dir)
        
        # Allow configuring binary path via env, default to Docker path
        self.mihomo_path = os.getenv("MIHOMO_PATH", "/app/mihomo")

    def get_beijing_time(self):
        """获取北京时间"""
        utc_now = datetime.now(timezone.utc)
        beijing_tz = timezone(timedelta(hours=8))
        return utc_now.astimezone(beijing_tz)

    def get_dynamic_urls(self):
        """Get proxy source URL"""
        if settings.PROXY_URLS:
            if isinstance(settings.PROXY_URLS, list):
                return settings.PROXY_URLS
            return [url.strip() for url in settings.PROXY_URLS.split(',')]

        # Fallback to free-nodes logic if no custom URLs provided
        base_url_prefix = "https://raw.githubusercontent.com/free-nodes/clashfree/refs/heads/main/clash"
        base_url_suffix = ".yml"
        
        now = self.get_beijing_time()
        
        # Generate today and yesterday string YYYYMMDD
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
                current_proxies = []
                success_parse = False
                
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict) and 'proxies' in data:
                        current_proxies = data['proxies']
                        success_parse = True
                        logger.info(f"Successfully loaded {len(current_proxies)} proxies from {url}")
                except Exception as e:
                    logger.error(f"YAML parse error for {url}: {e}")
                    pass
                
                # 如果 YAML 解析失败，尝试 Base64
                if not success_parse:
                    try:
                        # 补全 padding
                        missing_padding = len(content) % 4
                        if missing_padding:
                            content += '=' * (4 - missing_padding)
                        decoded = base64.b64decode(content).decode('utf-8')
                        data = yaml.safe_load(decoded)
                        if isinstance(data, dict) and 'proxies' in data:
                            current_proxies = data['proxies']
                            success_parse = True
                    except Exception:
                        pass
                
                if current_proxies:
                    proxies.extend(current_proxies)

            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
        
        return proxies

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
        
        if not os.path.exists(self.mihomo_path):
            logger.error(f"Mihomo binary not found at: {self.mihomo_path}")
            return False
            
        cmd = [self.mihomo_path, "-d", self.working_dir, "-f", config_path]
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

    def run_test(self, extra_proxies=None):
        """执行完整测试流程"""
        global CLEANED_PROXIES, LAST_UPDATE_TIME
        
        logger.info("Starting proxy cleanup job...")
        proxies = self.fetch_and_parse()
        
        if extra_proxies:
            logger.info(f"Adding {len(extra_proxies)} accumulated proxies...")
            proxies.extend(extra_proxies)
            
        if not proxies:
            logger.warning("No proxies found to test.")
            return

        # Deduplicate before testing
        unique_proxies = {}
        for p in proxies:
            # key based on server:port
            key = f"{p.get('server')}:{p.get('port')}"
            if key not in unique_proxies:
                # Temporary name for testing, will be renamed after test
                p['name'] = f"Node-{len(unique_proxies)}" 
                unique_proxies[key] = p
        
        proxies_to_test = list(unique_proxies.values())
        logger.info(f"Total unique proxies to test: {len(proxies_to_test)}")

        config_path = self.generate_test_config(proxies_to_test)
        
        if not self.start_mihomo(config_path):
            logger.error("Failed to start Mihomo core.")
            return

        logger.info("Mihomo started. Testing connectivity...")
        
        tested_proxies = [] # List of tuples: (proxy_dict, delay_int)
        base_url = f"http://127.0.0.1:{settings.MIHOMO_API_PORT}"
        headers = {"Authorization": f"Bearer {settings.MIHOMO_API_SECRET}"}
        
        for proxy in proxies_to_test:
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
        logger.info(f"Cleanup finished. Retained {len(final_proxies)}/{len(proxies_to_test)} proxies.")

cleaner_service = ProxyCleaner()