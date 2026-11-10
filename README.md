# Free Proxy Airport v7.1

聚合免费代理订阅 + 云端测速，产出 Clash/mihomo 配置。

## 订阅地址

```
https://icecome.github.io/free-proxy-airport/clash.yaml
```

## 本地运行

```pwsh
# 完整采集 + 测速 + 发布（强制走 Phase A，不等 NAS verdict）
$env:FREE_PROXY_AIRPORT_FORCE_COLLECT = '1'
python generator.py

# 只做国内视角 TCP 拨测，不写订阅
python cn_alive_check.py --json out\tcp_report.json
python cn_alive_check.py -o out\alive.yaml

# 回归自检（不需要网络）
python out\test_dedup.py
python out\verify_config.py
```

环境变量（全部可选）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `FREE_PROXY_AIRPORT_FORCE_COLLECT` | `0` | `1` = 强制重新采集与发布 |
| `FREE_PROXY_AIRPORT_MAX_OUTPUT` | `400` | 发布节点上限 |
| `FREE_PROXY_AIRPORT_DEDUP_ENDPOINT` | `1` | 每个入口发布的节点数；`0` 关闭 |
| `FREE_PROXY_AIRPORT_DEDUP_CANDIDATES` | `3` | 每个入口允许进入测速的副本数 |
| `FREE_PROXY_AIRPORT_CN_FRESH` | `7200` | NAS verdict 新鲜度（秒），超时强制采集 |
| `FREE_PROXY_AIRPORT_TEST_URL` | `http://cp.cloudflare.com/generate_204` | 写进客户端的健康检查 URL |
| `FREE_PROXY_AIRPORT_SPEED_ATTEMPTS` | `1` | 每节点下载探测次数 |
| `FREE_PROXY_AIRPORT_TCP_TIMEOUT_MS` | `2000` | TCP 降级拨测超时 |
| `FREE_PROXY_AIRPORT_TCP_WORKERS` | `64` | TCP 降级并发数 |

## 产物

- `output/clash.yaml` —— 生成器输出
- `docs/clash.yaml` —— GitHub Pages 订阅（必须与 `output/clash.yaml` 字节一致，workflow 里 `cmp` 强校验）
- `data/candidates.json` —— 云端阶段性候选（Phase A 产出，Phase B 读取）
- `data/probe_queue.json` —— 下发给 NAS 的待测队列
- `data/cn_alive.json` —— NAS 回传的国内可达结果
- `out/` —— 本地诊断脚本、测试与运行日志（不发布）

## 校验

CI 的 `Validate Clash Verge subscription` 会检查：必需组与规则存在、`output` 与 `docs`
字节一致、Pages 返回 200、所有健康检查 URL 国内可达、**发布节点无重复 endpoint**、
`PROXY` 组保留 `DIRECT`。

请仅在遵守当地法律法规和相关服务条款的前提下使用。
