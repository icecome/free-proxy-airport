# 环境变量

全部变量都有默认值，不设也能跑。前缀统一 `FREE_PROXY_AIRPORT_`。

采集/发布相关在 `airport/generator.py` 顶部集中定义；验证相关在
`airport/local_verify.py`、`airport/verify_nodes.py` 顶部定义。

---

## 采集与发布（generator.py）

| 变量 | 默认 | 作用 |
|---|---|---|
| `FREE_PROXY_AIRPORT_SOURCES` | `sources.txt` | 源清单路径；相对路径按**仓库根**解析，不按当前工作目录 |
| `FREE_PROXY_AIRPORT_SOURCE_TIMEOUT` | `12` | 单个源拉取超时（秒） |
| `FREE_PROXY_AIRPORT_MAX_CANDIDATES` | `0` | 候选截断上限，`0` = 不截断 |
| `FREE_PROXY_AIRPORT_MAX_OUTPUT` | `400` | 发布节点上限 |
| `FREE_PROXY_AIRPORT_MAX_WORKERS` | `48` | delay 测速并发 |
| `FREE_PROXY_AIRPORT_DEDUP_ENDPOINT` | `1` | 每个 `(host, port)` 发布的节点数；`0` 关闭 |
| `FREE_PROXY_AIRPORT_DEDUP_CANDIDATES` | `1` | 每个 endpoint 允许进入测速的副本数 |
| `FREE_PROXY_AIRPORT_FORCE_COLLECT` | `0` | `1` = 强制走 Phase A 采集，不等 NAS 判定 |
| `FREE_PROXY_AIRPORT_CN_FRESH` | `7200` | NAS 判定新鲜度（秒），超时强制采集 |
| `FREE_PROXY_AIRPORT_PROBE_QUEUE` | `500` | 下发给 NAS 的队列长度 |
| `FREE_PROXY_AIRPORT_TEST_URL` | `http://cp.cloudflare.com/generate_204` | 写进客户端的健康检查 URL |
| `FREE_PROXY_AIRPORT_URL_TEST_URL` | `https://www.google.com/generate_204` | 云端真实出口验证目标 |
| `FREE_PROXY_AIRPORT_LATENCY_TIMEOUT_MS` | `5000` | delay 探测超时（毫秒） |
| `FREE_PROXY_AIRPORT_SPEED_GATE` | `1` | `1` = 开启云端带宽闸门 |
| `FREE_PROXY_AIRPORT_SPEED_TIMEOUT` | `10` | 单次下载探测超时（秒） |
| `FREE_PROXY_AIRPORT_SPEED_ATTEMPTS` | `1` | 每节点下载探测次数 |
| `FREE_PROXY_AIRPORT_SPEED_TOP_N` | `60` | 只对延迟最好的 N 个做测速 |
| `FREE_PROXY_AIRPORT_MIN_MBPS` | `2.0` | 带宽闸门阈值 |
| `FREE_PROXY_AIRPORT_MIN_POOL` | `24` | 低于此池大小时降级保留慢节点 |
| `FREE_PROXY_AIRPORT_URL_TEST_STAGE` | `0` | `1` = 追加串行 url-test 阶段（已证实无额外信息，默认关） |
| `FREE_PROXY_AIRPORT_SKIP_ENGINE` | `0` | `1` = 跳过 mihomo，纯 Python TCP 降级 |
| `FREE_PROXY_AIRPORT_ENGINE_TIMEOUT` | `45` | mihomo 归档下载墙钟上限（秒） |
| `FREE_PROXY_AIRPORT_CN_BOOST` | `1000.0` | **已废弃**，仅为兼容旧 env 文件保留，不再读取 |

---

## 本地体检（local_verify.py / verify_nodes.py / rescue_pass.py）

| 变量 | 默认 | 作用 | 定义处 |
|---|---|---|---|
| `FREE_PROXY_AIRPORT_ENGINE` | 空 | mihomo 可执行文件路径；默认用 `.tools/mihomo/mihomo.exe` | `local_verify.py` |
| `FREE_PROXY_AIRPORT_DELAY_CONCURRENCY` | `12` | 单个 mihomo 实例内的 delay 并发 | `local_verify.py` |
| `FREE_PROXY_AIRPORT_TCP_TIMEOUT` | `4` | 首轮 TCP 探测超时（秒） | `local_verify.py` |
| `FREE_PROXY_AIRPORT_SPEED_TIMEOUT` | `10` | 流式测速超时（秒） | `local_verify.py`、`rescue_pass.py` |
| `FREE_PROXY_AIRPORT_MIN_MBPS` | `2.0` | `OK` / `SLOW` 的分界 | `local_verify.py` |
| `FREE_PROXY_AIRPORT_LATENCY_TIMEOUT_MS` | `5000` | mihomo delay 探测超时 | `local_verify.py` |
| `FREE_PROXY_AIRPORT_TCP_TIMEOUT_MS` | `2000` | TCP 降级拨测超时（生成器路径用） | `generator.py` |
| `FREE_PROXY_AIRPORT_TCP_WORKERS` | `64` | TCP 降级并发 | `generator.py` |

注意 `FREE_PROXY_AIRPORT_MIN_MBPS` 与 `FREE_PROXY_AIRPORT_SPEED_TIMEOUT` 被多个模块读取，
改一处等于同时改云端闸门与本地体检的判定线。

---

## 用法示例

```pwsh
# 强制重新采集并发布
$env:FREE_PROXY_AIRPORT_FORCE_COLLECT = '1'
python -m airport.generator

# 跳过 mihomo，纯 TCP 降级（CI 拿不到二进制时的兜底）
$env:FREE_PROXY_AIRPORT_SKIP_ENGINE = '1'
python -m airport.generator

# 只测前 20 个节点，不写台账
python -m airport.local_verify --limit 20 --no-registry

# 用自备的 mihomo
$env:FREE_PROXY_AIRPORT_ENGINE = 'C:\tools\mihomo.exe'
python -m airport.local_verify
```
