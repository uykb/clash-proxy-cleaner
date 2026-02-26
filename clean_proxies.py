import os
import yaml
import requests
import base64
import time
import subprocess
import socket
from datetime import datetime, timedelta, timezone
from loguru import logger

import urllib.parse
import json

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

    def decode_base64(self, content):
        """Helper to decode base64 content with padding fix"""
        try:
            missing_padding = len(content) % 4
            if missing_padding:
                content += '=' * (4 - missing_padding)
            return base64.b64decode(content).decode('utf-8')
        except Exception as e:
            logger.debug(f"Base64 decode error: {e}")
            return None

    def parse_vmess(self, uri):
        try:
            data = json.loads(self.decode_base64(uri[8:]))
            proxy = {
                "name": data.get("ps", "vmess"),
                "type": "vmess",
                "server": data.get("add"),
                "port": int(data.get("port", 443)),
                "uuid": data.get("id"),
                "alterId": int(data.get("aid", 0)),
                "cipher": "auto",
                "tls": data.get("tls") == "tls",
                "skip-cert-verify": True,
                "network": data.get("net", "tcp")
            }
            if proxy["network"] == "ws":
                proxy["ws-opts"] = {"path": data.get("path", "/"), "headers": {"Host": data.get("host", "")}}
            elif proxy["network"] == "grpc":
                proxy["grpc-opts"] = {"grpc-service-name": data.get("path", "")}
            return proxy
        except: return None

    def parse_ss(self, uri):
        try:
            # ss://base64(method:password)@host:port#name
            parts = uri[5:].split('#', 1)
            name = urllib.parse.unquote(parts[1]) if len(parts) > 1 else "ss"
            main = parts[0]
            if '@' in main:
                auth, server = main.split('@', 1)
                auth_dec = self.decode_base64(auth)
                method, password = auth_dec.split(':', 1)
            else:
                # Some ss links are base64(method:password@host:port)
                decoded = self.decode_base64(main)
                auth, server = decoded.split('@', 1)
                method, password = auth.split(':', 1)
            
            host, port = server.split(':', 1)
            return {
                "name": name,
                "type": "ss",
                "server": host,
                "port": int(port),
                "cipher": method,
                "password": password
            }
        except: return None

    def parse_trojan(self, uri):
        try:
            # trojan://password@host:port?query#name
            u = urllib.parse.urlparse(uri)
            name = urllib.parse.unquote(u.fragment) if u.fragment else "trojan"
            query = urllib.parse.parse_qs(u.query)
            return {
                "name": name,
                "type": "trojan",
                "server": u.hostname,
                "port": int(u.port),
                "password": u.username,
                "sni": query.get("sni", [u.hostname])[0],
                "skip-cert-verify": True
            }
        except: return None

    def parse_vless(self, uri):
        try:
            # vless://uuid@host:port?query#name
            u = urllib.parse.urlparse(uri)
            name = urllib.parse.unquote(u.fragment) if u.fragment else "vless"
            query = urllib.parse.parse_qs(u.query)
            proxy = {
                "name": name,
                "type": "vless",
                "server": u.hostname,
                "port": int(u.port),
                "uuid": u.username,
                "tls": query.get("security", [""])[0] == "tls" or query.get("security", [""])[0] == "reality",
                "skip-cert-verify": True,
                "network": query.get("type", ["tcp"])[0]
            }
            if query.get("security", [""])[0] == "reality":
                proxy["reality-opts"] = {
                    "public-key": query.get("pbk", [""])[0],
                    "short-id": query.get("sid", [""])[0]
                }
            if proxy["network"] == "ws":
                proxy["ws-opts"] = {"path": query.get("path", ["/"])[0], "headers": {"Host": query.get("host", [""])[0]}}
            elif proxy["network"] == "grpc":
                proxy["grpc-opts"] = {"grpc-service-name": query.get("serviceName", [""])[0]}
            return proxy
        except: return None

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
                
                content = resp.text.strip()
                current_proxies = []
                
                # Try YAML first
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict) and 'proxies' in data:
                        current_proxies = data['proxies']
                except: pass
                
                # Try Base64/Standard links if YAML fails or found no proxies
                if not current_proxies:
                    decoded = self.decode_base64(content)
                    if decoded:
                        # Could be YAML in base64 or a list of links
                        try:
                            data = yaml.safe_load(decoded)
                            if isinstance(data, dict) and 'proxies' in data:
                                current_proxies = data['proxies']
                        except: pass
                        
                        if not current_proxies:
                            # Try parsing line by line (vmess://, ss://, etc)
                            for line in decoded.splitlines():
                                line = line.strip()
                                if not line: continue
                                p = None
                                if line.startswith('vmess://'): p = self.parse_vmess(line)
                                elif line.startswith('ss://'): p = self.parse_ss(line)
                                elif line.startswith('trojan://'): p = self.parse_trojan(line)
                                elif line.startswith('vless://'): p = self.parse_vless(line)
                                if p: current_proxies.append(p)
                
                if current_proxies:
                    proxies.extend(current_proxies)
                    logger.info(f"Found {len(current_proxies)} proxies from {url}")
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

    def proxy_to_uri(self, p):
        try:
            ptype = p.get('type')
            name = urllib.parse.quote(p.get('name', ''))
            if ptype == 'vmess':
                data = {
                    "v": "2",
                    "ps": p.get('name'),
                    "add": p.get('server'),
                    "port": str(p.get('port')),
                    "id": p.get('uuid'),
                    "aid": str(p.get('alterId', 0)),
                    "net": p.get('network', 'tcp'),
                    "type": "none",
                    "host": p.get('ws-opts', {}).get('headers', {}).get('Host', '') if p.get('network') == 'ws' else '',
                    "path": p.get('ws-opts', {}).get('path', '/') if p.get('network') == 'ws' else (p.get('grpc-opts', {}).get('grpc-service-name', '') if p.get('network') == 'grpc' else ''),
                    "tls": "tls" if p.get('tls') else ""
                }
                return f"vmess://{base64.b64encode(json.dumps(data).encode()).decode()}"
            elif ptype == 'vless':
                uri = f"vless://{p.get('uuid')}@{p.get('server')}:{p.get('port')}?"
                params = {
                    "type": p.get('network', 'tcp'),
                    "security": "tls" if p.get('tls') else "none"
                }
                if p.get('reality-opts'):
                    params["security"] = "reality"
                    params["pbk"] = p['reality-opts'].get('public-key', '')
                    params["sid"] = p['reality-opts'].get('short-id', '')
                if p.get('network') == 'ws':
                    params["path"] = p.get('ws-opts', {}).get('path', '/')
                    params["host"] = p.get('ws-opts', {}).get('headers', {}).get('Host', '')
                elif p.get('network') == 'grpc':
                    params["serviceName"] = p.get('grpc-opts', {}).get('grpc-service-name', '')
                uri += urllib.parse.urlencode(params)
                uri += f"#{name}"
                return uri
            elif ptype == 'ss':
                auth = base64.b64encode(f"{p.get('cipher')}:{p.get('password')}".encode()).decode()
                return f"ss://{auth}@{p.get('server')}:{p.get('port')}#{name}"
            elif ptype == 'trojan':
                uri = f"trojan://{p.get('password')}@{p.get('server')}:{p.get('port')}?"
                params = {"sni": p.get('sni', p.get('server'))}
                uri += urllib.parse.urlencode(params)
                uri += f"#{name}"
                return uri
        except: pass
        return None

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
            
        # Generate base64.txt
        uris = [self.proxy_to_uri(p) for p in final_list]
        uris = [u for u in uris if u]
        if uris:
            b64_content = base64.b64encode("\n".join(uris).encode()).decode()
            with open("base64.txt", "w", encoding="utf-8") as f:
                f.write(b64_content)
            logger.info(f"Saved {len(uris)} nodes to base64.txt")
            
        logger.info(f"Done. Saved {len(final_list)} nodes to subscribe.yaml")

if __name__ == "__main__":
    ProxyCleaner().run()
