# 模块清单与调用关系

按流水线阶段列出 `airport/` 与 `scripts/` 的每个模块：它做什么、什么时候跑、被谁调用。
目录职责与存放规则见 [../structure.md](../structure.md)。

---

## 1. 流水线全景

```
                     ┌─────────────────────────────────────────┐
  sources.txt ──────►│ generator.py  Phase A: 采集 + 测速       │
                     │   → data/candidates.json                 │
                     │   → data/probe_queue.json                │
                     └───────────────┬─────────────────────────┘
                                     │ 队列下发
                                     ▼
                     ┌─────────────────────────────────────────┐
  NAS 容器 ─────────►│ nas_probe.py  TCP 拨测（国内视角）        │
                     │   → data/cn_alive.json ──► git push      │
                     └───────────────┬─────────────────────────┘
                                     │ 国内判定回传
                                     ▼
                     ┌─────────────────────────────────────────┐
                     │ generator.py  Phase B: 合并判定 + 发布    │
                     │   → output/clash.yaml → docs/clash.yaml  │
                     └─────────────────────────────────────────┘

  本地体检（与云端并行的另一条路，用于修复"已发布但大面积失效"）：
  docs/clash.yaml ─► local_verify.py ─► rescue_pass.py
                          │                  │
                          └──────┬───────────┘
                                 ▼
                        apply_verdicts.py ─► data/node_registry.json
                                 │
                                 ▼
                        publish_verified.py ─► output/clash.yaml + docs/clash.yaml
```

云端路径与本地体检路径共用同一个台账（`node_registry.json`），
所以本地验证过的节点会被云端发布尊重，反之亦然。

---

## 2. `airport/` 模块

| 模块 | 行数 | 做什么 | 何时跑 | 依赖 |
|---|---|---|---|---|
| `paths.py` | 55 | 所有文件系统路径的唯一定义处 | 被所有模块 import | 无 |
| `node_registry.py` | 137 | 时间受限的 verified/blacklist 台账，键为 `type\|server\|port\|cred` | 被读写 | `paths` |
| `generator.py` | 2083 | 采集源 → 解析 → 去重 → 测速 → 合并 NAS 判定 → 发布 | CI cron（每日 03:00）、手动 | `paths`、`node_registry` |
| `cn_alive_check.py` | 146 | 对订阅里每个 `server:port` 发 TCP 连接，判"我的宽带能否触达" | 手动 | 无 |
| `verify_nodes.py` | 434 | sing-box 起单节点 SOCKS 隧道 → 真实出口 → 判可用 | 手动、被 `rescue_pass` 复用 | `paths`、`node_registry` |
| `local_verify.py` | 507 | mihomo 批量四阶段体检（TCP → delay → egress → 测速） | 手动 | `paths`、`node_registry` |
| `rescue_pass.py` | 275 | 对首轮判死节点做二次意见（TCP×3 + sing-box） | 手动 | `paths`、`node_registry`、`verify_nodes` |
| `apply_verdicts.py` | 141 | 验证报告 → 台账，处理引擎优先级 | 手动 | `paths`、`node_registry` |
| `publish_verified.py` | 187 | 台账 verified 条目 → 订阅，凭据级去重 | 手动 | `paths`、`node_registry`、`generator` |
| `nas_probe.py` | 186 | NAS 容器里的拨测循环，结果写回仓库 | 容器内定时 | `paths` |

### 2.1 谁调用谁

```
publish_verified  ──► generator（复用 build_config / publish / validate_config）
rescue_pass       ──► verify_nodes（复用 build_singbox_config）
其余              ──► paths、node_registry
```

**没有循环依赖。** 修改 `verify_nodes.build_singbox_config` 的签名会同时影响 `rescue_pass`；
修改 `generator.publish` 的签名会同时影响 `publish_verified` 与 `tests/`。

---

## 3. `scripts/` 模块（源发现）

人工触发，不进日常流水线。产出 `data/discovered_*.json` 等中间结果，供人审阅后再写回 `sources.txt`。

| 模块 | 行数 | 做什么 | 产出 |
|---|---|---|---|
| `_probe_lib.py` | 206 | 共用抓取工具：镜像回退、硬性墙钟超时、base64 解码、节点计数、并发包装 | — |
| `discover_github.py` | 252 | GitHub Search API 按 15 组关键字翻页，对每个仓库并发探测 14 条订阅路径 | `data/discovered_github.json` |
| `discover_telegram.py` | 217 | 找 Telegram 公开频道，走 `t.me/s/<handle>` 无登录预览 | `data/discovered_telegram.json` |
| `harvest_telegram.py` | 213 | 对已确认频道翻更多页，测订阅 URL 与单体节点 TCP | `data/harvested_telegram.json` |
| `check_discovered.py` | 161 | 对发现结果做存活复检（能否取到、节点数、TCP 抽样率） | `data/checked_github.json` |
| `report_candidates.py` | 122 | 汇总排序，输出推荐清单，可 `--append` 写回 `sources.txt` | `data/candidates_report.json` |

依赖：除 `report_candidates.py` 只依赖 `airport.paths` 外，其余都依赖 `scripts/_probe_lib.py`；
`harvest_telegram.py` 还 import `discover_telegram.py`（复用频道探测逻辑）。

---

## 4. `tests/` 模块

不依赖网络，可在干净 clone 上跑通。

| 模块 | 行数 | 覆盖什么 | 变异点 |
|---|---|---|---|
| `test_dedup.py` | 84 | 端点键归一化、候选去重、发布去重（协议无关）、排序层级、健康检查 URL | 每 endpoint 副本上限（`DEDUP_ENDPOINT_CANDIDATES`） |
| `test_loopback.py` | 86 | 非路由地址在 4 个入口被拦截：主采集、回退重发、Phase B 旁路、publish 兜底 | 每个入口单独构造脏数据 |
| `verify_config.py` | 67 | 复刻 CI 的订阅校验：组、规则、健康 URL、重复端点、回环地址 | — |

`test_dedup.py` 的期望值跟随 `generator.DEDUP_ENDPOINT_CANDIDATES` 实时计算，
不再硬编码"每 endpoint 3 副本"——那个默认值已在 2026-11-22 改为 1。

---

## 5. 调用关系速查

想知道"改了 X 会影响谁"，查这张表：

| 改动 | 连带影响 |
|---|---|
| `paths.py` 任何常量 | 所有模块 + `tests/` + `nas/*.sh` |
| `node_registry.node_key()` | 台账键格式变化 ⇒ 已有 verified/blacklist 全部失配 |
| `generator.client_endpoint_key()` | 去重行为 + `tests/test_dedup.py` |
| `generator.publish()` 签名 | `publish_verified.py` + `tests/test_loopback.py` |
| `generator.build_config()` | `publish_verified.py` |
| `verify_nodes.build_singbox_config()` | `rescue_pass.py` |
| `local_verify` 的报告字段 | `apply_verdicts.py`（它按 `key`/`ok`/`stage` 读取） |
| `verify_nodes` 的报告字段 | `apply_verdicts.py`（同上） |
| `data/*.json` 的 schema | 生产者 + 消费者两侧，见 [../structure.md](../structure.md) §4 |
