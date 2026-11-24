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
python out\test_loopback.py
python out\verify_config.py output\clash.yaml
```

## 本地节点体检（2026-11-24 起）

云端 `generator.py` 只按 delay 排序，且采集源在本机常常整段不可达（`raw.githubusercontent.com`
被 RST）。本地体检把「当前发布列表到底还能不能用」测实，并把结论写回 `data/node_registry.json`：

```pwsh
# 0) 引擎：mihomo 走 ghproxy.net 镜像下载，落在 .tools/（gitignore 已忽略）
#    1.19.34 windows-amd64-compatible；sing-box 1.14.4 只作交叉复核
# 1) 全量体检：TCP -> mihomo delay -> egress 国家/IP -> 流式测速
python scripts\local_verify.py --workers 8 --batch 50 --no-registry
# 2) 二次意见：对首轮被判死的节点重试 TCP x3，再用 sing-box 独立复测
python scripts\rescue_pass.py --workers 16
# 3) 结论落库：mihomo 判定为准（客户端跑的是 mihomo），sing-box 只作证据
python scripts\apply_verdicts.py --apply --extra out\rescue_report.json `
  --extra-authoritative out\confirm_mihomo.json
# 4) 只用 verified 条目重建订阅（凭据级去重 + 复用 generator 的发布/校验链路）
python scripts\publish_verified.py --apply
```

| 脚本 | 作用 | 产物 |
|---|---|---|
| `scripts/local_verify.py` | mihomo 批量体检，四阶段记录死在哪一步 | `out/verify_report.json`、`out/verify_summary.txt` |
| `scripts/rescue_pass.py` | 对首轮判死的节点做二次意见（TCP x3 + sing-box 独立隧道） | `out/rescue_report.json` |
| `scripts/apply_verdicts.py` | 把报告写进 registry（verified 24h / failed IP 7d 域 3d） | `data/node_registry.json` |
| `scripts/publish_verified.py` | 只用 verified 条目出订阅，凭据级去重 | `output/clash.yaml`、`docs/clash.yaml` |

**两条实测出来的规则**（2026-11-24，400 节点全量）：

1. **单引擎判定不能当黑名单依据。** 55 节点交叉复核：mihomo 认为 15 个可用、sing-box 认为 16 个，
   只有 12 个一致 —— 双向各有 3 个「对方说死、自己说活」。所以被判死的节点必须过 `rescue_pass.py`，
   `apply_verdicts.py` 只把 mihomo 的结论当判决（发布物是 mihomo 配置），sing-box 的「OK」只写成
   `engine-mismatch` 证据，避免 7 天误杀。
2. **凭据级去重只对 vmess/vless/trojan/ss 成立。** socks5 的 `password` 常是公共默认值（本次 6 个
   节点同为 `Og==`），按凭据折叠会把互不相干的家宽出口删掉，所以 socks5/http 一律按 endpoint 认身份。

## 已修问题（2026-11-10）

| 症状 | 根因 | 修复 |
|---|---|---|
| 客户端全部「测试连接失败」 | 每个 url-test 组的 `url` 是 `http://www.gstatic.com/generate_204`，国内直连 TCP 超时 | 换成 `http://cp.cloudflare.com/generate_204`（国内实测 204/614ms），url-test 加 `tolerance=50` / `timeout=5000` / `max-failed-times=3`，`PROXY` 组加 `DIRECT` |
| 400 节点几乎从不更新、固定几个能用 | `CN_ALIVE_BOOST=1000` 把 NAS 验证节点永久钉在截断线上；run 预算 105min > cron 2h 导致排队/取消；`has_nas_verdict` 恒真使 Phase A 从不执行、probe_queue 冻结 8 天；mihomo 下载 403 后回退重发旧文件 | `cn_verified` 改排序层级；测速预算收紧；cron `*/30` + `timeout-minutes: 40`；`FORCE_COLLECT` + 陈旧 verdict 强制采集；纯 Python TCP 降级，二进制拿不到也产出新订阅 |
| 去重后仍重复、固定 73 个 | 旧去重按含 UUID 的指纹判重，同一 CDN 入口 12 个不同 UUID 全部通过 | `client_endpoint_key(host, port)` 按客户端实际列出去重，候选期每入口 3 副本、发布期 1 个 |
| 生成配置含本地回环节点 | 产物里 vmess `[NoMoreWalls#001] 防范境外势力渗透` 指向 `127.0.0.53:80`（uuid `88888888-...`），溯源为 `yy1588133/proxy-pool/main/clash.yaml` 中 10 处 `server: 127.0.0.53`；旧 `normalize_proxy` 只判 `if not server` | 新增 `is_non_routable_server()`（`ipaddress` 覆盖 127.0.0.0/8、`::1`、`0.0.0.0`、169.254、组播、保留段、私网段、localhost 字面量），在 4 个入口拦截：`normalize_proxy` 主采集、`load_existing_metrics` 回退重发旁路、`load_candidates` Phase B 旁路、`publish` 最后兜底；CI `Validate` 与 `verify_config.py` 加回归守卫，`out/test_loopback.py` 覆盖 21 拒绝 + 8 放行 + 3 条旁路 |

注意：`sni: 127.0.0.1` 之类字段**不过滤**——SNI 是 TLS 握手声明的主机名，由服务端解释，拨号目标仍是 `server` 的公网域名。域名形态的 `server` 也放行，不在采集期做 DNS 解析，交由下游 TCP/egress 门判定。

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
| `FREE_PROXY_AIRPORT_ENGINE` | 空 | 本地体检用的 mihomo 可执行文件路径；默认 `.tools/mihomo/mihomo.exe` |

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
