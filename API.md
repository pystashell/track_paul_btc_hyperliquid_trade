# API 与数据模型

所有请求均为：

```text
POST https://api.hyperliquid.xyz/info
```

用户地址固定为：

```text
0xdae4df7207feb3b350e4284c8efe5f7dac37f637
```

## 长期保存的 8 个 API

| API `type` | 类型 | 长期用途 | 文件 |
|---|---|---|---|
| `historicalOrders` | 事件 | 订单状态历史；每日把最近窗口并入已有全历史 | `data/historicalOrders.json` |
| `userFillsByTime` | 事件 | 全部 BTC 成交，是仓位和已实现盈亏复演的核心 | `data/userFillsByTime.json` |
| `userFunding` | 事件但会压缩 | 永续资金费；处理小时记录与日汇总的替换关系 | `data/userFunding.json` |
| `userNonFundingLedgerUpdates` | 事件 | 充值、提现等非资金费账户现金流 | `data/userNonFundingLedgerUpdates.json` |
| `openOrders` | 快照 | 轻量的当前挂单原始视图 | `data/openOrders.json` |
| `frontendOpenOrders` | 快照 | 带触发、TP/SL、reduce-only、TIF、children 等前端语义的挂单视图 | `data/frontendOpenOrders.json` |
| `clearinghouseState` | 快照 | BTC 仓位、入场价、未实现盈亏、累计资金费和永续账户状态 | `data/clearinghouseState.json` |
| `spotClearinghouseState` | 快照 | 统一账户的 USDC 总额及现货侧状态；用于总权益核对 | `data/spotClearinghouseState.json` |

`openOrders` 和 `frontendOpenOrders` 不是同一个响应格式。实测两者当前返回相同 oid
集合和相同核心挂单字段，但 `frontendOpenOrders` 额外提供前端解释订单所需的字段。
两份数据都很小，为了保持 API 忠实度，两者都作为快照保存。

没有长期保存以下 4 个接口：

- `portfolio`：官方展示用的采样账户价值和 PnL 曲线，由 fills、资金费、账户现金流和 BTC
  标记价格派生；它不提供新的底层交易事实。每日权益 checkpoint 已由
  `clearinghouseState` 和 `spotClearinghouseState` 保存，因此不再抓取或保存。
- `userFills`：与 `userFillsByTime(aggregateByTime=false)` 的成交记录相同，但只能取最近
  2,000 条，且没有时间范围；后者更适合历史合并。
- `subAccounts`：这个展示账户没有子账户，不属于 BTC 交易全历史。
- `userRole`：当前固定为普通 `user`，不是交易或账户价值历史。

另有一个按需修复查询 `orderStatus`：如果某条 fill 的 `oid` 已经滚出
`historicalOrders` 最近窗口，脚本会逐个查询该 oid，并把返回值中原始的
`HistoricalOrder` 对象并入 `historicalOrders.json`。它只用于补洞，不需要单独维护第
9 个 JSON；若任何 fill oid 仍无法恢复，验证会失败并阻止发布。

如果未来 Paul 开始交易 BTC 以外的资产，严格的范围校验会失败，届时应先明确扩大仓库
范围，而不是静默混入其他币种。

## 文件格式

### 事件型 API

四个事件文件的顶层都是数组，与 Hyperliquid API response 的类型一致。每条记录的字段、
字符串数值和嵌套结构保持原样；仓库只做去重、排序和跨次抓取的无上限合并。

### 快照型 API

单点 response 无法表达历史，因此以下四个快照文件统一增加一层：

- `openOrders.json`
- `frontendOpenOrders.json`
- `clearinghouseState.json`
- `spotClearinghouseState.json`

```json
{
  "schema": "hyperliquid.snapshot-history.v1",
  "request": {
    "type": "clearinghouseState",
    "user": "0xdae4df7207feb3b350e4284c8efe5f7dac37f637"
  },
  "snapshots": [
    {
      "runId": "20260806T224533Z",
      "capturedAt": "2026-08-06T22:45:38.000000+00:00",
      "response": {}
    }
  ]
}
```

只有 `schema`、`request`、`runId` 和 `capturedAt` 是仓库封装字段；`response` 内部就是
对应 API 当时返回的完整原始值。新爬虫的 `runId` 精确到微秒并附带随机后缀，不会再因
同秒多次请求产生文件名冲突。实际请求先保存在内存中，也不会为每一页创建互相覆盖的
正式文件。每个 UTC 日只保留最新一次通过验证的 checkpoint；同日重跑会替换当天较早
的 checkpoint，不会把一次 Action 重试误当成新的一天。

## 去重与归并

| 数据 | 稳定身份 | 排序 |
|---|---|---|
| historical order | `order.oid + status + statusTimestamp` | 新到旧 |
| fill | `coin + time + tid`；无 tid 时使用成交核心字段回退 | 旧到新 |
| non-funding ledger | `time + delta.type + 完整 delta` | 旧到新 |
| snapshot | `runId + capturedAt`；并限制每个 UTC 日一个 | 旧到新 |

`hash` 不作为成交或 ledger 的唯一身份，避免同一经济事件仅因哈希表现变化而重复计数。
出现相同稳定身份但内容变化时采用更新抓取到的完整 API 记录，并在本地验证报告中统计
冲突数。

### 资金费的特殊规则

`userFunding` 的老数据会从逐小时记录压缩成带 `delta.nSamples` 的日汇总。如果直接把
历次响应做 JSON union，同一天会同时存在 24 条小时记录和 1 条日汇总，导致资金费双计。

归并以 `UTC 日 + delta.type + coin` 为单位：

1. 如果已经保存了完整的 `nSamples` 条小时记录，且小时 `usdc` 之和与最新日汇总在
   `0.000001 USDC` 内一致，保留信息更完整的小时记录。
2. 小时记录不完整或后来汇总金额发生修订时，只保留最新日汇总。
3. 同一天绝不同时保留小时记录与日汇总。

所有输出行仍然是 Hyperliquid 返回过的原始行，脚本不会自行生成一条伪造资金费记录。

## API 窗口与抓取方式

Hyperliquid 官方文档当前说明：

- `historicalOrders` 最多返回最近 2,000 条；所以必须每天 union，不能日后回补旧订单。
- `userFillsByTime` 单次最多 2,000 条，服务端仅保留最近 10,000 条成交。
- 带时间范围的接口通常按最多 500 个元素或数据块分页。

脚本每天重新读取可访问的时间窗口，通过稳定身份并入本地无上限数组。时间范围一旦返回
500 条，就会递归拆分为更小且不重叠的区间；资金费区间只在 UTC 日边界拆分，避免把一个
日汇总切成两份。所有请求串行并带间隔和重试，不会以并发洪泛 API。

官方资料：

- [Hyperliquid Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Hyperliquid rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)

## 三层验证和发布边界

1. **抓取验证**：HTTP/JSON、响应类型、必要字段、BTC-only 范围、单次响应稳定身份唯一性，
   以及两个 open-order 视图的核心字段对照。
2. **候选合并验证**：旧数据不得减少；键必须唯一；排序固定；资金费不得小时/日双计；
   资金费总额必须等于本次 API 全量响应；所有历史 clearinghouse checkpoint 的 BTC
   仓位必须能由 fills 精确复演。
3. **落盘验证**：候选先写入临时目录并完整重读，通过后才替换 `data/`；替换后再次验证
   JSON、数量和 SHA-256。失败会回滚旧目录。

最新 checkpoint 还会检查：

- 成交复演仓位等于 `clearinghouseState` 的 BTC `szi`；
- `userFunding` 累计值与持仓 `cumFunding.allTime` 方向相反、金额相等；
- 充值、已实现盈亏、手续费、资金费和未实现盈亏组成的权益公式与统一账户状态相符。

每个正式 JSON 还必须小于 45 MiB，给 GitHub 的 50 MiB 提示阈值留出余量。达到闸门时
更新会在 commit 前失败，不会把超限文件推到 `main`。

脚本不包含任何 Git 写操作。自动化层只有在上述验证全部通过后，才应把 `data/*.json`
和必要代码作为一个原子 commit 更新到仓库。
