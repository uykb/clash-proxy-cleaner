import os
import yaml
import requests
import base64
import time
import subprocess
import signal
import json
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

    def fetch_and_parse(self):
        """下载并解析所有订阅源"""
        proxies = []
        urls = [url.strip() for url in settings.SOURCE_URLS.split(',') if url.strip()]
        
        for url in urls:
            try:
                logger.info(f"Fetching from: {url}")
                # 模拟 Clash 客户端或浏览器 UA
                headers = {"User-Agent": "Clash/1.0.0"}
                resp = requests.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                content = resp.text
                
                # 尝试解析 YAML
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict) and 'proxies' in data:
                        proxies.extend(data['proxies'])
                        continue
                except:
                    pass
                
                # 尝试 Base64 解码
                try:
                    # 补全 padding
                    missing_padding = len(content) % 4
                    if missing_padding:
                        content += '=' * (4 - missing_padding)
                    decoded = base64.b64decode(content).decode('utf-8')
                    # 简单的 vmess/trojan/ss 链接解析需要更复杂的逻辑，
                    # 这里假设 Base64 解码后是 YAML 格式或者暂不支持纯链接解析(为简化逻辑)
                    # 实际生产中通常 Base64 解码后是 vmness://... 列表，
                    # 想要完美支持需要集成像 subconverter 这样的转换逻辑。
                    # 为了本方案的可行性，我们暂时假设用户提供的是 Clash 格式的订阅链接，或者由转换器转换过的链接。
                    # 如果解码后包含 proxies: ...
                    data = yaml.safe_load(decoded)
                    if isinstance(data, dict) and 'proxies' in data:
                        proxies.extend(data['proxies'])
                except Exception as e:
                    logger.error(f"Failed to parse base64/content from {url}: {e}")

            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
        
        # 去重 (基于 name 和 server)
        unique_proxies = {}
        for p in proxies:
            key = f"{p.get('server')}:{p.get('port')}"
            # 确保名字不重复，防止 Clash 报错
            if key not in unique_proxies:
                # 清理名字中的非法字符
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
        self.mihomo_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        
        valid_proxies = []
        base_url = f"http://127.0.0.1:{settings.MIHOMO_API_PORT}"
        headers = {"Authorization": f"Bearer {settings.MIHOMO_API_SECRET}"}
        
        # 批量测试
        # Mihomo API 不支持一次性测所有，需要遍历或者创建一个 url-test 组
        # 这里为了精准控制，我们遍历请求 /proxies/{name}/delay
        
        # 优化：并行测试太复杂，这里用简单的串行，因为是后台任务
        # 如果节点多，可以考虑用 ThreadPoolExecutor
        
        for proxy in proxies:
            name = proxy['name']
            # 对中文名进行 URL 编码处理由 requests 自动完成
            try:
                test_url = f"{base_url}/proxies/{name}/delay?timeout=2000&url=http://www.gstatic.com/generate_204"
                resp = requests.get(test_url, headers=headers, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    delay = data.get('delay', 9999)
                    if delay < settings.MAX_LATENCY:
                        # 这是一个好节点
                        # 恢复原始名字(可选)，或者保留重命名
                        proxy['name'] = f"{proxy.get('type').upper()} {delay}ms"
                        valid_proxies.append(proxy)
            except Exception:
                pass # 超时或错误

        self.stop_mihomo()
        
        # 按延迟排序
        # 此时 valid_proxies 里的名字已经改成了带延迟的，无法直接用来排序回原来的字典
        # 简化处理：直接存
        valid_proxies.sort(key=lambda x: int(x['name'].split()[1].replace('ms','')))
        
        CLEANED_PROXIES = valid_proxies
        LAST_UPDATE_TIME = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Cleanup finished. Retained {len(valid_proxies)}/{len(proxies)} proxies.")

cleaner_service = ProxyCleaner()
