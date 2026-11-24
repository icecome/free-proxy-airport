# 数据文件字典

`data/` 下每个 JSON 的 schema、生产者、消费者与生命周期。
新增状态文件必须同步更新本文与 [../structure.md](../structure.md) §4。

---

## node_registry.json

节点台账，全项目最重要的状态文件。**时间受限**：没有永久判定。

```jsonc
{
  "verified": {
    "<node_key>": {
      "server": "162.35.101.177",
      "port": 443,
      "type": "vless",
      "at": 1795355096,          // 判定时间（unix）
      "detail": "OK 1694ms 3.38Mbps exit=US",
      "proxy": { /* 完整节点配置，发布时无需回源匹配 */ }
    }
  },
  "failed": {
    "<node_key>": {
      "server": "1.2.3.4",
      "port": 443,
      "type": "vless",
      "stage": "TCP",            // 死在哪一阶段
      "at": 1795355096,
      "expires_at": 1795960000   // 黑名单到期时间
    }
  },
  "updated_at": 1795355096
}
```

| 属性 | 规则 |
|---|---|
| `node_key` | `sha256(type\|server\|port\|cred)[:20]`，cred 取 `uuid` 或 `password` |
| verified TTL | 24 小时（`VERIFIED_TTL`） |
| blacklist TTL | IP 7 天，域名 3 天（`FAILED_TTL_IP` / `FAILED_TTL_DOMAIN`） |
| 通过即解黑 | `record_verified()` 会 `failed.pop(同 key)` |
| 过期清理 | `prune()` 在每次保存前调用 |
| 生产者 | `apply_verdicts.py`、`local_verify.py`、`verify_nodes.py` |
| 消费者 | `generator.py`（过滤+注入）、`publish_verified.py`、所有验证器 |

`failed` 条目**不存** `proxy` 负载——黑名单只需要身份，不需要配置。

---

## candidates.json

Phase A 的阶段性候选，供 Phase B 读取，避免每次重采集。

```jsonc
{
  "saved_at": 1795434799,
  "total_nodes": 7265,
  "sources_ok": 15,
  "candidates": [
    {
      "proxy": { "name": "...", "server": "...", "port": 443, "type": "vless", ... },
      "latency": 110,
      "region": "US",
      "speed_mbps": 0.0,
      "health_score": 2.133
    }
  ]
}
```

TTL 26 小时（`CANDIDATES_TTL_SECONDS`）。生产者与消费者都是 `generator.py`。

---

## probe_queue.json

下发给 NAS 的待测队列，上限 `PROBE_QUEUE_LIMIT`（默认 500）。

```jsonc
{
  "queued_at": "2026-11-24T03:43:33+00:00",
  "targets": [
    { "fp": "596db1c8155bda14", "name": "...", "type": "vless",
      "server": "211.34.73.83", "port": 12251 }
  ]
}
```

`fp` = `proxy_fingerprint(proxy)[:16]`，是云端与 NAS 之间的关联键。
生产 `generator.py`，消费 `nas_probe.py`。

---

## cn_alive.json

NAS 的国内视角 TCP 结果。

```jsonc
{
  "probed_at": 1794325711,
  "vantage": "nas-cn",
  "total": 500,
  "alive_count": 441,
  "alive": [
    { "fp": "596db1c8155bda14", "server": "211.34.73.83", "port": 12251,
      "alive": true, "tcp_ms": 1098 }
  ]
}
```

**6 小时后降级**：`generator.load_cn_alive()` 超过 21600 秒直接忽略，
判定从"准入门"退化为"排序提示"。

---

## source_stats.json

按源 URL 记录健康度，驱动优先级升降。

```jsonc
{
  "https://raw.githubusercontent.com/...": {
    "history": [63, 62, 62, 62, 63],  // 最近几轮的节点数
    "dead_streak": 0,                  // 连续失败次数
    "last_count": 63,
    "updated_at": 1795434783,
    "priority": 1.0                    // 乘进 health_score，越低越靠后
  }
}
```

连续失败 3 次（`SOURCE_DEAD_STREAK_LIMIT`）降权；30 天无记录（`SOURCE_STATS_TTL_DAYS`）清理。

---

## 源发现的中间结果

这些文件由 `scripts/` 生产，供人工审阅，不参与自动发布。

| 文件 | 生产者 | 结构要点 |
|---|---|---|
| `discovered_github.json` | `discover_github.py` | `repos_scanned`、`hits[]`（仓库 + 命中的订阅路径） |
| `discovered_telegram.json` | `discover_telegram.py` | `channels_with_proxy_data`、`usable[]`、`sub_host_health` |
| `harvested_telegram.json` | `harvest_telegram.py` | `channels`、`subscriptions`、`links`、`summary` |
| `checked_github.json` | `check_discovered.py` | `checked`、`alive`、`results[]`（含 TCP 抽样率） |
| `candidates_report.json` | `report_candidates.py` | `rows[]`、`recommended[]` |

---

## output/clash.yaml 与 docs/clash.yaml

同一份内容的两份拷贝：

- `output/clash.yaml` —— `generator.publish()` 写出，被 git 跟踪
- `docs/clash.yaml` —— GitHub Pages 发布源，CI 用 `cmp` 强制逐字节一致

两者必须字节相同。任何只改一个的操作都会被 CI 的 `Validate Clash Verge subscription` 拦下。

结构要点：`proxies[]`（400 上限，实际按可用数）、9 个 `proxy-groups`
（`AUTO-FAST` / `VERIFIED` / `UNVERIFIED` / `HK-POOL` / `JP-POOL` / `US-POOL` /
`AI-POOL` / `FALLBACK` / `PROXY`）、`rules[]`（含 4 条 AI 域名路由与 `GEOIP,CN,DIRECT`）。

节点名带前缀标记：`✅ ` = 本地/云端验证过，`⚠️ ` = 未验证的新候选。
