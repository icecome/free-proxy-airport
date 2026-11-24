# Free Proxy Airport v7.1

聚合免费代理订阅 + 云端测速，产出 Clash/mihomo 配置。

## 订阅地址

```
https://icecome.github.io/free-proxy-airport/clash.yaml
```

> 这个 URL 由 `docs/clash.yaml` 提供。`docs/` 是 GitHub Pages 源目录，
> **只放订阅产物，不放文档** —— 详见 [documentation/structure.md](documentation/structure.md) §2.1。

## 项目结构

```
airport/          业务库与流水线入口（采集 / 验证 / 台账 / 发布）
scripts/          源发现脚本（人工、低频）
tests/            回归测试（纳入版本控制，不依赖网络）
documentation/    人类文档：design / dev / ops / reference
data/             提交的运行时状态
output/ docs/     订阅产物（两份必须字节一致）
nas/              NAS 容器部署脚本
out/ .tools/      本地草稿区与引擎二进制（gitignored）
```

完整目录树、各目录职责、新增文件放哪：**[documentation/structure.md](documentation/structure.md)**。
文档索引：**[documentation/README.md](documentation/README.md)**。

## 快速开始

```pwsh
python -m pip install requests pyyaml PySocks

# 回归测试（不需要网络）
python tests\test_dedup.py
python tests\test_loopback.py
python tests\verify_config.py

# 完整采集 + 测速 + 发布（强制走 Phase A，不等 NAS 判定）
$env:FREE_PROXY_AIRPORT_FORCE_COLLECT = '1'
python -m airport.generator
```

所有模块都支持两种调用形式：`python -m airport.<模块>` 与 `python airport\<模块>.py`。
路径由 `airport/paths.py` 统一锚定仓库根，从任何工作目录跑都指向同一处。

## 本地节点体检

云端 `generator.py` 只按 delay 排序，且采集源在本机常常整段不可达
（`raw.githubusercontent.com` 被 RST）。本地体检把「当前发布列表到底还能不能用」测实，
并把结论写回 `data/node_registry.json`：

```pwsh
python -m airport.local_verify --workers 8 --batch 50 --no-registry   # 四阶段体检
python -m airport.rescue_pass --workers 16                            # 判死节点二次意见
python -m airport.apply_verdicts --apply `
  --extra out\rescue_report.json --extra-authoritative out\confirm_mihomo.json
python -m airport.publish_verified --apply                            # 只用 verified 重建订阅
```

| 模块 | 作用 | 产物 |
|---|---|---|
| `airport/local_verify.py` | mihomo 批量体检：TCP → delay → egress → 流式测速 | `out/verify_report.json` |
| `airport/rescue_pass.py` | 对首轮判死的节点做二次意见（TCP×3 + sing-box 独立隧道） | `out/rescue_report.json` |
| `airport/apply_verdicts.py` | 报告写进台账（verified 24h / failed IP 7d 域 3d） | `data/node_registry.json` |
| `airport/publish_verified.py` | 只用 verified 条目出订阅，凭据级去重 | `output/clash.yaml`、`docs/clash.yaml` |

**两条实测出来的规则**（2026-11-24，400 节点全量）：

1. **单引擎判定不能当黑名单依据。** 55 节点交叉复核：mihomo 认为 15 个可用、sing-box 认为 16 个，
   只有 12 个一致 —— 双向各有 3 个「对方说死、自己说活」。所以被判死的节点必须过
   `rescue_pass.py`，`apply_verdicts.py` 只把 mihomo 的结论当判决（发布物是 mihomo 配置），
   sing-box 的「OK」只写成 `engine-mismatch` 证据，避免 7 天误杀。
2. **凭据级去重只对 vmess/vless/trojan/ss 成立。** socks5 的 `password` 常是公共默认值
   （本次 6 个节点同为 `Og==`），按凭据折叠会把互不相干的家宽出口删掉，
   所以 socks5/http 一律按 endpoint 认身份。

## 已修问题（2026-11-10）

| 症状 | 根因 | 修复 |
|---|---|---|
| 客户端全部「测试连接失败」 | 每个 url-test 组的 `url` 是 `http://www.gstatic.com/generate_204`，国内直连 TCP 超时 | 换成 `http://cp.cloudflare.com/generate_204`（国内实测 204/614ms），url-test 加 `tolerance=50` / `timeout=5000` / `max-failed-times=3`，`PROXY` 组加 `DIRECT` |
| 400 节点几乎从不更新、固定几个能用 | `CN_ALIVE_BOOST=1000` 把 NAS 验证节点永久钉在截断线上；run 预算 105min > cron 2h 导致排队/取消；`has_nas_verdict` 恒真使 Phase A 从不执行、probe_queue 冻结 8 天；mihomo 下载 403 后回退重发旧文件 | `cn_verified` 改排序层级；测速预算收紧；cron `*/30` + `timeout-minutes: 40`；`FORCE_COLLECT` + 陈旧 verdict 强制采集；纯 Python TCP 降级，二进制拿不到也产出新订阅 |
| 去重后仍重复、固定 73 个 | 旧去重按含 UUID 的指纹判重，同一 CDN 入口 12 个不同 UUID 全部通过 | `client_endpoint_key(host, port)` 按客户端实际列出去重，候选期每入口 1 副本、发布期 1 个 |
| 生成配置含本地回环节点 | 产物里 vmess `[NoMoreWalls#001] 防范境外势力渗透` 指向 `127.0.0.53:80`（uuid `88888888-...`），溯源为 `yy1588133/proxy-pool/main/clash.yaml` 中 10 处 `server: 127.0.0.53`；旧 `normalize_proxy` 只判 `if not server` | 新增 `is_non_routable_server()`（`ipaddress` 覆盖 127.0.0.0/8、`::1`、`0.0.0.0`、169.254、组播、保留段、私网段、localhost 字面量），在 4 个入口拦截：`normalize_proxy` 主采集、`load_existing_metrics` 回退重发旁路、`load_candidates` Phase B 旁路、`publish` 最后兜底；CI `Validate` 与 `tests/verify_config.py` 加回归守卫，`tests/test_loopback.py` 覆盖 21 拒绝 + 8 放行 + 3 条旁路 |

注意：`sni: 127.0.0.1` 之类字段**不过滤**——SNI 是 TLS 握手声明的主机名，由服务端解释，
拨号目标仍是 `server` 的公网域名。域名形态的 `server` 也放行，不在采集期做 DNS 解析，
交由下游 TCP/egress 门判定。

环境变量完整清单见 [documentation/reference/env-vars.md](documentation/reference/env-vars.md)。

## 产物

- `output/clash.yaml` —— 生成器输出
- `docs/clash.yaml` —— GitHub Pages 订阅（必须与 `output/clash.yaml` 字节一致，workflow 里 `cmp` 强校验）
- `data/node_registry.json` —— 节点台账（verified / blacklist，带 TTL）
- `data/candidates.json`、`data/probe_queue.json`、`data/cn_alive.json` —— 阶段状态
- `out/` —— 本地诊断报告与草稿（gitignored，不发布）

各文件 schema 见 [documentation/reference/data-files.md](documentation/reference/data-files.md)。

## 校验

CI 的 `Validate Clash Verge subscription` 会检查：必需组与规则存在、`output` 与 `docs`
字节一致、Pages 返回 200、所有健康检查 URL 国内可达、**发布节点无重复 endpoint**、
`PROXY` 组保留 `DIRECT`、无非路由地址。本地用 `python tests\verify_config.py` 复刻同一套守卫。

请仅在遵守当地法律法规和相关服务条款的前提下使用。
