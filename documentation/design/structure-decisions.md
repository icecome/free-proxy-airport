# 设计说明：为什么这样组织

记录影响目录结构与模块划分的关键决策，避免后来者"顺手重构"回被推翻的形态。
纯技术实现细节见 [../reference/modules.md](../reference/modules.md)。

---

## 决策 1：业务代码收进 `airport/` 包

**原状**：`generator.py`、`node_registry.py`、`verify_nodes.py`、`cn_alive_check.py`、`nas_probe.py`
五个模块平铺在仓库根，验证器脚本散在 `scripts/`，测试散在 `out/`。

**问题**：

1. 根目录混着入口脚本、库模块、源清单、配置，看不出哪些是库、哪些是入口。
2. `scripts/` 同时装着"源发现"和"日常验证器"两类语义完全不同的东西。
3. 库模块靠 `sys.path.insert(0, 父目录)` 互相 import，路径约定不统一。

**决定**：所有被 import 的库与所有流水线入口收进 `airport/`，`scripts/` 只留源发现。

**依据**：判断标准是"被谁调用"，不是"是不是 Python"。`local_verify.py` 虽然从
`scripts/` 搬进来，但它每天都会被 `apply_verdicts` 的下游消费，属于流水线而非一次性工具。

---

## 决策 2：路径集中到 `paths.py`

**原状**：每个模块各自写 `Path("data/candidates.json")` 这类**相对当前工作目录**的路径。

**问题**（实测）：

```pwsh
# 在 airport/ 目录里跑，会写进 airport/output/clash.yaml
cd airport; python generator.py
```

工作目录一变就静默写到错误位置。CI 里恰好都在仓库根跑，所以这个缺陷一直没暴露。

**决定**：`airport/paths.py` 用 `Path(__file__).resolve().parent.parent` 锚定仓库根，
所有路径从它派生。禁止任何模块再写路径字面量。

**代价**：新增状态文件时必须同时改 `paths.py` 与 `documentation/structure.md` §4。
这个代价是有意的——它强制新文件有明确的归属。

---

## 决策 3：`docs/` 不用于放文档

**原状**：项目没有文档目录，`docs/` 被 GitHub Pages 占用（只放 `clash.yaml` 和 `.nojekyll`）。

**问题**：按通用惯例把文档放进 `docs/` 会同时踩两个坑：

1. 文档被公开发布到 `https://icecome.github.io/free-proxy-airport/`。
2. 若为了腾位置移动 `docs/clash.yaml`，会断掉所有客户端已配置的订阅 URL。

**决定**：人类文档进 `documentation/`，`docs/` 保持只放发布产物。

**依据**：实测确认站点根就是 `docs/`：

```
/clash.yaml        -> 200
/docs/clash.yaml   -> 404
```

**代价**：与"`docs/` 是文档目录"的行业惯例不符，所以 `README.md` 与 `AGENTS.md`
都显式写明这条，`structure.md` §2.1 单独用一节解释。

---

## 决策 4：测试从 `out/` 搬到 `tests/`

**原状**：`test_dedup.py`、`test_loopback.py`、`verify_config.py` 放在 `out/`，
而 `out/` 在 `.gitignore` 里。

**问题**：这三个测试**从来没有进过版本库**。`git ls-files out/` 返回 0 条。
后果是任何新 clone 都无法执行 README 里写的"回归自检"，只有原作者本机有这三个文件。

**决定**：搬进 `tests/`，纳入版本控制。`out/` 回归纯草稿区语义。

**依据**：测试是项目资产，不是本地产物。判定标准是"别人 clone 后需不需要它"。

---

## 决策 5：`nas/nas_probe.py` 与根目录 `nas_probe.py` 的重复

**原状**：两份 `nas_probe.py`，一份在根，一份在 `nas/`，内容有实质差异：

| 项 | 根目录版 | `nas/` 版 |
|---|---|---|
| `subprocess` 超时 | 300 秒 | 120 秒 |
| `git pull` 重试 | 3 次，指数退避 | 无重试 |

**问题**：`nas/setup.sh` 与 `nas/loop.sh` 引用的是 `/workspace/nas_probe.py`
（根目录版）与 `/app/nas_probe.py`——**实际运行的是根目录版**，`nas/` 里那份是
部署脚本的早期副本，从未被执行。

**决定**：保留根目录版（含重试逻辑，更健壮），搬进 `airport/nas_probe.py`；
`nas/` 里的旧副本归档到 `_bak-20261124/`；`nas/*.sh` 的路径改为 `airport/nas_probe.py`。

**依据**：以实际被执行的那份为准，而不是以"看起来更该在 `nas/` 里"的直觉为准。

---

## 决策 6：`nas/diag4.json` 移出 `nas/`

**原状**：`nas/diag4.json` 是一份 1Panel 容器创建请求的样本（`{"image":"python:3.12-alpine",...}`）。

**问题**：不是脚本，不被任何代码或文档引用，放在 `nas/` 里与部署脚本混在一起。

**决定**：移到 `_bak-20261124/nas-diag4.json.bak`。

**依据**：`nas/` 的职责是"可执行的部署脚本"，配置样本不属于此列；
且它无引用、无版本价值。

---

## 未做的改动（有意保留）

| 项 | 为什么不改 |
|---|---|
| `generator.py` 2083 行不拆 | 内部是单次流水线，拆开会产生大量跨模块状态传递；拆分收益低于风险。真正需要的是内部函数分组注释，不是文件拆分。 |
| `scripts/_probe_lib.py` 的下划线前缀 | 表示"包内私有"，但 Python 不会因此阻止 import。保留是因为它明确告诉读者"这不是入口"。 |
| `data/` 里的源发现中间结果 | 虽然只在人工审阅时用，但它们是可追溯的证据（哪些源在什么时候被评估过），且有 5 个文件、合计不到 40 KB。 |
| 两份 `clash.yaml` 的重复 | CI 用 `cmp` 强制一致，是刻意的双写。改成符号链接会让 GitHub Pages 与 Actions checkout 的兼容性变复杂。 |
