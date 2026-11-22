# 源发现脚本（按需运行，不进 cron）

`generator.py` 只消费 `sources.txt` 里的固定 URL，不会自己找新源。这三个脚本负责"找"，
跑完人工确认后再写进 `sources.txt`。**它们不参与日常订阅生成**，不改动 `output/`。

## 脚本

| 脚本 | 作用 |
|---|---|
| `discover_github.py` | GitHub Search API 按 15 组关键字翻页，对每个仓库并发探测 14 条订阅路径 |
| `discover_telegram.py` | 找 Telegram 公开频道，走 `t.me/s/<handle>` 无登录预览抓节点 |
| `harvest_telegram.py` | 对已确认发节点的频道翻更多页，测订阅 URL 与单体节点 TCP |
| `check_discovered.py` | 对发现结果做存活复检：能否取到、节点数、TCP 抽样存活率 |
| `report_candidates.py` | 汇总排序，输出推荐清单（可选 `--append` 写回 `sources.txt`） |

## 用法

```pwsh
python scripts/discover_github.py --pages 3 --max-repos 400 --workers 16 --repo-budget 14
python scripts/check_discovered.py --in data/discovered_github.json --out data/checked_github.json
python scripts/report_candidates.py            # 只看
python scripts/report_candidates.py --append   # 直接写进 sources.txt
```

## 踩过的坑（改代码前先看）

1. **`raw.githubusercontent.com` 在国内被 RST**。必须走 `http://ghproxy.net/` 前缀
   （`_probe_lib.RAW_MIRRORS`）。注意是 **http** 不是 https —— https 握手会间歇性 reset。

2. **本机 nekobox 的系统代理会把请求变成 5 秒后返回的假 502**，看起来像"所有镜像全挂"。
   所以 `_probe_lib` 用 `requests` 且 `trust_env=False`，绝不吃系统代理。

3. **ghproxy.net 对 404 响应极慢**（实测 19~32 秒）。`requests` 的 read timeout 只约束单次
   socket 读，管不住这种慢速回包，必须用 `http_get_hard()` 的 `future.result(timeout=...)`
   做硬性墙钟上限。

4. **ghproxy.net 有并发限速**：64 并发时 32/64 返回 503，且是静默失败——第一次全量扫描
   因此报告"400 仓库 0 命中"。修法：并发 ≤16、每个请求重试 2 次。当前参数（16 并发 + 2 重试）
   实测 100% 成功率。

5. **单仓库的多条路径必须并行探测**。串行时每个 404 吃 19 秒，一个仓库要 3 分钟；
   改成仓库内 8 路并发后降到 8~14 秒。

## Telegram 现状（2026-11-23）

- `t.me/s/` 机制**确认可用**，首次扫描找到 4 个真实发布节点的公开频道：
  `@v2raynodes`（13 个代理链接 + 4 个订阅）、`@v2rayfree`（4）、`@nodeshare`（3）、`@freefq`（1 订阅）。
- 但**当天晚些时候 t.me 连接被阻断**，`harvest_telegram.py` 的翻页复抓未能完成。
  频道内容已存于 `data/discovered_telegram.json`，网络恢复后可直接
  `python scripts/harvest_telegram.py` 补跑，无需重新发现。
- 用搜索引擎反查频道名（Bing/DDG）在此网络下无效：结果页被代理拦截，`t.me/` 命中数为 0。
  目前的频道名来自脚本内置种子列表。

## 产出文件

- `data/discovered_github.json` —— 仓库扫描命中
- `data/checked_github.json` —— 存活复检结果（20/20 可用）
- `data/discovered_telegram.json` —— 频道发现结果
- `data/candidates_report.json` —— 汇总排序
