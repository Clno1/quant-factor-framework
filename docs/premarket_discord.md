# 美股开盘前 Discord 日报（动量与板块轮动）

## 1. 目标与边界

生产 unit 在每个 XNYS 交易日美东时间 09:20 运行 `--channel all`，分别处理两个独立频道：

- `#momentum-alerts`：截至上一完整交易日收盘的动量突破/Setup 候选；
- `#sector-rotation`：上一完整交易日的板块与细分行业强弱。

实现位于独立的 `src/premarket_digest/` 叶子编排层。允许的依赖方向是：

```text
systemd -> scripts/run_premarket_digest.py -> src/premarket_digest
                                             |-> alerts/breakouts/data
                                             `-> group_analytics ArtifactReader
```

禁止 `alerts`、`group_analytics`、因子、回测、策略、模拟盘和执行模块反向导入
`premarket_digest`。盘前日报也不读写现有 `AlertStateStore`，所以不会把盘前候选错误地
标成盘中小时告警“已经投递”。

`sector-rotation` 是频道名称。当前 Stage 1 内容准确含义是“单日板块/细分行业强弱”，
不是多日行业动量或轮动趋势预测，Discord footer 会持续显示这一限制。

## 2. 为什么使用两个 Incoming Webhook

当前需求只有服务器定时单向发送，不需要读取频道历史、接收命令或交互按钮，因此两个
独立的 Discord Incoming Webhook 比 Bot 更简单：

```text
DISCORD_MOMENTUM_WEBHOOK_URL        -> #momentum-alerts
DISCORD_SECTOR_ROTATION_WEBHOOK_URL -> #sector-rotation
```

Webhook 与创建时选择的频道绑定；一个 Webhook 不能安全地作为两个频道的通用凭据。
完整 URL 含安全 token，等同密码。Discord 官方说明见
[Webhook Resource](https://docs.discord.com/developers/resources/webhook) 和
[Intro to Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)。

只有将来需要频道命令、读取最近消息来辅助严格去重，或使用交互组件时，才升级为 Bot。

## 3. 统一交易日语义

令：

- `T`：即将开盘的 XNYS session；
- `T-1 = previous_session(T)`：上一完整 XNYS session；
- 调度时间：`09:20 America/New_York`；
- 可发送窗口：09:20–09:29 ET。

Worker 使用 `exchange-calendars` 的 `XNYS` 日历二次校验：

1. 当前美东日期必须是实际 XNYS session；周末和交易所休市日正常跳过；
2. 周一会自动回看周五，节后会回看最后一个真实 session，不使用普通工作日 `BDay`；
3. systemd `Persistent=true` 可能在服务器恢复后补跑，因此 09:30 起必须跳过，不能把
   开盘后的消息伪装成“盘前”；
4. timer 直接使用 `America/New_York`，夏令时和冬令时不做固定新加坡时间换算；
5. 两个频道必须标示相同的 `T-1`，不把旧行业产物和新动量日线混在一份日报中。

## 4. `#momentum-alerts` 算法

### 4.1 数据范围

盘前摘要不调用 `run_live_alert_scan()`，也不把盘前 batch quote 拼成正式日线。它通过
`load_breakout_daily_dataset()` 读取一个 `US_LIQUID_5M` 正式版本，并复用现有
`src.breakouts.scanner.evaluate_daily_setup` 算法。日线、证券属性和 membership 都来自同一
版本，报告保存完整 `DataContract`；发送窗口内不会临时请求 FMP 或刷新股票池。

股票池步骤：

1. 解析 `US_ACTIVE -> US_LIQUID_5M` 的唯一 `DatasetVersion`；
2. 默认严格保留 `asset_type == STOCK`，ETF 不进入日报；
3. 保留当前流动性 `current_dollar_volume >= 5,000,000 USD` 的股票；配置的
   `momentum_alerts.always_tickers` 只能让已经存在于 `US_ACTIVE` 且资产类型合规的成员
   绕过流动性门槛，不会凭空补入股票池。它位于 `configs/default.yaml`，是 refresh 与
   premarket 的共享来源；不要再把新例外分散写进两个 env 文件；
4. 每只股票只允许使用 `date <= T-1` 的 bar；
5. 最后一根 bar 必须恰好等于 `T-1`，较旧数据不能混入候选；
6. 日线必须无重复 session，`evaluate_daily_setup()` 必须实际返回 `data_date == T-1`，且
   Close、Return20、ADR20、当日/20 日均成交额、Pivot 和 Score 都是有限值；算法至少需要
   65 个有效日线 session；
7. `exact_asof_count / universe_count >= 80%`，并且
   `evaluable_history_count / universe_count >= 80%`，两项同时通过才允许发送。

两项 80% 都是最低发送门槛，可分别通过
`premarket_digest.momentum.min_exact_asof_coverage` 和 `min_evaluable_coverage` 提高。
摘要会同时显示两组分子、分母和覆盖率；因此“只有一根 T-1 bar”不会被当成可计算，也
不会生成误导性的零候选消息。

生产 reader 校验 DuckDB 中的正式版本、目标 session、bars checksum 和 membership checksum，
并要求各股票最后一根可评估 bar 精确等于 `T-1`。旧的
`data/raw/universe/us_active.premarket.json` 校验器暂时保留为显式兼容测试路径，不再是默认盘前
输入；节假日仍按 XNYS session，而不是普通工作日或文件日期判断。

### 4.2 四项硬筛

对股票 `i`、`T-1` 日记为 `t`：

```text
Return20_i = (Close_i,t / Close_i,t-20 - 1) * 100
ADR20_i    = mean((High_i,d / Low_i,d - 1) * 100), d=t-19..t
DollarVol_i,t = Close_i,t * Volume_i,t
AvgDollarVol20_i = mean(Close_i,d * Volume_i,d), d=t-19..t
```

默认门槛来自既有 `momentum_alerts.strict_scan`：

```text
Return20 >= 20%
ADR20 >= 6%
DollarVol_t >= 10,000,000 USD
AvgDollarVol20 >= 10,000,000 USD
```

只有四项同时通过的股票进入日报候选。零候选仍会生成一条明确的空摘要，避免把“算法
正常运行但没有机会”误解为任务故障。

### 4.3 Setup、Pivot 与评分

在通过硬筛后，沿用既有解释型 Setup 评分：

| 条件 | 定义 | 分值 |
|---|---|---:|
| prior move | 过去约 80 根 bar 的最大低点到高点涨幅至少 30% | 15 |
| consolidation | 60 日先前高点距当前至少 9 个交易日 | 10 |
| MA50 distance | 收盘相对 MA50 在 -5% 到 +35% | 10 |
| MA trend | 收盘不低于 MA20 的 98%，MA10 不低于 MA20 的 98%，MA20 五日斜率为正 | 15 |
| tight range | 最近 3 日平均日内振幅 / ADR20 不高于 0.55 | 15 |
| higher lows | 最近 5 日低点线性斜率为正 | 10 |
| volume dry-up | 前 4 日均量 / 此前 16 日均量不高于 0.85 | 5 |
| near pivot | `Close/Pivot-1 >= -3%` | 10 |
| stop within ADR | `Close/Low_t-1 <= ADR20` | 5 |
| above pivot | `Close >= Pivot` | 5 |

其中：

```text
Pivot = max(High_{t-20}, ..., High_{t-1})
core_ready = prior_move AND consolidation AND MA50 distance AND MA trend AND tight range
```

状态定义：

```text
BREAKOUT = core_ready AND Close >= Pivot
READY    = core_ready AND Close 距 Pivot 不低于 -3%
SETUP    = core_ready，但未进入 READY/BREAKOUT
FORMING  = 四项硬筛已通过，但 core_ready 尚未全部满足
```

稳定排序为 `BREAKOUT -> READY -> SETUP -> FORMING`，再按 score 降序、20D 收益降序、
ticker 升序。Discord 默认展示前 10 个。QQQ 市场过滤独立显示：

```text
PASS = MA10 > MA20，并且 MA10、MA20 相对五日前都在上升
```

QQQ 缓存不是 `T-1` 时显示 `UNKNOWN`，不会伪造 PASS/FAIL。

## 5. `#sector-rotation` 算法与产物门槛

通知层不重算分类，不调用 Web router，也不直接读取 `latest_success.json`。它通过
`ArtifactReader.load_latest()` 分别固定并校验两个 immutable run：

```text
SP500 / FMP / sector / eod
SP500 / FMP / sub_industry / eod
```

每层发送前必须满足：

```text
manifest.asof == T-1
manifest.mode == eod
manifest.snapshot_id == EOD
manifest.session_status == FINAL
manifest.research_only is false
manifest.universe == SP500
manifest.taxonomy == FMP
manifest.taxonomy_level == 对应层级
manifest.generated_at 不得超过当前时间 60 秒（只容忍主机时钟微小偏差）
quality_status != NO_DATA
quality_summary.count_coverage >= 98%
```

98% 是正式 Discord 发送门槛，独立于 group writer 的最低工程发布门槛。只有
`eligible_for_ranking == true` 且 `robust_ew_return_1d` 有限的组参与排名。

### 5.1 Robust EW

对组 `g` 的每个有效计数单元：

```text
r_i,t = AdjustedClose_i,t / AdjustedClose_i,t-1 - 1
m_g   = median(r_i,t)
MAD_g = median(|r_i,t - m_g|)
sigma_robust = 1.4826 * MAD_g
r*_i,t = clip(r_i,t, m_g - 3*sigma_robust, m_g + 3*sigma_robust)
RobustEW_g,t = mean(r*_i,t)
```

有效成员少于 5 或 `MAD=0` 时不截尾。缺失收益不当成 0，而是留在 `n_expected` 中并
降低覆盖率。

宽度使用 ±1bp 严格边界：

```text
advance: r_i,t > +0.0001
decline: r_i,t < -0.0001
unchanged: 其余有效收益
up_pct = advances / n_valid
breadth_net = (advances - declines) / n_valid
```

相对基准为几何相对收益：

```text
relative_g = (1 + RobustEW_g) / (1 + SPY_return) - 1
```

排名 tie-break 固定为：

```text
RobustEW 降序 -> up_pct 降序 -> n_valid 降序 -> group_id 升序
```

Top 与 Bottom 从可排名集合的两端动态截取，并限制在 `floor(n_ranked/2)`，因此两组
永不重叠。默认显示 Sector Top/Bottom 各 3，Sub-industry 各 5，并显示上涨比例、
`n_valid/n_expected`、相对 SPY 以及主要正/负贡献股票。

### 5.2 局部降级

- 两层都通过：一条 Discord 消息中放两个 Embed；
- 只有一层通过：仍发送一条消息，只包含通过层，并明确列出未通过层；
- 两层都未通过：不发送陈旧数据，返回可重试失败，systemd 在发送窗口内重试；
- newer attempt 失败但 `T-1` 的 latest-success 仍完整：使用受校验的成功 run，并在摘要
  中显示 warning。

## 6. Discord 消息合同

每个频道、每个 `T` 的目标是一条消息。实现会在发送和 dry-run 时本地校验 Discord
硬限制：`content <= 2,000`、最多 10 个 Embed、每 Embed 最多 25 个 field、field value
不超过 1,024、所有 Embed 文本合计不超过 6,000 字符。官方限制见
[Message Resource](https://docs.discord.com/developers/resources/message)。

所有请求固定：

```json
{"allowed_mentions":{"parse":[]}}
```

只有配置了 ASCII 十进制 Discord role ID 时才允许显式角色 mention；动量仅在展示行中存在 READY 或
BREAKOUT 时 mention，板块日报按日 mention。正文中的 `@` 会插入零宽字符，不能意外
触发 `@everyone`。Discord 的 mention 规则见
[Allowed Mentions Object](https://docs.discord.com/developers/resources/message#allowed-mentions-object)。

投递固定使用 `POST <webhook>?wait=true`，只有 HTTP 2xx 且响应 JSON 存在非空
`message.id` 才算成功。Discord 官方说明 `wait=true` 会等待消息创建确认，见
[Execute Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook)。

`429` 不写死固定限流值，而是读取 `Retry-After` header 或 JSON `retry_after` 后有限
重试；400/401/403/404 不自动重试；5xx 因无法证明 Discord 未创建消息而直接标记
`UNKNOWN`，禁止自动重试。HTTP redirect 也不会自动跟随，避免隐藏的第二次 POST。
所有请求开始前会按 connect/read 两阶段各 15 秒，再加 2 秒安全余量评估最晚启动点；
预测无法在 09:30 ET 前结束时不再发起 POST。
官方要求按响应头处理限流，见
[Rate Limits](https://docs.discord.com/developers/topics/rate-limits)。

## 7. 幂等、部分失败与不确定投递

独立状态库：

```text
outputs/premarket_digest/state.sqlite3
```

唯一键：

```text
(target_session, channel)
```

每行只保存非敏感目标别名、`source_session`、冻结后的 payload、payload hash、状态、
尝试次数和 Discord message ID；不保存 Webhook URL。状态机：

```text
PENDING -> SENDING -> SENT
                  `-> FAILED   (另存 retryable=1/0，区分安全重试与永久冻结)
                  `-> UNKNOWN  (请求可能已到达 Discord，但回执丢失)
```

两个频道逐个 claim、逐个提交：momentum 成功而 sector 失败时，整体返回部分失败；下一次
systemd 重试会跳过已 `SENT` 的 momentum，只补 sector。并发 worker 由文件锁隔离。

Incoming Webhook 没有客户端幂等键。若请求已经到达 Discord、客户端却在读取响应时
超时，系统无法证明消息是否创建；此时标记 `UNKNOWN`，退出码 3，默认禁止自动重发。
运维人员先查看频道，确认没有消息后，才可对一个明确 session、一个明确频道显式运行
`--retry-unknown --channel <channel>`。CLI 禁止自动推导恢复 session，也禁止对 `all` 同时
重试不确定投递。这是一种偏向
“不重复”的 at-most-once 策略，不宣称严格 exactly-once。

`FAILED_RETRYABLE` 默认继续使用第一次冻结的 payload 重试，保持审计一致；
`FAILED_PERMANENT` 在其他频道触发 systemd 重跑时仍保持冻结，不会再次 POST。如果凭据、
数据或模板已经修复，可对单个频道显式加 `--rebuild-failed`；它只允许替换 `FAILED`，
绝不会改写 `SENT` 或 `UNKNOWN`。旧版没有 `retryable` 列的 SQLite 会在启动时自动加列，
旧 `FAILED` 行按永久冻结处理，避免升级后意外重发。

人工恢复命令必须固定一个 `--session` 和一个频道，并同时给出历史发送、窗口外发送两项
明确授权：

```bash
# 已人工确认频道中没有 UNKNOWN 对应消息后，才允许重发
python scripts/run_premarket_digest.py --send --allow-outside-window \
  --session 2026-07-16 --allow-historical-send \
  --channel momentum --retry-unknown

# 修复凭据/数据/模板后，重建一个永久失败的冻结 payload
python scripts/run_premarket_digest.py --send --allow-outside-window \
  --session 2026-07-16 --allow-historical-send \
  --channel sector-rotation --rebuild-failed
```

## 8. 安全配置

在 Discord 中分别进入两个频道的“编辑频道 -> 整合 -> Webhooks”，各创建一个
Incoming Webhook。不要把 URL 粘贴到聊天、YAML、源码或 Git。

本机可运行隐藏输入向导；默认只验证 URL 格式，不发送测试消息：

```bash
python scripts/configure_premarket_discord.py
```

显式发送两条无 mention 测试消息：

```bash
python scripts/configure_premarket_discord.py --test-send
```

服务器为盘前 worker 使用独立密钥文件，避免 systemd 自动把两个新 Webhook 注入
refresh、group writer 和盘中 worker：

```bash
sudo install -d -m 0750 -o root -g quant /etc/quant
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/premarket-digest.env.example \
  /etc/quant/premarket-digest.env
sudo /opt/quant/.venv/bin/python /opt/quant/scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env --test-send
sudo chown root:quant /etc/quant/premarket-digest.env
sudo chmod 0640 /etc/quant/premarket-digest.env
```

上线前需人工确认两个频道分别恰好收到一条不含 mention 的测试消息。未确认前不得启用 timer。独立 env 文件是运行时
注入隔离，不是强安全边界：这些 unit 同属 `quant` 用户/组，`root:quant 0640` 的文件仍可被
该组读取。需要强隔离时，应使用独立 service user、专用 ACL/组或 systemd credentials。

环境文件最终包含：

```text
PREMARKET_DIGEST_ENABLED=true
DISCORD_MOMENTUM_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_SECTOR_ROTATION_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_MOMENTUM_ROLE_ID=
DISCORD_SECTOR_ROTATION_ROLE_ID=
```

本地 `.env.local` 迁移时，momentum 在显式新变量为空时可回退到旧
`DISCORD_WEBHOOK_URL`；独立服务器文件应始终使用新变量。sector
绝不回退，避免误投到动量频道。异常、日志、SQLite、preview 和 CLI JSON 均不得出现
完整 URL 或 token。怀疑泄漏时应立即删除并重建对应 Webhook。

## 9. Dry-run、正式安装和运维

默认不联系 Discord，会生成 JSON 与 Markdown preview：

```bash
python scripts/run_premarket_digest.py --session 2026-07-16
python scripts/run_premarket_digest.py --session 2026-07-16 --channel momentum
python scripts/run_premarket_digest.py --session 2026-07-16 --channel sector-rotation
```

服务器 dry-run 通过 Python 安全解析环境文件，不要用 shell `source`：

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env --session 2026-07-16
```

preview 位于：

```text
outputs/premarket_digest/dry_runs/<T>/
```

正式历史 session 发送必须同时写出危险确认开关，防止误发旧日报：

```bash
python scripts/run_premarket_digest.py --send --session 2026-07-16 \
  --allow-historical-send --allow-outside-window --channel momentum
```

任何非 systemd 的手工 `--send` 都必须显式加 `--allow-outside-window`；正常定时任务使用
`--send --scheduled`，自动推导当天 session，不能同时传 `--session`。

安装 timer 前，先确认 `US_LIQUID_5M` 正式版本契约、`T-1` 动量精确/可计算覆盖，以及
sector/sub-industry 两级正式产物门槛均通过。当前 unit 显式固定 `--channel all`；单个频道失败时
按各自状态记录并返回非零，由 systemd 在发送窗口内重试。通过后执行：

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
sudo install -m 0644 deploy/systemd/quant-premarket-digest.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/quant-premarket-digest.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/quant-premarket-digest.service \
  /etc/systemd/system/quant-premarket-digest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now quant-premarket-digest.timer
sudo systemctl start quant-premarket-digest.service
sudo systemctl status quant-premarket-digest.timer quant-premarket-digest.service
sudo journalctl -u quant-premarket-digest.service -n 200 --no-pager
```

退出码：

| code | 含义 | systemd 行为 |
|---:|---|---|
| 0 | 已发送、已幂等发送、dry-run、休市/窗口外正常跳过 | 不重试 |
| 1 | 数据门槛暂未通过、明确可重试的投递失败、部分失败 | 3 分钟后重试 |
| 2 | 功能未启用、Webhook 缺失/无效、payload 永久错误 | 不自动重试 |
| 3 | Discord 接收结果不确定，可能已经创建消息 | 不自动重试 |

混合失败时，只要任一频道仍是 `FAILED_RETRYABLE`，整体优先返回 1；该频道补发完成后，
才由另一个频道的永久错误或 `UNKNOWN` 返回 2/3。这样一个坏频道不会剥夺另一个频道的
安全重试机会。

服务器迁移必须单活：先在旧机停 timer 并确认 service inactive，再用 SQLite backup API
或停机后的 checkpoint 完整复制 outbox（不能只拷仍有 WAL 的主文件），恢复 `quant:quant`
所有权和 0600 权限后才在新机启 timer。两台机器同时运行无法由本地 `flock`/SQLite 防重。

## 10. 与现有盘中小时提醒的关系

`quant-momentum-alerts.timer` 仍是 10:00–15:59 ET 的盘中小时扫描；盘前 timer 是 09:20 ET
的上一收盘动量日报。分钟 `--auto` worker 另有独立 outbox 和五日晋级状态，三者状态互不复用。

- 希望 `#momentum-alerts` 同时有盘前摘要和盘中升级提醒：保留旧 timer；
- 希望该频道严格每天只有一条消息：部署新 timer 后执行
  `sudo systemctl disable --now quant-momentum-alerts.timer`；
- 不要通过共享 outbox 或修改盘中 `AlertStateStore` 来强行合并两种不同语义。
