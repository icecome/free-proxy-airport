# 文档索引

本目录是人类文档。项目目录职责与新增文件的存放规则见
[structure.md](structure.md)——那是**唯一裁决依据**，与其他文档冲突时以它为准。

> `docs/` 不是文档目录：它是 GitHub Pages 源，只放订阅产物。原因见
> [structure.md §2.1](structure.md)。

---

## 按用途查

| 我想… | 看这里 |
|---|---|
| 知道项目怎么跑起来 | [../README.md](../README.md) |
| 知道某个文件该放哪个目录 | [structure.md](structure.md) §3 决策表 |
| 知道某个模块干什么、被谁调用 | [reference/modules.md](reference/modules.md) |
| 知道某个环境变量的作用与默认值 | [reference/env-vars.md](reference/env-vars.md) |
| 知道 `data/` 里某个 JSON 的字段含义 | [reference/data-files.md](reference/data-files.md) |
| 写新代码，想知道约定 | [dev/coding-conventions.md](dev/coding-conventions.md) |
| 理解目录为什么长这样 | [design/structure-decisions.md](design/structure-decisions.md) |
| 部署 / 排查 / 跑运维流程 | [ops/](ops/) |
| 给 AI 编码代理的指令 | [../AGENTS.md](../AGENTS.md) |

---

## 目录

```
documentation/
├── README.md                        本文：索引
├── structure.md                     目录职责、存放规范、开发流程约束（裁决依据）
├── design/
│   └── structure-decisions.md       为什么这样组织：6 条决策 + 有意不做的改动
├── dev/
│   └── coding-conventions.md        环境、命令、路径规则、测试写法、调试技巧
├── ops/
│   └── runbook.md                   日常运维：采集、体检、发布、故障处置
└── reference/
    ├── modules.md                   每个模块做什么、被谁调用、改动的连带影响
    ├── env-vars.md                  环境变量完整清单（按模块分组）
    └── data-files.md                data/ 与产物的 JSON schema
```

---

## 文档分工

| 文档 | 读者 | 回答什么 | 更新时机 |
|---|---|---|---|
| `README.md` | 新成员 | 项目是什么、怎么跑 | 用法变化时 |
| `structure.md` | 所有开发者 | 文件放哪、目录职责、开发约束 | 目录或数据文件变动时 |
| `design/` | 维护者 | 为什么这样设计、推翻过什么 | 架构决策时 |
| `dev/` | 写代码的人 | 怎么写、怎么测 | 约定变化时 |
| `ops/` | 跑运维的人 | 怎么执行、怎么排查 | 流程或故障处置变化时 |
| `reference/` | 所有人 | 查表：模块、变量、schema | 对应实体变化时 |
| `AGENTS.md` | AI 编码代理 | 代理需要但人不需要的细节 | 踩坑后 |

---

## 写作约定

- 中文技术文档，风格遵循 `tech-doc-style-chinese`：克制、准确、可扫读。
- 文件名 kebab-case，全小写英文；目录按**用途**划分，不按时间或人。
- 不在文件名里嵌版本号或日期（版本由 Git 管理）。
- 一个文档只承载一类主题；超过 250 行先考虑拆分。
- 命令一律 pwsh 形式（`nas/*.sh` 例外，那是容器内 POSIX sh）。
- 每条规则带依据；没有依据的"建议"不写进规范。
