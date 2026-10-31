# Free Proxy Airport v7

机场级体验的 Clash Verge / Mihomo 免费节点订阅系统。GitHub Actions 每 30 分钟自动聚合公开免费节点源，执行真实延迟测试与带宽准入测速，剔除超时、无效和低带宽节点，并按健康评分生成自动分组。

## Clash Verge 使用方式

Profiles -> Add URL:

```text
https://icecome.github.io/free-proxy-airport/clash.yaml
```

## 节点源管理

所有订阅源集中在仓库根目录的 `sources.txt`，**一行一个 URL**，`#` 开头为注释：

- 加新源：GitHub 网页上直接编辑 `sources.txt` 加一行，下一轮（≤30 分钟）自动生效
- 删死源：某源连续多轮日志出现 `[WARN] source skipped`，直接删掉那行
- 源健康看板：每轮运行日志输出 `[SUMMARY] sources_total=N sources_ok=M`，M 长期为 0 时检查你的 URL

## 自动更新

工作流名称：`AI Self-Healing Proxy v7`

节点源：见 `sources.txt`（默认内置 openRunner / snakem982 proxypool / Flikify / free-clash-v2ray 四个源）

核心能力：

- 真实延迟测速和 timeout 自动剔除
- 带宽准入筛选（top 30 节点经 Cloudflare 测速端点实测下载，< 1Mbps 淘汰，可通过 `FREE_PROXY_AIRPORT_MIN_MBPS` / `FREE_PROXY_AIRPORT_SPEED_TOP_N` / `FREE_PROXY_AIRPORT_MIN_POOL` 环境变量调整）
- 节点健康评分排序（延迟 0.4 + 带宽 0.4 + 地区 0.2）
- HK / JP / US / AI 自动分组
- FALLBACK 自动降级（测速失败复用上次产出，池子不足保留低速节点兜底）
- OpenAI / ChatGPT / Claude / Anthropic 智能分流

输出文件：

```text
output/clash.yaml
docs/clash.yaml
```

请仅在遵守当地法律法规和相关服务条款的前提下使用。
