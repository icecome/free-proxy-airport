# 运维 Runbook

日常操作与故障处置。命令一律从仓库根执行；模块都支持 `python -m airport.<模块>`。

---

## 1. 例行任务

### 1.1 云端自动发布（无需人工）

CI `.github/workflows/update.yml` 每天 03:00 UTC 跑一次：

```
采集 → 测速 → 合并 NAS 判定 → 发布 → 提交 output/ 与 docs/ → 校验
```

失败会在 Actions 页面报红。手动触发：仓库 Actions 页 → `AI Self-Healing Proxy v7` → `Run workflow`。

### 1.2 本地体检已发布列表

当客户端大面积连不上，或发布列表长期没换过节点时执行：

```pwsh
# 第 1 步：全量四阶段体检（400 节点约 3 分钟，8 实例并发）
python -m airport.local_verify --workers 8 --batch 50 --no-registry

# 第 2 步：对判死节点做二次意见（TCP×3 + sing-box 独立隧道）
python -m airport.rescue_pass --workers 16

# 第 3 步：结论落库（mihomo 判决为准，sing-box 只作证据）
python -m airport.apply_verdicts --apply `
  --extra out\rescue_report.json --extra-authoritative out\confirm_mihomo.json

# 第 4 步：只用 verified 条目重建订阅
python -m airport.publish_verified --apply
```

每步的产物：

| 步骤 | 产物 | 说明 |
|---|---|---|
| 1 | `out/verify_report.json`、`out/verify_summary.txt` | 每节点四阶段判定 |
| 2 | `out/rescue_report.json` | 二次意见结果 |
| 3 | `data/node_registry.json` | verified 24h / blacklist IP 7d 域 3d |
| 4 | `output/clash.yaml` + `docs/clash.yaml` | 两份必须字节一致 |

第 4 步会先备份到 `_bak-YYYYMMDD/`，再覆盖。

### 1.3 只做 TCP 可达性快查

不需要引擎二进制，几秒出结果：

```pwsh
python -m airport.cn_alive_check --json out\tcp_report.json
python -m airport.cn_alive_check -o out\alive.yaml
```

### 1.4 源发现（人工、低频）

```pwsh
python scripts\discover_github.py --pages 3 --max-repos 400 --workers 16 --repo-budget 14
python scripts\check_discovered.py --in data\discovered_github.json --out data\checked_github.json
python scripts\report_candidates.py            # 只看
python scripts\report_candidates.py --append   # 写回 sources.txt
```

---

## 2. 故障处置

### 2.1 客户端全部显示「测试连接失败」

**先查健康检查 URL**，这是 2026-11-10 那次全红的根因：

```pwsh
python tests\verify_config.py
```

它会检查所有 `url-test` / `fallback` 组的 `url` 是否为国内可达的
`cp.cloudflare.com/generate_204`。若报 `CN-blackholed health URL`，说明有人改回了
`gstatic` / `google.com`。

**再查 `output` 与 `docs` 是否一致**：

```pwsh
cmp output/clash.yaml docs/clash.yaml
```

不一致说明发布只写了一半，重跑 `python -m airport.publish_verified --apply`。

### 2.2 节点大面积失效

按 §1.2 走完整四步。典型结果（2026-11-24 实测 400 条）：

```
TCP 可达 150/400 (37%)
mihomo 隧道可用 15
rescue 再捞回 2
候选池新增可用 14
最终发布 25
```

若 `publish_verified` 报 `nothing verified`，说明台账里没有新鲜条目——
先确认 `local_verify` 真的跑通了（引擎二进制在不在）。

### 2.3 采集全失败（0 源成功）

```
[WARN] ALL sources failed; check sources.txt URLs or your network egress
```

本地跑 `generator.py` 时最常见：`raw.githubusercontent.com` 在国内被 RST。
`generator.py` **没有**镜像回退（只有 `scripts/_probe_lib.py` 有）。

处置：

- 云端跑（CI runner 在国外，不受影响）
- 或本地走本地体检路径（§1.2），跳过采集
- 或临时给 `FREE_PROXY_AIRPORT_SOURCES` 指向镜像 URL

### 2.4 mihomo 报 `global-client-fingerprint is removed`

```
level=error msg="The `global-client-fingerprint` configuration is removed,
please set `client-fingerprint` directly on the proxy instead"
```

mihomo 1.19.34 删除了该顶层配置项。这是**警告不是致命错**，配置仍能加载。
影响：没自带 `client-fingerprint` 的 TLS 节点可能握手失败（实测 41 条节点受影响，
但补上该字段并未救活它们，说明不是主因）。

待修项：把该键从 `generator.build_config()` 去掉，改为逐节点写入。

### 2.5 `publish_verified` 出来的节点比台账少

正常现象。凭据级去重会合并"同 UUID 多入口"的节点：

```
17 verified → 12 发布（4 组合并）
```

`--min-speed 2.0` 会进一步减少。想全量发布就不加参数。

### 2.6 引擎二进制丢失

`.tools/` 被 gitignore，新 clone 后不存在。

```pwsh
# mihomo：local_verify 首次运行会尝试自动下载（走 ghproxy.net 镜像）
python -m airport.local_verify --limit 1

# 或手工放到指定位置
#   .tools/mihomo/mihomo.exe
#   .tools/sing-box-1.14.4/sing-box.exe
```

`rescue_pass.py` 需要 sing-box；`local_verify.py` 需要 mihomo。
`cn_alive_check.py` 两者都不需要。

---

## 3. 回滚

### 3.1 回滚订阅

```pwsh
# 看有哪些快照
ls _bak-*/
# 直接覆盖
copy _bak-20261124\docs-clash-114333.yaml docs\clash.yaml
copy _bak-20261124\output-clash-114333.yaml output\clash.yaml
```

### 3.2 回滚台账

```pwsh
copy _bak-20261124\registry-snapshots\_node_registry-20261124-114233.json data\node_registry.json
```

### 3.3 回滚目录结构

结构重构前的状态打了 git tag：

```pwsh
git tag -l                          # 找 pre-restructure-20261124
git diff pre-restructure-20261124 --stat
git checkout pre-restructure-20261124 -- .   # 谨慎：会覆盖当前工作区
```

---

## 4. NAS 拨测

容器内循环，每 900 秒跑一次（`PROBE_INTERVAL`）：

```sh
# nas/setup.sh  首次初始化：clone 仓库、跑一次、进入循环
# nas/loop.sh   循环体：cd /workspace && python3 airport/nas_probe.py
```

`nas_probe.py` 的行为：

1. `git pull` 领任务（最多重试 3 次，指数退避）
2. 读 `data/probe_queue.json`，队列签名未变则直接退出（省资源）
3. 对每个 `server:port` 发 TCP 连接（48 并发，4 秒超时，2 次重试）
4. 写 `data/cn_alive.json` → `git commit` + `git push`

诊断：`nas/diag.sh` 输出目录、队列、前 15 个节点的实拨结果到 `data/diag-out.txt`。

---

## 5. 检查清单

发布前：

- [ ] `python tests\test_dedup.py` 通过
- [ ] `python tests\test_loopback.py` 通过
- [ ] `python tests\verify_config.py` 通过
- [ ] `cmp output/clash.yaml docs/clash.yaml` 无差异
- [ ] 台账 verified 条目非空
