import os
import yaml
import requests
import base64
import time
import subprocess
import socket
from datetime import datetime, timedelta, timezone
from loguru import logger

class ProxyCleaner:
    def __init__(self):
        self.mihomo_process = None
        self.working_dir = "data"
        if not os.path.exists(self.working_dir):
            os.makedirs(self.working_dir)
        
        # Paths for GitHub Actions environment
        self.mihomo_path = "./mihomo"
        self.max_latency = int(os.getenv("MAX_LATENCY", "1500"))
        self.api_port = 9090
        self.api_secret = "clash-cleaner-secret"
        self.geo_cache = {}

    def get_beijing_time(self):
        utc_now = datetime.now(timezone.utc)
        beijing_tz = timezone(timedelta(hours=8))
        return utc_now.astimezone(beijing_tz)

    def get_dynamic_urls(self):
        proxy_urls = os.getenv("PROXY_URLS")
        if proxy_urls:
            return [url.strip() for url in proxy_urls.split(',')]

        # Fallback to free-nodes logic
        base_url_prefix = "https://raw.githubusercontent.com/free-nodes/clashfree/refs/heads/main/clash"
        base_url_suffix = ".yml"
        now = self.get_beijing_time()
        date_today = now.strftime("%Y%m%d")
        date_yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        return [f"{base_url_prefix}{date_today}{base_url_suffix}", f"{base_url_prefix}{date_yesterday}{base_url_suffix}"]

    def fetch_and_parse(self):
        proxies = []
        target_urls = self.get_dynamic_urls()
        
        # Optional SOCKS5 proxy for fetching
        request_proxies = None
        socks5 = os.getenv("SOCKS5_PROXY")
        if socks5:
            request_proxies = {"http": socks5, "https": socks5}
            logger.info(f"Using proxy: {socks5}")

        for url in target_urls:
            try:
                logger.info(f"Fetching from: {url}")
                resp = requests.get(url, headers={"User-Agent": "Clash/1.0.0"}, timeout=15, proxies=request_proxies)
                if resp.status_code != 200: continue
                
                content = resp.text
                current_proxies = []
                success = False
                
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict) and 'proxies' in data:
                        current_proxies = data['proxies']
                        success = True
                except: pass
                
                if not success:
                    try:
                        missing_padding = len(content) % 4
                        if missing_padding: content += '=' * (4 - missing_padding)
                        decoded = base64.b64decode(content).decode('utf-8')
                        data = yaml.safe_load(decoded)
                        if isinstance(data, dict) and 'proxies' in data:
                            current_proxies = data['proxies']
                    except: pass
                
                if current_proxies:
                    proxies.extend(current_proxies)
                    logger.info(f"Found {len(current_proxies)} proxies")
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
        return proxies

    def resolve_host(self, host):
        try:
            return socket.gethostbyname(host)
        except:
            return host

    def fetch_geo_batch(self, proxies):
        """Batch fetch GeoIP info for proxy servers"""
        unique_hosts = list(set(p.get('server') for p in proxies if p.get('server')))
        logger.info(f"Fetching GeoIP for {len(unique_hosts)} unique hosts...")
        
        # ip-api batch limit is 100
        batch_size = 100
        for i in range(0, len(unique_hosts), batch_size):
            batch = unique_hosts[i:i+batch_size]
            try:
                # We send just the hosts, ip-api handles both IP and Domain
                resp = requests.post("http://ip-api.com/batch?fields=status,message,countryCode,query", 
                                    json=[{"query": h} for h in batch], timeout=20)
                if resp.status_code == 200:
                    results = resp.json()
                    for res in results:
                        if res.get('status') == 'success':
                            self.geo_cache[res.get('query')] = res.get('countryCode')
            except Exception as e:
                logger.error(f"GeoIP batch error: {e}")
            
            # Rate limit for free tier: 15 requests per minute
            if len(unique_hosts) > batch_size:
                time.sleep(2)

    def start_mihomo(self, config_path):
        if not os.path.exists(self.mihomo_path):
            logger.error("Mihomo binary not found")
            return False
            
        cmd = [self.mihomo_path, "-d", self.working_dir, "-f", config_path]
        self.mihomo_process = subprocess.Popen(cmd)
        for _ in range(15):
            try:
                requests.get(f"http://127.0.0.1:{self.api_port}/version", 
                             headers={"Authorization": f"Bearer {self.api_secret}"})
                return True
            except:
                time.sleep(1)
        return False

    def stop_mihomo(self):
        if self.mihomo_process:
            self.mihomo_process.terminate()
            self.mihomo_process.wait()

    def run(self):
        logger.info("Starting proxy cleanup...")
        raw_proxies = self.fetch_and_parse()
        if not raw_proxies:
            logger.warning("No proxies found.")
            return

        unique_proxies = {}
        for p in raw_proxies:
            key = f"{p.get('server')}:{p.get('port')}"
            if key not in unique_proxies:
                p['name'] = f"Node-{len(unique_proxies)}"
                unique_proxies[key] = p
        
        proxies_to_test = list(unique_proxies.values())
        logger.info(f"Testing {len(proxies_to_test)} unique nodes...")

        test_config = {
            "log-level": "silent",
            "external-controller": f"0.0.0.0:{self.api_port}",
            "secret": self.api_secret,
            "mode": "global",
            "proxies": proxies_to_test
        }
        config_path = os.path.join(self.working_dir, "test_config.yaml")
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(test_config, f, allow_unicode=True)

        if not self.start_mihomo(config_path):
            logger.error("Failed to start Mihomo.")
            return

        valid_proxies = []
        headers = {"Authorization": f"Bearer {self.api_secret}"}
        
        for proxy in proxies_to_test:
            name = proxy['name']
            try:
                test_url = f"http://127.0.0.1:{self.api_port}/proxies/{name}/delay?timeout=2000&url=http://www.gstatic.com/generate_204"
                resp = requests.get(test_url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    delay = resp.json().get('delay', 9999)
                    if delay < self.max_latency:
                        valid_proxies.append((proxy, delay))
            except: pass

        self.stop_mihomo()
        valid_proxies.sort(key=lambda x: x[1])

        # Fetch GeoIP for valid proxies
        if valid_proxies:
            self.fetch_geo_batch([p for p, _ in valid_proxies])
        
        final_list = []
        counts = {}
        for p, delay in valid_proxies:
            ptype = p.get('type', 'Unknown').upper()
            server = p.get('server')
            geo = self.geo_cache.get(server, "XX")
            
            # Format: JP-VMESS 120ms
            base = f"{geo}-{ptype} {delay}ms"
            if base in counts:
                counts[base] += 1
                p['name'] = f"{base} {counts[base]}"
            else:
                counts[base] = 0
                p['name'] = base
            final_list.append(p)

        output = {
            "proxies": final_list,
            "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": [p['name'] for p in final_list]}],
            "rules": ["MATCH,Proxy"]
        }
        
        with open("subscribe.yaml", "w", encoding="utf-8") as f:
            yaml.dump(output, f, allow_unicode=True)
            
        logger.info(f"Done. Saved {len(final_list)} nodes to subscribe.yaml")

if __name__ == "__main__":
    ProxyCleaner().run()
