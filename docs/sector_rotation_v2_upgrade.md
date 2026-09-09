# 板块轮动 V2：实现、验收与升级操作

实施日期：2026-09-09。依据：`sector_rotation_v2_public_source_revision.md`。

本次是原板块分析领域的原地升级，不是新的交易引擎。代码实现不等于已经完成作者数据对账、策略收益验证或 SG 部署。没有自动买卖、仓位修改，也没有因开发验收向 Discord 发送消息。

## 1. 本次交付与仍待验证的部分

| 用户要求的阶段 | 已实现 | 仍需真实数据验收 |
|---|---|---|
| 公开基础版对账 | 原11主题、日等权篮子、五项积分、状态优先级、趋势文字、严格越线事件、冻结输入重放 | TradingView 同日/同源/同复权逐值对账；不是13主题高级截图复刻 |
| 主站生产候选 | 中文主入口、科技/全市场两个观察范围、5/20/60日强弱、状态确认、真实广度门槛、风险标签、详情与历史曲线 | ETF真实持仓广度、供应商量价复权核验、阈值与预测性验证 |
| 连接动量突破 | 上层只读调用既有完成日扫描，按同日股票/板块关联，保留原分数、参考位、股票池及详情链接 | SG 的 US_ACTIVE 刷新覆盖是否合格；主题ETF未登记成分时不臆造关联 |
| 日度背景实验 | 授权证据 JSON 导入、已知时点/年龄校验、独立家族计数、价格响应标签 | 宏观原始序列转换规则及数据许可、历史vintage、相对无背景模型的增量验证 |

当前没有上线自动 FRED/ICE 抓取、NQ v4、15/60/240 分钟模型、统计残差或作者未知的“余量”模型。背景未接入时明确留空；不会为截图完整而生成虚构数值。

## 2. 目录与依赖

```text
src/group_analytics/rotation/
  themes.py       版本化主题/代理/成员登记
  engine.py       两条profile与日线状态重放
  service.py      CLI计算编排；独立价格缓存
  store.py        不可变产物、校验和、成功指针
  replay.py       从冻结输入重放对账（不联网、不发布）
  context.py      可选日度背景/价格响应实验

src/premarket_digest/rotation.py   上层个股关联与中文Discord编排
src/webapp/group_analytics_routes.py  主站只读API
scripts/run_group_rotation.py    新日度构建入口
scripts/audit_group_rotation.py  只读重放入口
```

核心 factors、backtest、papertrading、breakouts、alerts 等不导入 group_analytics。领域内部也不导入这些策略/应用模块。既有数据适配器是单向被读取的依赖；个股关联位于上层，不向突破扫描器加入轮动条件。

新主页面 `/group-analytics`；旧单日统计保留在 `/group-analytics/daily`，旧 heat/groups API 保持兼容。新版 writer 不再自动更新旧单日榜；如确实需要旧榜继续更新，可独立运行原 `run_group_analytics.py --level all`。

## 3. 两条算法轨道

共同 schema 为 `rotation.v2.1`，profile 分别为 `source_v1_compat`、`research_v2`。

### 3.1 来源兼容轨道

公开11主题相对 QQQ；额外11个行业ETF相对 SPY，是本项目扩展，详情明确标注不是作者原11主题。两个范围不混排。

`q=主题价格/基准价格`；`RS_k=100×(q_t/q_(t-k)-1)`。篮子每个交易日等权重算，累计为研究指数。原版保留有效成员平均、全缺失时0收益、历史数据向前映射等对照行为。

| 分项 | 原规则 |
|---|---|
| 趋势30 | q>MA20、q>MA50、MA20>5期前MA20，各10分 |
| 改善25 | RS5>0加8；RS5>RS20/4加9；RS5>5期前RS5加8 |
| 量能20 | 相对日收益>0且金额倍数≥1.3加20，否则倍数≥1加10 |
| 广度15 | ≥70加15；否则≥50加8；ETF保留70/30趋势代理 |
| 未延伸10 | RS60未超过18%，且q距MA50未超过8%，加10 |

金额倍数是当前close×volume（篮子求和）除以含当日的20期均值。不是净资金流。

状态严格依序：无数据 → 派发条件 → 已延伸且分数≥60 → 分数≥75 → 分数≥60 → 分数<45且RS5/20均负 → 中性。原版来源标签仅在对照详情使用。

保存 `source_cross_up_60/75`、`source_cross_down_45`。当前严格越过、上一期含等号；与 `source_state` 和真正的消息投递事件分开。

### 3.2 生产候选轨道

1. 以 XNYS 真实交易日对齐，最多最近300个完整收盘点。休市不算一天，未来/未收盘日期拒绝，提前收盘按官方日历。
2. 生产数据不向前补价。篮子只要当日任何登记成员缺失，即断开指数；恢复后重新开始有效分段。必需的61点连续窗口不足，不输出合格状态。
3. 广度分母要求成员当日价格和自己的20日均线均有效。ETF无登记持仓时广度为缺失，绝不把70解释成70%成员确认。
4. 存储、云巨头等3/4只篮子仍显示价格和成员；少于5只不进入“广度确认优先区”。
5. `amount_verified=false` 是默认值。未核验供应商量价复权前，生产金额字段、量能倍数与总分都停用。不能为了补总分而开启 `--amount-verified`。

生产状态的透明候选公式：

```text
X = ln(1 + RS20/100)
Y = ln(1 + RS5/100) - [X - ln(1 + RS5/100)] / 3
```

Y 比较最近5日与此前15日的每5日平均相对速度，避免把原版 RS20/4 冒充精确前期速度。

| X方向 | Y方向 | 候选状态 |
|---|---|---|
| 正 | 正 | 领先且加速 |
| 正 | 负 | 领先但降温 |
| 负 | 正 | 落后但改善 |
| 负 | 负 | 落后且减速 |

X绝对值≤0.005或Y绝对值≤0.002时进入边界带，不新增确认；浮点等号有小容差。新候选连续两个完整日线点才替换已确认状态。边界/切换待确认期间保留旧状态供解释，但研究优先级降为等待。数据中断则清空确认，不把缺失解释为反向信号。重复运行不会增加计数。

状态以窗口完整重放计算后写入不可变快照，不建立跨进程可变交易状态。`state_since_scope=replay_window`；页面显示“本次回放窗口内确认起点”，不是声称知道300日以前的真实首次发生日期。跨版本或价格修订可能改变重放结果，旧快照仍保留。

研究优先级按以下顺序产生：

- 数据不足 → 数据不足。
- 过度延伸 → 延伸偏大，不追涨。
- 边界/尚有待确认新候选 → 等待确认。
- 已确认领先、绝对20日收益>0且价格>绝对MA20 → 价格领先；若有效成员≥5、覆盖≥80%、广度≥60%，升级为“优先核对”。
- 已确认改善且绝对5日收益>0 → 转强观察。
- 降温/落后或绝对趋势不合格 → 降温或落后。
- 其他 → 等待确认。

这些阈值是待验证的研究参数，不是概率或仓位系数。当前主表默认按RS20排序，可切RS5/60，不按未经验证的总分排序。

## 4. 个股关联与盘前输出

构建脚本只读调用 `CompletedSessionMomentumSource`，使用现有突破算法与完成日缓存；不改其分数和状态。扫描或缓存质量门槛失败，仅关闭个股关联，不伪造候选。

关联必须满足报告日期、个股 `data_date` 与轮动日期一致；状态属于 BREAKOUT/READY/SETUP，价格/突破位有效，并按ticker去重。篮子按登记成员连接；全市场行业按FMP同板块连接，明确标注“非ETF持仓名单”。每主题最多3只，保留原扫描分、参考位与股票池链接。

原扫描的同日最后收盘视角、网页个股详情的后续实时诊断是两个时点。点击链接不会把历史候选变成已经获得盘中确认的交易。

Discord正式渲染使用同一不可变快照，科技与全市场分组，每组至多2个观察方向，并列出风险/失效条件、既有个股与参考位、主站固定run链接。没有合格方向就说等待，不强制每天选股。现有角色提醒配置及日期去重保留。

一旦 rotation 目录出现成功或失败尝试，日报切换到新版。新版缺失、陈旧、损坏时失败关闭，不偷偷换回旧单日涨跌榜。快照必须恰为盘前目标的上一完成交易日、FINAL，且有效主题达到80%（默认22个中至少18个）；历史生成时间不能来自未来。

## 5. 产物与对账

默认目录：`outputs/group_analytics/rotation/`。

- `runs/rot_日期_哈希.json`：不可变快照，保存两个profile、参数、成员版本、最新与120期详情、私有冻结输入面板、输入哈希、候选和背景状态。
- `latest.json`：原子更新的最后成功指针，不能被更早日期回退。
- `last_attempt.json`：最后成功/失败状态。失败不覆盖成功快照；主站对最新读取给出失败提醒。
- 数据缓存：`data/reference/group_analytics/rotation/`；共享 `data/raw/ohlcv` 仅作只读回退。

重放不联网、不更新缓存、不发布、不发送：

```bash
.venv/bin/python scripts/audit_group_rotation.py
.venv/bin/python scripts/audit_group_rotation.py --run-id 实际快照编号
```

返回 MATCH 表示本项目当前代码可从该次冻结输入复现最新中间值，不表示作者数据源一致或已经证明未来收益。输入和产物均校验；原始输入面板不暴露给Web API。

只读 API：`/api/group-analytics/rotation?run=...` 为精简总表；`/api/group-analytics/rotation/{theme_id}?run=...` 为详情。固定run链接不会随着下一次发布换数据；历史/日期不确定时有明确警告。浏览页面不调用供应商、不启动计算或发送。

## 6. 日度背景实验的使用边界

通过 `--context-file /path/authorized-evidence.json` 传入JSON数组。每条格式示例（纯格式演示，不是真实宏观信号）：

```json
{
  "id": "example-rates-v1",
  "family": "rates",
  "target_benchmark": "QQQ",
  "direction": "support",
  "strength": 0.8,
  "observed_at": "2026-09-04T20:00:00Z",
  "published_at": "2026-09-07T20:15:00Z",
  "first_seen_at": "2026-09-07T20:16:00Z",
  "publish_allowed": true,
  "source": "经过核验的来源标识，不放密钥",
  "rule_version": "经过审计并冻结的转换规则版本"
}
```

family允许 rates/dollar/credit/growth/inflation/liquidity/events。strength必须是调用方可解释、已审计的[0,1]证据强度，不是此模块凭行情自动训练生成。要把美元变化转换成strength，必须另提供公开、可验证的规则及数据，不能手工凑结论。

默认截止为源交易日正式收盘；可用 `--decision-cutoff` 指定带时区、介于该次收盘与当前时间之间的时刻。观察、发布、首次已知时间不合格或证据超过7个自然日、许可未确认就拒绝。默认至少两个独立家族，且目标基准一致；同一家族不因重复条目重复加票。单侧强度≥0.7算该家族过线，缺失不是0利空或0利多。存在拒绝项时整层保守标为证据不足。

只输出支持/压制/混合/不足，以及与目标主题绝对5日价格方向的相容标签。跨基准证据不套用。实验不修改主题积分、个股扫描或仓位；未实现宏观冠军迟滞、作者百分制成色、样本外残差或预测收益。

## 7. SG服务器：继续用root部署

以下是操作说明，本次没有远程执行。沿用 `/home/projects/quant` 和 `.venv`，不需要conda，也不要求创建quant用户。

### 7.1 更新代码后，先只构建和验收

```bash
cd /home/projects/quant
.venv/bin/python -m pip install -r requirements.txt

# 拉取登记主题价格，计算但不发布；--refresh会更新轮动自己的价格缓存。
.venv/bin/python scripts/run_group_rotation.py --refresh --asof latest --dry-run \
  --env-file /etc/quant/momentum-alerts.env

# 上一条成功后正式生成快照；此命令仍然不发送Discord。
.venv/bin/python scripts/run_group_rotation.py --refresh --asof latest \
  --env-file /etc/quant/momentum-alerts.env
.venv/bin/python scripts/audit_group_rotation.py

# 只预览下一开盘日的板块摘要；没有--send，不联系Discord。
.venv/bin/python scripts/run_premarket_digest.py --channel sector-rotation \
  --env-file /etc/quant/premarket-digest.env
```

若你实际使用不同env路径，沿用现有服务中的路径，不要新建或覆盖已有密钥文件。FMP凭据只给writer所需环境，Discord Webhook只给既有投递worker。检查日志时不要粘贴凭据内容。

若writer成功但 `candidate_linkage=unavailable`，先检查既有 `quant-us-daily-refresh.service` 的完成日US_ACTIVE缓存/manifest及覆盖率；不能把它理解为“所有股票没有机会”。恢复后重新构建轮动可生成新的关联快照。

### 7.2 修改现有systemd writer，不重复建计时任务

仓库里的 `quant-group-analytics-eod.service` 已改用新脚本，但服务器可能有原先root版drop-in覆盖ExecStart；只拉代码或覆盖主unit不保证实际启动命令变化。

先检查非密钥配置：

```bash
systemctl show quant-group-analytics-eod.service \
  -p User -p Group -p WorkingDirectory -p ExecStart -p DropInPaths
```

用 `systemctl edit quant-group-analytics-eod.service` 编辑现有覆盖配置，保留已有环境文件和重试设置。需要的root/path字段如下；`ExecStart=`空行先清除旧命令：

```ini
[Service]
User=root
Group=root
WorkingDirectory=/home/projects/quant
Environment=PYTHONPATH=/home/projects/quant
Environment=GROUP_ANALYTICS_ENABLED=true
ProtectHome=read-only
ReadWritePaths=/home/projects/quant/data /home/projects/quant/outputs /home/projects/quant/logs
ExecStartPre=
ExecStartPre=/home/projects/quant/.venv/bin/python -c "import sys; assert sys.version_info >= (3, 11)"
ExecStart=
ExecStart=/home/projects/quant/.venv/bin/python /home/projects/quant/scripts/run_group_rotation.py --refresh --asof latest
```

这些目录须已经存在。若现有unit有额外写目录保护或其他drop-in，按实际有效配置合并，不删除无关保护。主模板默认仍是通用 `/opt/quant`/quant用户；SG使用上面的既有root覆盖。

```bash
systemctl daemon-reload
systemctl show quant-group-analytics-eod.service -p ExecStart -p User -p WorkingDirectory
systemctl start quant-group-analytics-eod.service
systemctl status quant-group-analytics-eod.service --no-pager
journalctl -u quant-group-analytics-eod.service -n 80 --no-pager
systemctl list-timers --all 'quant-*'
```

已有轮动构建timer和美东09:20投递timer继续使用；不用另建一套。本仓库writer timer为新加坡周二至周六07:45，先等已有US行情刷新完成。`After=`只是顺序关系，不会主动启动或保证刷新成功；个股关联自身仍会做完成日门槛。

主站重启要用你现有的网站服务名。本次默认配置开启只读轮动入口，但若网站环境保留 `GROUP_ANALYTICS_ENABLED=false` 或 `GROUP_ANALYTICS_WEB_ENABLED=false`，需要把网站进程中的这两个开关改为true。writer的WEB_ENABLED=false不影响另一进程的主站配置。

### 7.3 正式投递与回退注意

主站、对账、JSON/Markdown预览均检查后，再让既有投递timer执行。不要为了测新版就删除SQLite去重记录：同日已经SENT仍不会重复发，UNKNOWN必须人工确认，FAILED重建沿用旧的显式恢复流程，详见 `premarket_discord.md`。

需要人工发送时，仍使用既有 `run_premarket_digest.py --send --allow-outside-window --channel sector-rotation` 授权开关和原有env文件；该命令会真实发送，本文的默认验收步骤不执行它。

回退可以先隐藏主站入口/暂停轮动writer，保存所有产物及投递数据库，再恢复旧代码和writer入口。不要通过删除rotation目录制造静默回退；要回旧版需要完整版本回退并显式验证旧榜日期，不让旧统计冒充新报告。

## 8. 验证记录与后续研究准入

本地全量命令 `.venv/bin/python -m pytest -q --disable-warnings`：187项测试通过，56个子测试通过。JavaScript语法检查及 `git diff --check` 通过。

自动测试覆盖：原版积分/状态优先级、越线事件、ETF伪广度隔离、全缺失/中间缺日、未来数据隔离、日历休市/未收盘、两bar确认/边界浮点、重复重放、输入指纹、不可变产物及回退指针、候选同日关联/降级、背景时点与许可、角色提醒、API只读/输入隐藏、旧接口及核心零反向依赖。

浏览器使用隔离临时目录的合成行情完成桌面与390px手机验收，包括范围切换、主题详情、股票池链接、图表、返回列表和错误日志；模拟数据未写入项目生产outputs，未发送到Discord。

上线后仍须先采集同日TradingView对照与真实冻结快照。未来5/20日收益、相同有效样本的RS20/RS60基线、原版积分消融、背景增量、成本/PIT/重叠收益检验尚未执行。没有这些结果前，这一版的承诺是“可读、可核对的日线研究工具”，不是“已经验证能选中下一轮赢家”。
