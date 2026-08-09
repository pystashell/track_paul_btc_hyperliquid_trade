# Paul BTC Hyperliquid Trade History

这是一个公开、可复核的历史数据仓库，用来长期追踪交易员 **Paul** 在
[Hyperliquid](https://hyperliquid.xyz/) 上的 **BTC 永续合约交易**。

- 账户：`0xdae4df7207feb3b350e4284c8efe5f7dac37f637`
- 范围：BTC 交易、订单、资金费、资金进出与账户状态
- 目标：保留从账户开始活动至今的完整历史，不受 Hyperliquid 单次返回窗口限制
- 数据原则：事件保持 API 字段；单点状态只在外层增加必要的快照历史结构

## 目录

```text
track_paul_btc_hyperliquid_trade/
├── .github/
│   └── workflows/
│       └── daily-update.yml
├── data/
│   ├── clearinghouseState.json
│   ├── frontendOpenOrders.json
│   ├── historicalOrders.json
│   ├── openOrders.json
│   ├── spotClearinghouseState.json
│   ├── userFillsByTime.json
│   ├── userFunding.json
│   └── userNonFundingLedgerUpdates.json
├── tests/
│   └── test_tracker.py
├── .gitignore
├── API.md
├── README.md
└── tracker.py
```

四个事件型接口是去重后的完整 API 数组；四个单点状态接口使用统一的
`hyperliquid.snapshot-history.v1` 外层，将每天的原始 API response 作为 checkpoint 保存。
`portfolio` 是由底层事件和价格派生的官方展示曲线，不属于长期保存范围。具体定义见
[API.md](API.md)。

首次上传时，上述 8 个 `data/*.json` 必须和代码一起进入 `main`。它们不是临时抓取文件，
而是已经整理好的历史基线，也是以后每次增量更新的输入。GitHub Actions 不读取本地旧备份，
也不需要 `source_path`、`user_snapshot/`、`account_state/` 或 SQLite。

## 本地验证

项目仅使用 Python 标准库，建议 Python 3.11 或更高版本。

```powershell
python -m unittest discover -s tests -v
python tracker.py validate --data-dir data --report .local/validate.json
```

每日更新命令为：

```powershell
python tracker.py update --data-dir data --report .local/update.json
```

不改动 `data/` 的联网试跑命令为：

```powershell
python tracker.py update --dry-run --data-dir data --report .local/dry-run.json
```

`update` 首先直接读取仓库现有的 8 个 `data/*.json`，再把当天 API response 读入内存，
去重合并为完整候选版本。更新过程依次执行抓取验证、候选合并验证和落盘后独立验证。
`--dry-run` 会把候选写入自动清理的临时目录并重读验证，绝不替换正式 `data/`。任何一步
失败都不会留下一个可被提交的候选版本；`tracker.py` 本身不会执行 `git commit` 或
`git push`。

## GitHub Actions 自动更新

[daily-update.yml](.github/workflows/daily-update.yml) 每天 `00:23 UTC`（北京时间
`08:23`）运行，也支持在 Actions 页面手动触发。手动运行默认是安全的 dry run；只有明确把
`publish` 设为 `true` 才允许改数据并提交。定时任务固定 checkout `main`，按以下顺序执行：

1. 运行合并规则测试；
2. 验证更新前的 8 个 JSON；
3. 从 Hyperliquid 抓取并在内存中合并；手动 dry run 只验证临时候选；
4. 独立验证落盘后的完整数据；
5. 确认工作区只有 `data/*.json` 发生变化；
6. 有变化时生成一个 commit，并直接 push 到 `main`。

workflow 不创建每日分支，也不会 force-push。任何测试、抓取、合并、路径白名单或最终
验证失败都会在 commit 之前停止，因此远端 `main` 不会改变。GitHub runner 是临时机器，
失败任务中的下载和临时文件会随任务销毁；并发锁保证不会有两个更新任务同时写数据。

workflow 已按最小权限显式申请 `contents: write`，通常不需要把仓库的默认 token 权限整体
改成可写。如果仓库或组织策略额外限制写权限，可在 **Settings → Actions → General →
Workflow permissions** 中检查；如果以后给 `main` 增加 branch protection，也必须明确
允许 GitHub Actions 写入，否则最后的正常 push 会被保护规则拒绝。Hyperliquid 查询是
公共只读 API，不需要额外 secret；提交使用仓库自动提供的 `GITHUB_TOKEN`。

## 提交边界

首次上传只提交以下内容：

- `.github/workflows/daily-update.yml`；
- 8 个 `data/*.json` 历史基线；
- `tracker.py`、`tests/test_tracker.py`、`.gitignore`、`README.md`、`API.md`。

不要提交 `.local/`、`.idea/`、`outputs/`、原始每日抓取包、SQLite、日志或任何临时目录。
尤其不要把外部唯一备份复制进仓库。Action 的正常 commit 也被白名单限制为上述 8 个
`data/*.json`。

## 试运营顺序

1. 本地运行单元测试和 `validate`；
2. 本地运行一次 `update --dry-run`，确认正式 8 个 JSON 的 SHA-256 完全不变；
3. 人工检查本页“提交边界”中的文件，再创建首次 commit；
4. 上传后先从 Actions 页面手动运行，保持 `publish=false`；
5. dry run 通过后，再手动以 `publish=true` 做一次真实更新，随后进入每日定时运行。

合并逻辑不依赖“昨天”这个日期。中间漏跑几天后仍会重新读取服务端可见区间并按稳定身份
补入；但如果停机长到订单或成交已经滚出 Hyperliquid 的服务端窗口，任何爬虫都无法保证
补回未曾保存的记录，因此每日任务失败应及时查看并重跑。

## 当前历史基线

初始历史由 2026-04-29 至 2026-08-06 的既有抓取合并，并在 2026-08-08 完成了一次联网
预验证 checkpoint。账户最早的资金和交易事件始于 2025-11-15，因此事件文件覆盖的是
账户开始活动以来的历史，而不是从第一次运行本项目的日期才开始记录。

本仓库不使用 SQLite。所有需要公开和长期维护的数据都直接保存在 `data/*.json`。
