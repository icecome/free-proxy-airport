# 开发约定

写代码前读这一页。目录与文件放哪见 [../structure.md](../structure.md)。

---

## 1. 本地环境

| 项 | 要求 |
|---|---|
| Python | 3.11+（实测 3.13.7）；用到 `X \| None` 这类语法，低于 3.10 会语法错 |
| 依赖 | `requests`、`pyyaml`；`rescue_pass.py` 额外需要 `PySocks`（sing-box SOCKS 隧道出口探测） |
| 引擎 | mihomo（`.tools/mihomo/`）、sing-box（`.tools/sing-box-*/`），首次运行由脚本自行下载或手工放置 |
| Shell | Windows 用 pwsh；`nas/*.sh` 是 POSIX sh，在容器内跑 |

```pwsh
python -m pip install requests pyyaml PySocks
```

---

## 2. 常用命令（均已核实）

```pwsh
# 回归测试（不需要网络，干净 clone 即可跑）
python tests\test_dedup.py          # 去重 + 排序层级
python tests\test_loopback.py       # 非路由地址四入口拦截
python tests\verify_config.py       # 复刻 CI 的订阅校验

# 本地体检全链路（需要网络 + 引擎二进制）
python -m airport.local_verify --workers 8 --batch 50 --no-registry
python -m airport.rescue_pass --workers 16
python -m airport.apply_verdicts --apply --extra out\rescue_report.json --extra-authoritative out\confirm_mihomo.json
python -m airport.publish_verified --apply

# 干跑（只打印，不写文件）
python -m airport.publish_verified
python -m airport.apply_verdicts

# 采集 + 发布（云端主流程，本地一般不需要跑）
$env:FREE_PROXY_AIRPORT_FORCE_COLLECT = '1'
python -m airport.generator
```

---

## 3. 写代码的硬性要求

### 3.1 路径

**禁止**在任何模块里写 `Path("data/...")`、`open("sources.txt")` 这类相对路径字面量。
一律从 `airport.paths` 取：

```python
# ❌ 工作目录一变就写错地方
queue_path = Path("data/probe_queue.json")

# ✅
from airport import paths
queue_path = paths.PROBE_QUEUE_FILE
```

需要新路径时：先在 `paths.py` 加常量，再在 `documentation/structure.md` §4 补一行。

### 3.2 入口引导

新增带 `main()` 的模块，必须支持两种调用形式。照抄现有引导块：

```python
from __future__ import annotations

# Allow `python airport/<module>.py` as well as `python -m airport.<module>`:
# the script form puts airport/ on sys.path, not the repo root.
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
```

### 3.3 内部 import

```python
from airport import paths               # ✅ 绝对导入
from airport import node_registry as registry
import node_registry                    # ❌ 依赖 sys.path 注入，脆弱
```

`scripts/` 内部允许 `from scripts._probe_lib import ...`，但 `airport/` 不得 import `scripts/`。

### 3.4 依赖方向

见 [../structure.md](../structure.md) §6.1。要改 `verify_nodes.build_singbox_config()`
或 `generator.publish()` 的签名，先查 [../reference/modules.md](../reference/modules.md) §5
的连带影响表。

---

## 4. 测试怎么写

放 `tests/`，文件名 `test_<对象>.py`。三个约束：

1. **不依赖网络。** 需要真实节点的验证属于手工流程，产物进 `out/`，不是测试。
2. **不依赖工作目录。** 用 `Path(__file__).resolve().parent.parent` 定位仓库根。
3. **断言给出期望值。** 参考 `test_dedup.py` 的做法——期望值从被测常量实时计算，
   避免常量改了测试假绿：

```python
# ❌ 硬编码，常量一改就失配（2026-11-24 实际踩到）
assert len(out) == 8

# ✅ 跟随常量
expect = min(12, g.DEDUP_ENDPOINT_CANDIDATES) + len(other_hosts)
assert len(out) == expect
```

---

## 5. 提交前检查

```pwsh
python tests\test_dedup.py
python tests\test_loopback.py
python tests\verify_config.py
```

- [ ] 三个测试全绿
- [ ] 没有在仓库根落临时脚本
- [ ] 没有把测试放进 `out/`
- [ ] 没有在 `docs/` 放文档
- [ ] 新增路径常量已进 `paths.py`
- [ ] 若动了 `output/clash.yaml`，确认 `docs/clash.yaml` 同步（CI 会 `cmp`）

---

## 6. 调试技巧

| 现象 | 先查什么 |
|---|---|
| 所有节点在客户端全红 | 组的 `url` 是否 `cp.cloudflare.com`；`output` 与 `docs` 是否一致 |
| mihomo 报 `global-client-fingerprint is removed` | 1.19.34 已删该配置项，节点需自带 `client-fingerprint`；这是警告不是致命错 |
| 某节点 mihomo 报 503 但 sing-box 能通 | REALITY-over-WS 的引擎差异，属正常，记为 `engine-mismatch` 而非拉黑 |
| `publish_verified` 出来的节点数少于台账 verified 数 | 凭据级去重合并了同 UUID 的多入口；加 `--min-speed` 会进一步减少 |
| 采集全失败（0 源成功） | `raw.githubusercontent.com` 在国内被 RST，本地跑需走 `ghproxy.net` 镜像（`scripts/_probe_lib.py` 已处理，`generator.py` 未处理） |
