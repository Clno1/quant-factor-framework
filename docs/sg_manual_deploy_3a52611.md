# SG 手工部署指引：3a52611

更新日期：2026-08-09

本指引把本地提交 `3a52611` 部署到 `root@<SG_IP>:/home/projects/quant`。发布包只含 Git
跟踪的代码、配置模板和文档；不会包含本地 `data`、`outputs`、`logs`、虚拟环境或密钥。

## 1. 从 Mac 上传发布包

发布包已经生成：

```text
/private/tmp/quant-3a52611.tar.gz
SHA-256 2adccb5af1689dfafc50ac31d657256e81820200c5345bb10003fe583047fa2c
```

在 Mac 终端执行：

```bash
export SG_IP='<SG 公网 IP>'
scp /private/tmp/quant-3a52611.tar.gz root@"$SG_IP":/tmp/
ssh root@"$SG_IP"
```

以下命令都在 SG 中执行。

## 2. 进入维护窗口并备份

先停止定时触发和 Web。若第二条命令仍显示运行中的 Quant oneshot，等待其自然结束，再继续备份。

```bash
set -euo pipefail
systemctl stop 'quant-*.timer' || true
systemctl stop quant-web.service || true
systemctl list-units 'quant-*.service' --state=running --no-pager
if systemctl list-units 'quant-*.service' --state=running --no-legend | grep -q .; then
  echo '仍有 Quant service 运行，请等待其结束后重新执行本段。' >&2
  exit 1
fi
```

创建完整一致性备份，不删除任何旧数据：

```bash
TS=$(date +%Y%m%dT%H%M%S%z)
BACKUP="/home/projects/quant-backups/$TS"
mkdir -p "$BACKUP/project" "$BACKUP/etc-quant" "$BACKUP/systemd"
rsync -aHAX --numeric-ids /home/projects/quant/ "$BACKUP/project/"
rsync -aHAX --numeric-ids /etc/quant/ "$BACKUP/etc-quant/"
cp -a /etc/systemd/system/quant-* "$BACKUP/systemd/" 2>/dev/null || true
printf '%s\n' "$BACKUP"
```

## 3. 验证并安装代码

```bash
echo '2adccb5af1689dfafc50ac31d657256e81820200c5345bb10003fe583047fa2c  /tmp/quant-3a52611.tar.gz' | sha256sum -c -
RELEASE="/tmp/quant-release-3a52611-$TS"
mkdir -p "$RELEASE"
tar -xzf /tmp/quant-3a52611.tar.gz -C "$RELEASE"
test -f "$RELEASE/scripts/run_mvp.py"
```

先预览将删除的旧代码。`data/outputs/logs/runlog/.venv/.env.local/.git` 均受保护：

```bash
rsync -ani --delete \
  --exclude='.git/' --exclude='.env.local' --exclude='.venv/' --exclude='.venv-worker/' \
  --exclude='data/' --exclude='outputs/' --exclude='logs/' --exclude='runlog/' \
  --exclude='.deploy-commit' "$RELEASE/" /home/projects/quant/
```

确认预览只涉及代码、模板、测试和文档后执行正式同步：

```bash
rsync -a --delete \
  --exclude='.git/' --exclude='.env.local' --exclude='.venv/' --exclude='.venv-worker/' \
  --exclude='data/' --exclude='outputs/' --exclude='logs/' --exclude='runlog/' \
  --exclude='.deploy-commit' "$RELEASE/" /home/projects/quant/
printf '%s\n' '3a52611' > /home/projects/quant/.deploy-commit
cd /home/projects/quant
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

预期完整测试为 `409 passed`。Linux 依赖版本造成的弃用 warning 可以记录，但失败不能忽略。

## 4. 安装并校验 systemd

保留现有 `/etc/quant/*.env`。确认 `market-data.env` 有 FMP key、`web.env` 有
`QUANT_WEB_AUTH_USER=quant` 和正确密码，不要把内容输出到日志。

```bash
for src in deploy/systemd/quant-*-root.service; do
  dst=$(basename "$src" | sed 's/-root\.service$/.service/')
  install -m 0644 "$src" "/etc/systemd/system/$dst"
done
install -m 0644 deploy/systemd/quant-*.timer /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/quant-*.service /etc/systemd/system/quant-*.timer
```

## 5. PIT 门禁与正式重发

统一解析本次应发布的 XNYS 交易日：

```bash
TARGET=$(.venv/bin/python -c 'from src.utils.market_calendar import latest_publishable_xnys_session as f; print(f().date())')
echo "$TARGET"
```

先检查 NASDAQ100，不要跳过 candidate：

```bash
if .venv/bin/python scripts/run_data_pipeline.py pit --universe NASDAQ100 \
  --target-session "$TARGET" --candidate-only --env-file /etc/quant/market-data.env --json \
  | tee "/tmp/nasdaq100-candidate-$TARGET.json"; then
  NASDAQ_RC=0
else
  NASDAQ_RC=$?
fi
echo "NASDAQ100 candidate exit=$NASDAQ_RC"
```

截至 2026-08-09 已知 Nasdaq 官方为 `HONA`、FMP 为 `EA`。若仍不一致，不得发布 NASDAQ100，
也不得手工修改名单。继续独立发布 SP500 和 MAG7：

```bash
.venv/bin/python scripts/run_data_pipeline.py pit --universe SP500 \
  --target-session "$TARGET" --env-file /etc/quant/market-data.env --json
.venv/bin/python scripts/run_data_pipeline.py update --universe SP500 --universe MAG7 \
  --target-session "$TARGET" --workers 6 --force --env-file /etc/quant/market-data.env --json
.venv/bin/python scripts/run_factor_research.py --universe SP500 --universe MAG7 \
  --target-session "$TARGET" --force --env-file /etc/quant/market-data.env --json
```

若 `NASDAQ_RC` 非零，安装临时 systemd drop-in，避免 NASDAQ100 阻断使两个成功池的日常任务
被标红并反复重试：

```bash
mkdir -p /etc/systemd/system/quant-market-data.service.d
cat > /etc/systemd/system/quant-market-data.service.d/blocked-nasdaq100.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/home/projects/quant/.venv/bin/python /home/projects/quant/scripts/run_data_pipeline.py update --universe SP500 --universe MAG7 --workers 6 --env-file /etc/quant/market-data.env
EOF
mkdir -p /etc/systemd/system/quant-factor-research.service.d
cat > /etc/systemd/system/quant-factor-research.service.d/blocked-nasdaq100.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/home/projects/quant/.venv/bin/python /home/projects/quant/scripts/run_factor_research.py --universe SP500 --universe MAG7
EOF
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/quant-market-data.service /etc/systemd/system/quant-factor-research.service
```

只有 NASDAQ100 candidate 全部通过时，才追加正式 PIT、行情和研究：

```bash
.venv/bin/python scripts/run_data_pipeline.py pit --universe NASDAQ100 --target-session "$TARGET" --env-file /etc/quant/market-data.env --json
.venv/bin/python scripts/run_data_pipeline.py update --universe NASDAQ100 --target-session "$TARGET" --workers 6 --force --env-file /etc/quant/market-data.env --json
.venv/bin/python scripts/run_factor_research.py --universe NASDAQ100 --target-session "$TARGET" --force --env-file /etc/quant/market-data.env --json
rm /etc/systemd/system/quant-market-data.service.d/blocked-nasdaq100.conf
rm /etc/systemd/system/quant-factor-research.service.d/blocked-nasdaq100.conf
systemctl daemon-reload
```

最后三行只在 NASDAQ100 三条正式命令全部成功后执行；失败时保留 drop-in。

## 6. 存储、Web 与日志验收

```bash
.venv/bin/python scripts/run_data_pipeline.py status --json
.venv/bin/python scripts/check_app_storage.py
.venv/bin/python -c "from src.data.foundation import MarketDataReader; r=MarketDataReader(); [print(u, r.require_latest(u).version_id) for u in ('SP500','MAG7')]"
systemctl enable --now quant-web.service
systemctl restart quant-web.service
```

不打印密码地加载 Web 凭证并检查核心入口：

```bash
set -a; source /etc/quant/web.env; set +a
for path in research research/factor-data research/universes/SP500 strategies watchlists backtests paper; do
  curl -sS --fail -u "$QUANT_WEB_AUTH_USER:$QUANT_WEB_AUTH_PASSWORD" \
    -o /dev/null -w "$path HTTP=%{http_code} time=%{time_total}s\n" \
    "http://127.0.0.1:18823/$path"
done
curl -sS --fail -u "$QUANT_WEB_AUTH_USER:$QUANT_WEB_AUTH_PASSWORD" \
  'http://127.0.0.1:18823/api/research/factor-data/meta'
```

因子数据性能抽查：

```bash
URL='http://127.0.0.1:18823/api/research/factor-data/snapshot?universe=SP500&factor=MOM_12M&date=latest&ticker=AAPL'
systemctl restart quant-web.service; sleep 2
curl -sS --fail -u "$QUANT_WEB_AUTH_USER:$QUANT_WEB_AUTH_PASSWORD" -o /dev/null -w 'cold=%{time_total}s\n' "$URL"
curl -sS --fail -u "$QUANT_WEB_AUTH_USER:$QUANT_WEB_AUTH_PASSWORD" -o /dev/null -w 'hot=%{time_total}s\n' "$URL"
```

冷查询应小于 2 秒，热查询应小于 0.5 秒。最后检查服务和错误日志：

```bash
systemctl status quant-web.service --no-pager
journalctl -u quant-web.service -u quant-market-data.service -u quant-factor-research.service -n 200 --no-pager
systemctl --failed --no-pager
```

确认上述检查通过后再恢复定时器：

```bash
systemctl enable --now \
  quant-us-daily-refresh.timer quant-market-data.timer quant-factor-research.timer \
  quant-group-analytics-eod.timer quant-paper-trading.timer quant-data-requests.timer \
  quant-premarket-digest.timer quant-momentum-alerts.timer quant-intraday-momentum-monitor.timer
systemctl list-timers 'quant-*' --all --no-pager
```

NASDAQ100 未通过时，跨池结论显示 `INSUFFICIENT` 是正确状态，但 SP500/MAG7 页面必须可用。
不要删除备份、旧 Parquet、DuckDB 或 SQLite。
