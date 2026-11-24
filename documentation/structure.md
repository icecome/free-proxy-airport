# 项目结构与文件存放规范

本文是**目录职责与新增文件的唯一裁决依据**。任何"这个文件该放哪"的问题，
先查本文的两张表，再动手。与其他文档冲突时以本文为准。

---

## 1. 目录树

```
free-proxy-airport/
├── airport/                 业务库：采集 → 测速 → 台账 → 发布（唯一可执行代码来源）
│   ├── __init__.py          包说明与流水线阶段索引
│   ├── paths.py             所有文件系统路径的唯一真源（禁止别处再写 Path("data/...")）
│   ├── generator.py         采集 + 测速 + 发布订阅（云端主流程）
│   ├── node_registry.py     时间受限的 verified / blacklist 台账
│   ├── cn_alive_check.py    本地 TCP 可达性拨测
│   ├── verify_nodes.py      sing-box 隧道 + 真实出口验证
│   ├── local_verify.py      mihomo 批量健康度与测速
│   ├── rescue_pass.py       对判死节点的二次意见
│   ├── apply_verdicts.py    验证报告 → 台账
│   ├── publish_verified.py  台账 → 订阅
│   └── nas_probe.py         NAS 端国内视角拨测循环
├── scripts/                 源发现脚本：一次性、人工触发，不参与日常流水线
│   ├── README.md            发现脚本的用法与踩坑记录
│   ├── _probe_lib.py        发现脚本共用的抓取工具（私有，下划线前缀）
│   ├── discover_github.py   按关键字搜 GitHub 仓库并探测订阅路径
│   ├── discover_telegram.py 找 Telegram 公开频道
│   ├── harvest_telegram.py  对已确认频道翻页抓节点
│   ├── check_discovered.py  对发现结果做存活复检
│   └── report_candidates.py 汇总排序，可写回 sources.txt
├── tests/                   回归测试（纳入版本控制，不依赖网络）
│   ├── test_dedup.py        端点去重 + 发布去重 + 排序层级
│   ├── test_loopback.py     非路由地址在四个入口都被拦截
│   └── verify_config.py     复刻 CI 的订阅校验守卫
├── documentation/           人类文档（本文所在目录）
│   ├── README.md            文档索引
│   ├── structure.md         本文：目录职责与存放规范
│   ├── design/              为什么这样设计
│   ├── dev/                 怎么写、怎么测
│   ├── ops/                 怎么跑、怎么修
│   └── reference/           稳定的查表资料（模块清单、环境变量、数据字典）
├── data/                    提交进仓库的状态文件（见 §4）
├── output/                  生成物：订阅配置（提交，CI 逐字节比对）
├── docs/                    ⚠️ GitHub Pages 源目录，只放订阅与 .nojekyll
├── nas/                     NAS 容器部署脚本
├── out/                     本地草稿区（gitignored）
├── .tools/                  下载的引擎二进制（gitignored，约 137 MB）
├── _bak-YYYYMMDD/           破坏性操作前的回滚快照（gitignored）
├── .github/workflows/       CI
├── sources.txt              订阅源清单
└── README.md                人类入口：项目是什么、怎么跑
```

---

## 2. 顶层目录职责表

| 目录 | 职责 | 允许存放 | 禁止存放 |
|---|---|---|---|
| `airport/` | 业务逻辑与 CLI 入口 | 可执行模块、被 import 的库、`paths.py` | 数据文件、生成产物、一次性脚本 |
| `scripts/` | 源发现（人工、低频） | 只被人工调用的发现/汇总脚本、`_probe_lib.py` | 日常流水线要跑的代码（那是 `airport/`） |
| `tests/` | 回归测试 | 不依赖网络、可在干净 clone 上跑通的测试 | 需要真实代理节点的集成测试（放 `out/`） |
| `documentation/` | 人类文档 | 设计、开发、运维、参考四类 Markdown | 任何会被程序读取的配置 |
| `data/` | 提交的运行时状态 | 见 §4 白名单 | 中间调试产物、日志 |
| `output/` | 生成物 | 仅 `clash.yaml` | 手工编辑（会被下一次生成覆盖） |
| `docs/` | **GitHub Pages 源** | 仅 `clash.yaml`、`.nojekyll` | **任何文档**（会被公开发布，且改路径会断订阅 URL） |
| `nas/` | NAS 部署脚本 | `setup.sh`、`loop.sh`、诊断脚本 | Python 业务逻辑（那在 `airport/nas_probe.py`） |
| `out/` | 本地草稿 | 报告、调试转储、一次性探针 | 任何项目依赖的东西（gitignored，clone 后不存在） |
| `.tools/` | 引擎二进制 | `mihomo/`、`sing-box-*/` | 任何源码 |
| `_bak-YYYYMMDD/` | 回滚快照 | 被覆盖文件的副本 | 长期归档（过期应删） |

### 2.1 `docs/` 的特殊约束（最容易踩）

`docs/` 被 GitHub Pages 占用，不是文档目录。证据：

```
https://icecome.github.io/free-proxy-airport/clash.yaml        -> 200
https://icecome.github.io/free-proxy-airport/docs/clash.yaml   -> 404
```

站点根即 `docs/`。因此：

- 文档一律进 `documentation/`，不进 `docs/`。
- `docs/clash.yaml` 的路径是**已发布的对外契约**，改名等于断掉所有客户端的订阅。
- `docs/` 下除订阅外多放一个文件，就会被公开托管。

---

## 3. 新增文件放哪：决策表

按顺序回答，第一个命中的即为归宿。

| 你新增的东西是… | 放这里 | 命名 |
|---|---|---|
| 业务流水线的新阶段 / 新验证器 / 新发布器 | `airport/` | `snake_case.py`，动词开头（`verify_*`、`publish_*`、`apply_*`） |
| 被多个 `airport/` 模块共用的库 | `airport/` | 名词（`node_registry.py`、`paths.py`） |
| 只有人工偶尔跑的探索/汇总脚本 | `scripts/` | 动词开头（`discover_*`、`harvest_*`、`report_*`） |
| 发现脚本共用的抓取工具 | `scripts/_probe_lib.py` | 下划线前缀标记"私有" |
| 能在干净 clone 上跑通的回归测试 | `tests/` | `test_<被测对象>.py`；校验脚本用 `verify_<对象>.py` |
| 设计决策、方案对比 | `documentation/design/` | kebab-case，见 §5 |
| 编码规范、调试技巧、测试策略 | `documentation/dev/` | 同上 |
| 部署、运维、故障排查 | `documentation/ops/` | 同上 |
| 模块清单、环境变量表、数据字典 | `documentation/reference/` | 同上 |
| 新的订阅源 URL | `sources.txt` 追加一行 | — |
| 新的运行时状态文件 | `data/` + 同步更新本文 §4 与 `airport/paths.py` | `snake_case.json` |
| 需要下载的二进制工具 | `.tools/<name>/` | 由代码自动下载，不手工放 |
| 一次性调试产物 | `out/` | 随意，但不得被代码引用 |

### 3.1 判定不了时

问三个问题，任一为"是"就说明放错了：

1. 它被 `airport/` 里的代码 import 吗？→ 是则必须在 `airport/`（不能放 `scripts/`、`tests/`）。
2. 它需要在干净 clone 上存在才能跑测试吗？→ 是则必须在 `tests/` 或 `airport/`，不能放 `out/`。
3. 它会被 CI 或客户端读取吗？→ 是则路径不可随意改动，且需同步本文。

---

## 4. `data/` 白名单

`data/` 里的每个文件都必须被 `airport/` 的代码读取或写入，且被 CI 提交。

| 文件 | 生产者 | 消费者 | 说明 |
|---|---|---|---|
| `candidates.json` | `generator` Phase A | `generator` Phase B | 阶段化候选，26h TTL |
| `probe_queue.json` | `generator` | `nas_probe` | 下发给 NAS 的待测队列，上限 500 |
| `cn_alive.json` | `nas_probe` | `generator`、`verify_nodes` | NAS 国内视角结果，6h 后降级为提示 |
| `node_registry.json` | `apply_verdicts`、`local_verify`、`verify_nodes` | 几乎所有模块 | verified 24h / blacklist IP 7d 域 3d |
| `source_stats.json` | `generator` | `generator` | 源优先级与连续失败计数 |
| `discovered_*.json`、`checked_github.json`、`harvested_telegram.json`、`candidates_report.json` | `scripts/*` | 人工审阅、`report_candidates` | 源发现的中间结果 |

**新增状态文件的三步**：① 在 `airport/paths.py` 加常量；② 在本文 §4 补一行；
③ 若需 CI 提交，在 `.github/workflows/update.yml` 的 `for f in ...` 列表加路径。

---

## 5. 命名规范

### 5.1 目录

- 全小写英文，单词间用连字符（`documentation/`、`design/`）。
- 不用时间（`2026-11/`）或人名（`zhangsan/`）组织目录。
- 同一层级单复数一致，默认复数。

### 5.2 文件

| 类型 | 规范 | 示例 | 反例 |
|---|---|---|---|
| Python 模块 | `snake_case.py` | `node_registry.py` | `NodeRegistry.py`、`node-registry.py` |
| 测试 | `test_<对象>.py` | `test_dedup.py` | `dedup_test.py`、`test1.py` |
| 文档 | kebab-case `.md` | `coding-conventions.md` | `编码规范.md`、`规范v2.md` |
| 文档索引 | 固定 `README.md` | `documentation/dev/README.md` | `index.md`、`目录.md` |
| 数据文件 | `snake_case.json` | `probe_queue.json` | `probeQueue.json` |
| 回滚快照 | `_bak-YYYYMMDD/` | `_bak-20261124/` | `backup/`、`old/` |

### 5.3 禁止

- 文件名嵌版本号（版本由 Git 管理）：`generator_v2.py` ❌
- 文件名嵌日期（ADR 除外）：`report-2026-11-24.md` ❌
- 中英混排文件名：`设计文档-design.md` ❌
- `utils.py`、`helpers.py`、`common.py` 这类无信息量模块名 ❌

---

## 6. 开发流程约束

### 6.1 依赖方向（违反即返工）

```
paths.py  ←  所有模块（唯一叶子，只被 import，不 import 别人）

generator.py        →  node_registry, paths
verify_nodes.py     →  node_registry, paths
local_verify.py     →  node_registry, paths
rescue_pass.py      →  node_registry, paths, verify_nodes
apply_verdicts.py   →  node_registry, paths
publish_verified.py →  generator, node_registry, paths
cn_alive_check.py   →  （无内部依赖）
nas_probe.py        →  paths
```

规则：

1. **禁止循环 import。** `rescue_pass` 可以 import `verify_nodes`，反向不行。
2. **`airport/` 不得 import `scripts/` 或 `tests/`。** 依赖只能从 `scripts/`/`tests/` 指向 `airport/`。
3. **路径只能来自 `paths.py`。** 新增 `Path("data/...")` 字面量即违规——工作目录一变就写错地方。
4. **`scripts/` 之间可以互相 import**（`harvest_telegram` import `discover_telegram` 是既有的），
   但不得反向被 `airport/` 依赖。

### 6.2 入口形式

`airport/` 下每个带 `main()` 的模块必须同时支持两种调用：

```pwsh
python -m airport.local_verify        # 从仓库根
cd airport; python local_verify.py    # 从包目录
```

靠模块头部的 `if __package__ in (None, ""):` 引导块实现。新增可执行模块时照抄。

### 6.3 改动后的最小验证

```pwsh
python tests/test_dedup.py
python tests/test_loopback.py
python tests/verify_config.py
python -m airport.publish_verified          # 干跑，确认台账能出订阅
```

涉及 `paths.py` 或目录变动时，额外从**另一个工作目录**再跑一次，确认不依赖 CWD：

```pwsh
cd $env:TEMP; python C:\path\to\tests\test_dedup.py
```

### 6.4 提交前检查

- [ ] 没有在根目录落临时脚本（那是 `out/` 的事）
- [ ] 没有把测试放进 `out/`（会被 gitignore，clone 后不存在）
- [ ] 没有在 `docs/` 放文档（会被公开托管）
- [ ] 新增路径常量已进 `paths.py`，且本文 §4/§1 已同步
- [ ] 四个测试全绿

---

## 7. 本规范的维护

| 触发事件 | 回写内容 |
|---|---|
| 新增/改名/删除顶层目录 | §1 目录树、§2 职责表 |
| 新增 `data/` 状态文件 | §4 白名单、`airport/paths.py` |
| 新增 `airport/` 模块 | §6.1 依赖方向、`documentation/reference/modules.md` |
| 目录职责调整 | 本文 + `README.md` + `AGENTS.md` 三处同步 |

本文件超过 250 行时先拆到 `documentation/design/`，只留规则与索引。
