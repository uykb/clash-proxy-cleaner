# Clash Proxy Cleaner (GitHub Actions Edition)

这是一个自动清洗 Clash 代理节点的工具，已从 Docker 部署迁移到 GitHub Actions 方案。

## 工作原理

1. **GitHub Actions** 每天伦敦时间 00:00 自动运行一次。
2. 下载 **Mihomo (Clash Meta)** 内核。
3. 从配置的订阅源获取节点。
4. 使用 Mihomo 进行实际连通性和延迟测速。
5. 过滤掉高延迟节点，并将结果按延迟排序。
6. 生成 `subscribe.yaml` 并提交回仓库。

## 如何使用

### 1. 配置订阅源
在 GitHub 仓库设置中添加 Secret：
- `PROXY_URLS`: 您的原始订阅链接，多个链接用逗号分隔。

### 2. 获取清洗后的订阅
您的订阅地址为：
`https://raw.githubusercontent.com/您的用户名/您的仓库名/main/subscribe.yaml`

## 配置项
可以在 `.github/workflows/clean-proxies.yml` 中修改以下环境变量：
- `MAX_LATENCY`: 最大允许延迟（默认 1500ms）。
- `CRON`: 触发频率（默认每天一次）。
