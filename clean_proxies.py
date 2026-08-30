import os
import yaml
import requests
import base64
import time
import subprocess
import socket
import random
import concurrent.futures
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


    def decode_base64(self, content):
        """Helper to decode base64 content with padding fix"""
        try:
            content = content.strip().replace('\n', '').replace('\r', '')
            missing_padding = len(content) % 4
            if missing_padding:
                content += '=' * (4 - missing_padding)
            return base64.b64decode(content).decode('utf-8')
        except Exception as e:
            logger.debug(f"Base64 decode error: {e}")
            return None

    def _sanitize_proxy(self, proxy):
        if not proxy: return None
        # Fix SS cipher
        if proxy.get('type') == 'ss':
            cipher = proxy.get('cipher')
            if cipher == 'chacha20-poly1305':
                proxy['cipher'] = 'chacha20-ietf-poly1305'
        return proxy

    def parse_vmess(self, uri):
        try:
            decoded = self.decode_base64(uri[8:])
            if not decoded: return None
            data = json.loads(decoded)
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
                if not auth_dec: return None
                method, password = auth_dec.split(':', 1)
            else:
                # Some ss links are base64(method:password@host:port)
                decoded = self.decode_base64(main)
                if not decoded: return None
                auth, server = decoded.split('@', 1)
                method, password = auth.split(':', 1)
            
            host, port = server.split(':', 1)
            proxy = {
                "name": name,
                "type": "ss",
                "server": host,
                "port": int(port),
                "cipher": method,
                "password": password
            }
            return self._sanitize_proxy(proxy)
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

    def _fetch_single_url(self, url, proxies_list, request_proxies):
        try:
            resp = requests.get(url, headers={"User-Agent": "Clash/1.0.0"}, timeout=15, proxies=request_proxies)
            if resp.status_code != 200:
                return False
            
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
                # Sanitize all proxies
                current_proxies = [self._sanitize_proxy(p) for p in current_proxies if p]
                proxies_list.extend(current_proxies)
                logger.info(f"Found {len(current_proxies)} proxies from {url}")
            
            return True

        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return False

    def fetch_and_parse(self):
        proxies = []
        
        # Optional SOCKS5 proxy for fetching
        request_proxies = None
        socks5 = os.getenv("SOCKS5_PROXY")
        if socks5:
            request_proxies = {"http": socks5, "https": socks5}
            logger.info(f"Using proxy: {socks5}")

        proxy_urls_env = os.getenv("PROXY_URLS")
        if proxy_urls_env:
            target_urls = [url.strip() for url in proxy_urls_env.split(',')]
            for url in target_urls:
                self._fetch_single_url(url, proxies, request_proxies)
        else:
            # Fallback to free-nodes logic with infinite backward loop
            base_url_prefix = "https://raw.githubusercontent.com/free-nodes/clashfree/refs/heads/main/clash"
            base_url_suffix = ".yml"
            now = self.get_beijing_time()
            
            days_back = 0
            while True:
                date_str = (now - timedelta(days=days_back)).strftime("%Y%m%d")
                url = f"{base_url_prefix}{date_str}{base_url_suffix}"
                
                logger.info(f"Fetching from: {url}")
                if not self._fetch_single_url(url, proxies, request_proxies):
                    logger.warning(f"Failed to fetch {url}, stopping auto-fetch loop.")
                    break
                
                days_back += 1
                # Safety limit to prevent infinite loops
                if days_back > 30: 
                    logger.warning("Reached 30 days limit, stopping.")
                    break
        
        return proxies

    def country_code_to_flag(self, country_code):
        """Convert 2-letter country code to flag emoji"""
        if not country_code or len(country_code) != 2:
            return "🌐"
        try:
            return "".join(chr(ord(c.upper()) + 127397) for c in country_code)
        except Exception:
            return "🌐"

    def resolve_host(self, host):
        try:
            return socket.gethostbyname(host)
        except:
            return host

    def fetch_geo_batch(self, targets):
        """Batch fetch GeoIP & IP quality info (hosting vs residential)"""
        unique_targets = list(set(t for t in targets if t))
        if not unique_targets:
            return
        logger.info(f"Fetching GeoIP & IP quality for {len(unique_targets)} unique servers...")
        
        # ip-api batch limit is 100
        batch_size = 100
        for i in range(0, len(unique_targets), batch_size):
            batch = unique_targets[i:i+batch_size]
            try:
                # ip-api batch handles both IP and Domain automatically
                resp = requests.post("http://ip-api.com/batch?fields=status,message,countryCode,hosting,mobile,isp,as,query", 
                                    json=[{"query": h} for h in batch], timeout=15)
                if resp.status_code == 200:
                    results = resp.json()
                    for res in results:
                        if res.get('status') == 'success':
                            self.geo_cache[res.get('query')] = {
                                "countryCode": res.get('countryCode', 'XX'),
                                "hosting": res.get('hosting', False),
                                "mobile": res.get('mobile', False),
                                "isp": res.get('isp', ''),
                                "as": res.get('as', '')
                            }
            except Exception as e:
                logger.error(f"GeoIP batch error: {e}")
            
            # Rate limit for free tier: 15 requests per minute
            if len(unique_targets) > batch_size:
                time.sleep(2)

    def start_mihomo(self, config_path):
        if not os.path.exists(self.mihomo_path):
            logger.error("Mihomo binary not found")
            return False
            
        cmd = [self.mihomo_path, "-d", self.working_dir, "-f", config_path]
        self.mihomo_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # 增加等待时间至 30 秒
        for _ in range(30):
            try:
                resp = requests.get(f"http://127.0.0.1:{self.api_port}/version", 
                                    headers={"Authorization": f"Bearer {self.api_secret}"},
                                    timeout=2)
                if resp.status_code == 200:
                    return True
            except:
                time.sleep(1)
        
        # 打印 Mihomo 错误日志
        try:
            _, stderr = self.mihomo_process.communicate(timeout=5)
            logger.error(f"Mihomo failed to start: {stderr.decode()}")
        except:
            logger.error("Mihomo failed to start and could not read error output")
        return False

    def stop_mihomo(self):
        if self.mihomo_process:
            self.mihomo_process.terminate()
            self.mihomo_process.wait()
            self.mihomo_process = None

    def test_single_proxy(self, proxy_info):
        name, proxy, headers, max_latency, api_port = proxy_info
        try:
            # 使用标准的 HTTPS 204 进行端到端 TLS 握手及真实连通性测试
            test_url = f"http://127.0.0.1:{api_port}/proxies/{name}/delay?timeout=2500&url=https://cp.cloudflare.com/generate_204"
            resp = requests.get(test_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                delay = resp.json().get('delay', 9999)
                return (proxy, delay) if delay < max_latency else None
        except:
            pass
        return None

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

        self.api_port = random.randint(9000, 9999)
        self.mixed_port = random.randint(17800, 17899)
        test_config = {
            "log-level": "silent",
            "external-controller": f"127.0.0.1:{self.api_port}",
            "secret": self.api_secret,
            "mode": "global",
            "mixed-port": self.mixed_port,
            "proxies": proxies_to_test,
            "proxy-groups": [
                {
                    "name": "Proxy",
                    "type": "select",
                    "proxies": [p['name'] for p in proxies_to_test]
                }
            ]
        }
        config_path = os.path.join(self.working_dir, "test_config.yaml")
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(test_config, f, allow_unicode=True)

        if not self.start_mihomo(config_path):
            logger.error("Failed to start Mihomo.")
            exit(1)

        valid_proxies = []
        headers = {"Authorization": f"Bearer {self.api_secret}"}
        
        # 并发测速
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(self.test_single_proxy, (p['name'], p, headers, self.max_latency, self.api_port)): p for p in proxies_to_test}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    valid_proxies.append(result)

        self.stop_mihomo()
        valid_proxies.sort(key=lambda x: x[1])

        # 批量获取有效节点的 GeoIP & IP 质量（极速批量处理）
        if valid_proxies:
            unique_servers = list(set(p.get('server') for p, _ in valid_proxies if p.get('server')))
            self.fetch_geo_batch(unique_servers)
        
        final_list = []
        counts = {}
        for p, delay in valid_proxies:
            ptype = p.get('type', 'Unknown').upper()
            server = p.get('server')
            info = self.geo_cache.get(server)
            
            if isinstance(info, dict):
                country_code = info.get('countryCode') or "XX"
                # hosting 为 False 或者 mobile 为 True 判定为住宅/家宽 IP (RES)
                is_residential = (info.get('hosting') is False) or (info.get('mobile') is True)
                ip_type = "RES" if is_residential else "DC"
            else:
                country_code = info if isinstance(info, str) else "XX"
                ip_type = "DC"

            flag = self.country_code_to_flag(country_code)

            # Format: 🇯🇵[RES] JP-VLESS 85ms 或 🇸🇬[DC] SG-TROJAN 60ms
            base = f"{flag}[{ip_type}] {country_code.upper()}-{ptype} {delay}ms"
            if base in counts:
                counts[base] += 1
                p['name'] = f"{base} {counts[base]}"
            else:
                counts[base] = 0
                p['name'] = base
            final_list.append(p)
        
        if not final_list:
            logger.warning("No valid proxies after testing.")

        output = {
            "proxies": final_list,
            "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": [p['name'] for p in final_list]}],
            "rules": ["MATCH,Proxy"]
        }
        
        with open("subscribe.yaml", "w", encoding="utf-8") as f:
            yaml.dump(output, f, allow_unicode=True)
            
        # Generate base64.txt with fallback
        try:
            with open("base64.txt", "r", encoding="utf-8") as f:
                old_base64 = f.read().strip()
        except FileNotFoundError:
            old_base64 = ""
        
        uris = [self.proxy_to_uri(p) for p in final_list]
        uris = [u for u in uris if u]
        if uris:
            b64_content = base64.b64encode("\n".join(uris).encode()).decode()
            with open("base64.txt", "w", encoding="utf-8") as f:
                f.write(b64_content)
            logger.info(f"Saved {len(uris)} nodes to base64.txt")
        elif old_base64:
            logger.warning("No valid proxies, keeping old base64.txt")
        else:
            with open("base64.txt", "w", encoding="utf-8") as f:
                f.write("")
            logger.warning("No valid proxies, wrote empty base64.txt")
            
        logger.info(f"Done. Saved {len(final_list)} nodes to subscribe.yaml")

if __name__ == "__main__":
    ProxyCleaner().run()
