# Free Proxy Airport v7

机场级体验的 Clash Verge / Mihomo 免费节点订阅系统。GitHub Actions 每 30 分钟自动聚合公开免费节点源，解析、去重、剔除全局性死亡节点后生成候选池（上限 400 个）；真正的质量择优由客户端 url-test 和本地 `cn_alive_check.py` 完成。

## 本地存活拨测（国内视角）

```bash
python cn_alive_check.py                # 对订阅内全部节点做 TCP 直连拨测（你的网络视角）
python cn_alive_check.py -o alive.yaml  # 导出存活节点
python cn_alive_check.py --json r.json  # 完整报告
```

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

- 多格式订阅源解析（Clash YAML / Base64+URI / 明文 URI，自动识别）
- 节点指纹去重（同 server:port+凭据 只保留一个）
- 云端全局活性粗筛（mihomo 真实建连，超时节点剔除；带宽准入可选，默认关闭——国内视角择优交给客户端与本地拨测）
- 候选池上限 400（`FREE_PROXY_AIRPORT_MAX_OUTPUT` 可调）
- HK / JP / US / AI 自动分组
- FALLBACK 自动降级（测速失败复用上次产出，池子不足保留低速节点兜底）
- OpenAI / ChatGPT / Claude / Anthropic 智能分流
- mihomo 内核下载带 sha256 完整性校验

输出文件：

```text
output/clash.yaml
docs/clash.yaml
```

请仅在遵守当地法律法规和相关服务条款的前提下使用。
