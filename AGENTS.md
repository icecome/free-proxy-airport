# AGENTS.md — free-proxy-airport
> 免费代理聚合订阅：云端采集测速 + NAS 国内拨测，产出 Clash/mihomo 配置。
> 优先级：用户当前指令 > 本文件 > `documentation/` 其他文档 > `README.md`。
> 目录职责与新增文件放哪，唯一裁决依据是 [documentation/structure.md](documentation/structure.md)。

本文只写**代理需要但人不需要**的细节。项目背景与用法见 [README.md](README.md)；
模块清单见 [documentation/reference/modules.md](documentation/reference/modules.md)。

---

## 1. 项目速览

| 项 | 值 |
|---|---|
| 语言 | Python 3.11+（实测 3.13.7），无类型检查器、无 linter、无打包 |
| 依赖 | `requests`、`pyyaml`；`rescue_pass.py` 另需 `PySocks` |
| 外部引擎 | mihomo 1.19.34（`.tools/mihomo/`）、sing-box 1.14.4（`.tools/sing-box-1.14.4/`） |
| CI | GitHub Actions，每日 03:00 UTC，`timeout-minutes: 90` |
| 发布产物 | `output/clash.yaml` + `docs/clash.yaml`，两份必须字节一致 |
| 订阅 URL | `https://icecome.github.io/free-proxy-airport/clash.yaml` |

### 反直觉事实（先记住再动手）

1. **`docs/` 不是文档目录，是 GitHub Pages 源。** 站点根即 `docs/`：
   `/clash.yaml` → 200，`/docs/clash.yaml` → 404。文档一律进 `documentation/`。
   `docs/clash.yaml` 的路径是**已发布的对外契约**，改名等于断掉所有客户端订阅。
2. **`out/` 被 `.gitignore` 忽略。** 放在里面的东西 clone 后不存在。三个回归测试
   曾经在里面，等于从未进过版本库。
3. **本地跑 `generator.py` 采集必定 0 源成功。** `raw.githubusercontent.com` 在国内被 RST，
   而 `generator.py` 没有镜像回退（只有 `scripts/_probe_lib.py` 有）。采集只能在 CI 跑。
4. **mihomo 与 sing-box 的判定不一致是常态。** 55 节点交叉复核：mihomo 15 可用、
   sing-box 16 可用，只有 12 个一致。黑名单一条是 7 天，单引擎判定不足以定罪。
5. **`data/cn_alive.json` 已过期 13 天以上是正常的。** 超过 6 小时就会被
   `load_cn_alive()` 忽略并降级为排序提示，不会阻断发布。

---

## 2. 目录结构

```
airport/       业务库 + 流水线入口      scripts/     源发现（人工、低频）
tests/         回归测试（纳入版本控制）  documentation/  人类文档
data/          提交的运行时状态          output/ docs/  订阅产物（两份字节一致）
nas/           NAS 部署脚本（POSIX sh）  out/ .tools/ _bak-*/   gitignored
```

| 目录 | 允许存放 | 禁止存放 |
|---|---|---|
| `airport/` | 可执行模块、库、`paths.py` | 数据文件、产物、一次性脚本 |
| `scripts/` | 人工调用的发现/汇总脚本 | 日常流水线代码 |
| `tests/` | 无网络依赖的测试 | 需要真实节点的集成测试 |
| `documentation/` | 设计/开发/运维/参考四类 md | 被程序读取的配置 |
| `data/` | 见 structure.md §4 白名单 | 调试产物、日志 |
| `docs/` | 仅 `clash.yaml`、`.nojekyll` | **任何文档** |
| `out/` | 报告、调试转储 | 任何项目依赖的东西 |

完整职责表与新增文件的决策树在 [structure.md](documentation/structure.md) §2 §3，本文不复读。

---

## 3. 常用命令（已核实 2026-11-24）

```pwsh
# 回归测试：不需要网络、不需要引擎，干净 clone 即可跑
python tests\test_dedup.py           # 去重 + 排序层级
python tests\test_loopback.py        # 非路由地址四入口拦截
python tests\verify_config.py        # 复刻 CI 的订阅校验

# 干跑（只打印，不写任何文件）
python -m airport.publish_verified
python -m airport.apply_verdicts

# 本地体检全链路（需网络 + 引擎二进制，400 节点约 5 分钟）
python -m airport.local_verify --workers 8 --batch 50 --no-registry
python -m airport.rescue_pass --workers 16
python -m airport.apply_verdicts --apply --extra out\rescue_report.json --extra-authoritative out\confirm_mihomo.json
python -m airport.publish_verified --apply

# 快速 TCP 可达性（不需要引擎）
python -m airport.cn_alive_check --json out\tcp_report.json

# 采集 + 发布（本地通常跑不通，见反直觉事实 3）
$env:FREE_PROXY_AIRPORT_FORCE_COLLECT = '1'
python -m airport.generator
```

**没有** lint / typecheck / formatter 脚本，不要臆造。

所有模块同时支持 `python -m airport.<模块>` 与 `python airport\<模块>.py`；
路径由 `airport/paths.py` 锚定仓库根，从任何工作目录跑都指向同一处。

---

## 4. 架构与分层铁律

```
paths.py  ←  所有模块（唯一叶子）

generator.py        → node_registry, paths
verify_nodes.py     → node_registry, paths
local_verify.py     → node_registry, paths
rescue_pass.py      → node_registry, paths, verify_nodes
apply_verdicts.py   → node_registry, paths
publish_verified.py → generator, node_registry, paths
cn_alive_check.py   → （无内部依赖）
nas_probe.py        → paths
```

1. **禁止循环 import。** `rescue_pass` 可以 import `verify_nodes`，反向不行。
2. **`airport/` 不得 import `scripts/` 或 `tests/`。** 依赖只能反向指。
3. **路径只能来自 `paths.py`。** 新增 `Path("data/...")` 字面量即违规——工作目录一变就写错地方。
4. **新增可执行模块** = 建文件 + 抄引导块 + 在 `__init__.py` 加一行阶段索引。

### 改动的连带影响

| 改动 | 连带影响 |
|---|---|
| `paths.py` 任何常量 | 所有模块 + `tests/` + `nas/*.sh` |
| `node_registry.node_key()` | 台账键格式变化 ⇒ 已有 verified/blacklist 全部失配 |
| `generator.publish()` 签名 | `publish_verified.py` + `tests/test_loopback.py` |
| `generator.build_config()` | `publish_verified.py` |
| `verify_nodes.build_singbox_config()` | `rescue_pass.py` |
| `local_verify` / `verify_nodes` 报告字段 | `apply_verdicts.py`（按 `key`/`ok`/`stage` 读） |

---

## 5. 编码规范

| 实体 | 规范 | 示例 |
|---|---|---|
| 模块 | `snake_case.py`，动词开头表示入口 | `verify_nodes.py`、`publish_verified.py` |
| 测试 | `test_<对象>.py` / `verify_<对象>.py` | `test_dedup.py` |
| 私有模块 | 下划线前缀 | `scripts/_probe_lib.py` |
| 数据文件 | `snake_case.json` | `probe_queue.json` |
| 文档 | kebab-case `.md` | `structure-decisions.md` |

路径写法（唯一正确形式）：

```python
from airport import paths

queue_path = paths.PROBE_QUEUE_FILE       # ✅
queue_path = Path("data/probe_queue.json")  # ❌ 依赖 CWD
```

测试断言（唯一正确形式）：

```python
expect = min(12, g.DEDUP_ENDPOINT_CANDIDATES) + len(other_hosts)   # ✅ 跟随常量
assert len(out) == expect
assert len(out) == 8                                                # ❌ 常量一改就假绿
```

---

## 6. 硬性规则

以下规则来自本项目实际踩过的问题，遇到同类直接照做：

**R1 任何节点判定都必须有 TTL，不得永久化。**
`verified` 24 小时、`failed` IP 7 天 / 域名 3 天，由 `node_registry.prune()` 在每次保存前清理。
依据：免费节点今天能用不代表明天能用，永久白名单会冻结发布集（2026-11-10 的 400 节点冻结事故）。

**R2 判死节点必须过二次意见才能进黑名单。**
先 `rescue_pass.py`（TCP×3 + sing-box 独立隧道），再由 `apply_verdicts.py` 按引擎优先级落库。
依据：55 节点交叉复核中 mihomo 与 sing-box 双向各有 3 个「对方说死、自己说活」。
适用条件：任何写 `record_failed()` 的路径。

**R3 发布物是 mihomo 配置，所以 mihomo 的判定是判决。**
sing-box 说 OK 而 mihomo 说不行的节点，仍记 failed，但 `stage` 标 `engine-mismatch`。
依据：`docs/clash.yaml` 被 Clash/mihomo 类客户端加载，其他引擎的判定只是证据。

**R4 凭据级去重只对 vmess/vless/trojan/ss 成立。**
socks5/http 一律按 endpoint 认身份。
依据：socks5 的 `password` 常是公共默认值（实测 6 个节点同为 `Og==`），
按凭据折叠会把互不相干的家宽出口删掉。

**R5 写 `docs/clash.yaml` 必须与 `output/clash.yaml` 字节一致。**
依据：CI 用 `cmp --silent` 强校验，不一致直接红；且 `docs/` 是 Pages 源，只认这一份。

**R6 破坏性覆盖前先备份到 `_bak-YYYYMMDD/`。**
`publish_verified.py` 已内置（覆盖前 `copy2` 到备份目录）。
依据：根目录不是 git 仓库（本条继承工作区规则 R4）。

**R7 命令进文档前必须真跑过。**
本文第 3 节每条都标了核实日期；跑不通的命令要么修好要么删掉。

---

## 7. 禁止事项

| 禁止项 | 原因 |
|---|---|
| 在 `docs/` 放文档 | 会被 GitHub Pages 公开托管，且挤占订阅产物的位置 |
| 把测试放进 `out/` | `out/` 被 gitignore，clone 后不存在，等于测试从未存在 |
| 在 `airport/` 里写 `Path("data/...")` 字面量 | 工作目录一变就静默写到错误位置 |
| 从 `airport/` import `scripts/` | 依赖方向反了，`scripts/` 是一次性工具 |
| 手工编辑 `output/clash.yaml` 或 `docs/clash.yaml` | 下次生成即被覆盖，且两份会不一致 |
| 给 `node_registry` 加永不过期的条目 | 会让发布集冻结，见 R1 |
| 把 `.tools/` 或 `.history/` 提交进 git | 合计约 137 MB 二进制/历史快照 |
| 在仓库根新建临时 `.py` 脚本 | 那是 `out/` 的事；根目录只放项目文件 |

---

## 8. 已知坑位

| 坑 | 处置 |
|---|---|
| `raw.githubusercontent.com` 在国内被 RST，本地采集 0 源成功 | `generator.py` 无镜像回退；本地改走 `local_verify` 路径，或让 CI 跑 |
| mihomo 1.19.34 报 `global-client-fingerprint is removed` | 该顶层键已被删除，是**警告不是致命错**，配置仍加载。41 条节点缺 `client-fingerprint`，但实测补上并不救活它们 ⇒ 不是主因。待修：从 `build_config()` 去掉该键 |
| mihomo delay 接口报 503 / `unexpected status: 404 Not Found` / x509 IP SAN 错误，而 sing-box 能通 | REALITY-over-WS 的引擎差异。记为 `engine-mismatch`，不拉黑 |
| 一份坏节点会让整个 mihomo 实例起不来 | `local_verify.run_instance()` 已做二分隔离：批量失败就拆成两半重跑，最后落到单节点 |
| `subprocess.Popen` 报 `PermissionError: [WinError 5]` | 引擎路径为空串时 Windows 会拒绝。`find_engine()` 必须先判 `cand.is_file()`，不能只判 `exists()` |
| 同一实例内节点重名导致 `Parse config error: duplicate name` | 构造测试变体时必须给每个变体唯一 `name`，不能复用节点原名 |
| 抓取被本机 nekobox 系统代理劫持，5 秒后返回假 502 | 所有 `requests.Session` 必须 `trust_env=False`。`local_verify`、`rescue_pass`、`_probe_lib` 均已处理 |
| `ghproxy.net` 对 404 响应慢达 19–32 秒，`requests` 的 read timeout 管不住 | 用 `future.result(timeout=...)` 做硬性墙钟上限（`_probe_lib.http_get_hard()`） |
| `ghproxy.net` 64 并发时静默返回 503 | 并发 ≤16 + 每请求重试 2 次（`scripts/_probe_lib.py`） |
| `sni: 127.0.0.1` 之类字段不该被过滤 | SNI 是 TLS 握手声明的主机名，拨号目标仍是 `server`；只有 `server` 本身非路由才拦 |
| Windows 下用 `str.write_text` 写含 CJK 的 YAML 可能双编码 | `write_config()` 用 `write_bytes` 显式 UTF-8 |
| 台账备份文件名带秒级时间戳，同一秒内两次 apply 会互相覆盖 | 备份前先确认目标不存在，或串行执行（当前流程是串行的） |

---

## 9. 文档体系入口

| 文档 | 回答什么 | 冲突时 |
|---|---|---|
| [documentation/structure.md](documentation/structure.md) | 文件放哪、目录职责、开发约束 | **以它为准** |
| [documentation/README.md](documentation/README.md) | 文档索引（含 design/dev/ops/reference 全部条目） | — |
| [documentation/reference/modules.md](documentation/reference/modules.md) | 模块做什么、被谁调用、改动的连带影响 | 与代码冲突以代码为准 |
| [documentation/reference/env-vars.md](documentation/reference/env-vars.md) | 环境变量清单 | 与代码冲突以代码为准 |
| [documentation/reference/data-files.md](documentation/reference/data-files.md) | `data/` 与产物的 JSON schema | 与代码冲突以代码为准 |
| [documentation/ops/runbook.md](documentation/ops/runbook.md) | 怎么跑、怎么修、怎么回滚 | — |

工作区级约定（多项目共用目录、会话归位规则）在上一级 `../AGENTS.md`，本文不复读。

---

## 10. 维护规则

| 触发事件 | 回写内容 |
|---|---|
| 新增/改名/删除顶层目录 | `documentation/structure.md` §1 §2 + `README.md` + 本文 §2 |
| 新增 `data/` 状态文件 | `airport/paths.py` + `structure.md` §4 + `reference/data-files.md` |
| 新增 `airport/` 模块 | `__init__.py` 阶段索引 + `reference/modules.md` + 本文 §4 |
| 踩到新坑且已定位 | 本文 §8 |
| 命令跑通或失效 | 本文 §3（带核实日期） |
| 架构决策被推翻 | `design/structure-decisions.md` 追加一条，写明原状/问题/决定/依据 |

本文上限 250 行。超过时先拆到 `documentation/`：规则条数不减，只把细节挪走、
留一句话规则 + 链接。
