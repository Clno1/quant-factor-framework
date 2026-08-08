(function () {
    const app = document.getElementById('decision-replay-app');
    if (!app) return;

    const state = {
        kind: app.dataset.sourceKind,
        id: app.dataset.sourceId,
        meta: null,
        payload: null,
        mode: 'all',
        expanded: new Set(),
        dateRequest: 0,
        dateController: null,
        stockRequest: 0,
        stockController: null,
    };

    const apiRoot = state.kind === 'backtest'
        ? `/api/backtests/${encodeURIComponent(state.id)}/decision-replay`
        : `/api/paper/${encodeURIComponent(state.id)}/decision-replay`;

    const el = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    function number(value, digits = 2) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return Number(value).toLocaleString('zh-CN', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        });
    }

    function compactNumber(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value));
    }

    function pct(value, digits = 2) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return `${(Number(value) * 100).toFixed(digits)}%`;
    }

    function signedClass(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '';
        return Number(value) < 0 ? 'neg' : Number(value) > 0 ? 'pos' : '';
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, options);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
        return payload;
    }

    function activeDates() {
        if (!state.meta) return [];
        return state.mode === 'decision' ? state.meta.decision_dates : state.meta.dates;
    }

    function setLoading(loading, message = '') {
        const node = el('replay-loading');
        node.hidden = !loading;
        if (message) node.textContent = message;
    }

    function renderTimeline() {
        if (typeof Plotly === 'undefined' || !state.meta) return;
        const timeline = state.meta.timeline || [];
        const dates = timeline.map((row) => row.date);
        const nav = timeline.map((row) => row.nav);
        const decisionRows = timeline.filter((row) => row.is_rebalance);
        const traces = [{
            x: dates,
            y: nav,
            type: 'scatter',
            mode: 'lines',
            name: state.kind === 'paper' ? '账户净值' : '策略净值',
            line: { color: '#42A5F5', width: 2 },
            hovertemplate: '%{x}<br>净值 %{y:.4f}<extra></extra>',
        }, {
            x: decisionRows.map((row) => row.date),
            y: decisionRows.map((row) => row.nav),
            type: 'scatter',
            mode: 'markers',
            name: '调仓日',
            marker: { color: '#FFB300', size: 7, symbol: 'diamond' },
            hovertemplate: '%{x}<br>调仓日<extra></extra>',
        }];
        const layout = {
            height: 210,
            margin: { l: 48, r: 24, t: 18, b: 36 },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#9AA0A6', size: 11 },
            xaxis: { gridcolor: 'rgba(255,255,255,.05)' },
            yaxis: { gridcolor: 'rgba(255,255,255,.05)', title: 'NAV' },
            showlegend: true,
            legend: { orientation: 'h', x: 0, y: 1.16 },
            hovermode: 'x unified',
        };
        Plotly.newPlot('replay-timeline', traces, layout, {
            responsive: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['lasso2d', 'select2d'],
        });
        const timelineNode = el('replay-timeline');
        timelineNode.on('plotly_click', (event) => {
            const date = event?.points?.[0]?.x;
            if (date) requestDate(String(date).slice(0, 10));
        });
    }

    function markTimelineDate(date) {
        if (typeof Plotly === 'undefined' || !el('replay-timeline')) return;
        Plotly.relayout('replay-timeline', {
            shapes: [{
                type: 'line',
                x0: date,
                x1: date,
                y0: 0,
                y1: 1,
                yref: 'paper',
                line: { color: '#00C853', width: 1, dash: 'dot' },
            }],
        });
    }

    function renderSummary(summary) {
        const items = [
            ['日期', summary.date],
            ['股票池', summary.universe_count ?? '—'],
            ['合格股票', summary.eligible_count ?? '—'],
            ['信号 Top 组', summary.signal_top_count ?? '—'],
            ['实际持有', summary.held_count ?? '—'],
            ['策略当日收益', pct(summary.net_return)],
            ['净值', number(summary.nav, 4)],
            ['当日交易成本', summary.total_cost_cash == null ? '—' : `$${number(summary.total_cost_cash, 2)}`],
        ];
        el('replay-summary').innerHTML = items.map(([label, value]) => `
            <div>
                <span>${escapeHtml(label)}</span>
                <strong class="mono">${escapeHtml(value)}</strong>
            </div>
        `).join('');

        const dayType = el('replay-day-type');
        dayType.textContent = summary.is_rebalance ? '决策日' : '观察日';
        dayType.className = `status-pill ${summary.is_rebalance ? 'is-decision' : 'is-observation'}`;
        el('replay-execution-date').textContent = summary.execution_date
            ? `执行日 ${summary.execution_date}`
            : '';
    }

    function actionClass(action) {
        if (action === '买入' || action === '调增') return 'is-buy';
        if (action === '卖出' || action === '调减') return 'is-sell';
        if (action === '排除') return 'is-excluded';
        return 'is-neutral';
    }

    function factorDetail(row) {
        const execution = row.execution;
        const factors = (row.factors || []).map((factor) => `
            <tr>
                <td class="mono">${escapeHtml(factor.factor_id)}</td>
                <td class="mono num">${pct(factor.weight)}</td>
                <td class="mono num">${number(factor.raw, 6)}</td>
                <td class="mono num">${number(factor.clean, 4)}</td>
                <td class="mono num">${number(factor.strategy_input, 4)}</td>
                <td class="mono num">${number(factor.contribution, 4)}</td>
            </tr>
        `).join('');
        const executionLine = execution ? `
            <div class="replay-execution-audit">
                <span>${escapeHtml(execution.side || '')} ${number(execution.quantity, 2)} 股</span>
                <span>执行日 <b class="mono">${escapeHtml(execution.execution_date || '待成交')}</b></span>
                <span>原始价 <b class="mono">${number(execution.raw_price, 4)}</b></span>
                <span>成交价 <b class="mono">${number(execution.fill_price, 4)}</b></span>
                <span>滑点 <b class="mono">${number(execution.slippage_bps, 2)} bps</b></span>
                <span>费用 <b class="mono">$${number(execution.fee, 2)}</b></span>
            </div>
        ` : '';
        return `
            <tr class="replay-factor-row" data-detail-for="${escapeHtml(row.ticker)}">
                <td colspan="17">
                    <div class="replay-factor-detail">
                        <table>
                            <thead>
                                <tr>
                                    <th>因子</th>
                                    <th class="num">权重</th>
                                    <th class="num">原始值</th>
                                    <th class="num">清洗值</th>
                                    <th class="num">策略输入</th>
                                    <th class="num">分数贡献</th>
                                </tr>
                            </thead>
                            <tbody>${factors}</tbody>
                        </table>
                        ${executionLine}
                    </div>
                </td>
            </tr>
        `;
    }

    function renderTable(rows) {
        state.expanded.clear();
        const body = el('replay-table-body');
        body.innerHTML = rows.map((row) => {
            const primary = row.factors?.[0] || {};
            const rawHint = primary.raw == null
                ? ''
                : `<small>${escapeHtml(primary.factor_id)} raw ${number(primary.raw, 4)}</small>`;
            const eligibility = row.eligible
                ? '<span class="replay-eligibility is-pass">合格</span>'
                : `<span class="replay-eligibility is-fail" title="${escapeHtml(row.exclusion_reason)}">排除</span>`;
            const execution = row.execution;
            const executionStatus = execution
                ? `<span class="replay-exec-status is-${escapeHtml(execution.status || 'pending')}">${escapeHtml(execution.status || 'pending')}</span>`
                : (row.action === '持有' ? '无需交易' : '—');
            const target = row.target_weight == null ? '—' : pct(row.target_weight);
            return `
                <tr class="replay-stock-row ${row.eligible ? '' : 'is-excluded'}"
                    data-ticker="${escapeHtml(row.ticker.toLowerCase())}"
                    data-eligible="${row.eligible ? '1' : '0'}"
                    data-action="${escapeHtml(row.action)}"
                    data-group="${row.signal_group ?? ''}">
                    <td>
                        <button class="replay-expand icon-btn" type="button"
                                data-expand="${escapeHtml(row.ticker)}"
                                title="展开因子明细" aria-label="展开因子明细">+</button>
                    </td>
                    <td>
                        <button class="replay-ticker-link mono" type="button"
                                data-stock="${escapeHtml(row.ticker)}">${escapeHtml(row.ticker)}</button>
                    </td>
                    <td class="mono num">${number(row.close, 2)}</td>
                    <td class="mono num ${signedClass(row.daily_return)}">${pct(row.daily_return)}</td>
                    <td class="mono num">${compactNumber(row.volume)}</td>
                    <td class="mono num replay-score">${number(row.score, 4)}${rawHint}</td>
                    <td class="mono num">${row.rank ?? '—'}</td>
                    <td class="mono num">${pct(row.percentile, 1)}</td>
                    <td class="mono num">${row.signal_group == null ? '—' : `Q${row.signal_group}`}</td>
                    <td>${eligibility}</td>
                    <td class="mono num">${pct(row.held_weight)}</td>
                    <td class="mono num">${target}</td>
                    <td><span class="replay-action ${actionClass(row.action)}">${escapeHtml(row.action)}</span></td>
                    <td>${executionStatus}</td>
                    <td class="mono num post-event-cell">${number(execution?.fill_price, 4)}</td>
                    <td class="mono num post-event-cell ${signedClass(row.next_holding_return)}">${pct(row.next_holding_return)}</td>
                    <td class="mono num post-event-cell ${signedClass(row.portfolio_contribution)}">${pct(row.portfolio_contribution, 3)}</td>
                </tr>
                ${factorDetail(row)}
            `;
        }).join('');
        applyFilters();
    }

    function applyFilters() {
        const search = (el('replay-search').value || '').trim().toLowerCase();
        const eligibility = el('replay-eligibility').value;
        const action = el('replay-action').value;
        const group = el('replay-group').value;
        let visible = 0;
        document.querySelectorAll('.replay-stock-row').forEach((row) => {
            const isEligible = row.dataset.eligible === '1';
            const show = (!search || row.dataset.ticker.includes(search))
                && (!eligibility || (eligibility === 'eligible' ? isEligible : !isEligible))
                && (!action || row.dataset.action === action)
                && (!group || row.dataset.group === group);
            row.hidden = !show;
            const detail = row.nextElementSibling;
            if (detail?.classList.contains('replay-factor-row')) {
                const ticker = detail.dataset.detailFor;
                detail.hidden = !show || !state.expanded.has(ticker);
            }
            if (show) visible += 1;
        });
        const total = state.payload?.row_count ?? 0;
        el('replay-visible-count').textContent = `${visible} / ${total}`;
    }

    async function loadDate(date) {
        if (!date) return;
        state.dateController?.abort();
        const controller = new AbortController();
        const request = ++state.dateRequest;
        state.dateController = controller;
        setLoading(true, '正在载入决策快照…');
        try {
            const payload = await fetchJson(
                `${apiRoot}?date=${encodeURIComponent(date)}`,
                { signal: controller.signal },
            );
            if (request !== state.dateRequest) return;
            state.payload = payload;
            el('replay-date').value = payload.date;
            renderSummary(payload.summary);
            renderTable(payload.rows || []);
            updateStepper();
            markTimelineDate(payload.date);
            setLoading(false);
        } catch (error) {
            if (error.name === 'AbortError' || request !== state.dateRequest) return;
            setLoading(true, `载入失败：${error.message}`);
        }
    }

    function updateStepper() {
        const dates = activeDates();
        const current = state.payload?.date;
        const index = dates.indexOf(current);
        el('replay-prev').disabled = index <= 0;
        el('replay-next').disabled = index < 0 || index >= dates.length - 1;
    }

    function stepDate(direction) {
        const dates = activeDates();
        const current = state.payload?.date;
        const index = dates.indexOf(current);
        if (index < 0) {
            const target = direction < 0
                ? [...dates].reverse().find((date) => !current || date < current)
                : dates.find((date) => !current || date > current);
            if (target) loadDate(target);
            return;
        }
        const target = dates[index + direction];
        if (target) loadDate(target);
    }

    function requestDate(date) {
        const dates = activeDates();
        if (!dates.length || !date) return;
        const normalized = String(date).slice(0, 10);
        const target = dates.includes(normalized)
            ? normalized
            : [...dates].reverse().find((candidate) => candidate <= normalized)
                || dates[0];
        loadDate(target);
    }

    function alignToActiveDate() {
        const dates = activeDates();
        const current = state.payload?.date;
        if (!dates.length || !current || dates.includes(current)) {
            updateStepper();
            return;
        }
        requestDate(current);
    }

    async function openStock(ticker) {
        state.stockController?.abort();
        const controller = new AbortController();
        const request = ++state.stockRequest;
        state.stockController = controller;
        const drawer = el('replay-drawer');
        drawer.classList.add('is-open');
        el('replay-drawer-backdrop').classList.add('is-open');
        drawer.setAttribute('aria-hidden', 'false');
        el('replay-drawer-ticker').textContent = ticker;
        el('replay-stock-factor-table').innerHTML = '<div class="replay-loading">载入中…</div>';
        try {
            const data = await fetchJson(
                `${apiRoot}/stocks/${encodeURIComponent(ticker)}`,
                { signal: controller.signal },
            );
            if (request !== state.stockRequest) return;
            renderStockCharts(data);
            renderStockFactors(data);
        } catch (error) {
            if (error.name === 'AbortError' || request !== state.stockRequest) return;
            el('replay-stock-factor-table').innerHTML =
                `<div class="replay-loading">载入失败：${escapeHtml(error.message)}</div>`;
        }
    }

    function closeStock() {
        state.stockRequest += 1;
        state.stockController?.abort();
        state.stockController = null;
        el('replay-drawer').classList.remove('is-open');
        el('replay-drawer-backdrop').classList.remove('is-open');
        el('replay-drawer').setAttribute('aria-hidden', 'true');
    }

    function renderStockCharts(data) {
        if (typeof Plotly === 'undefined') return;
        const dates = data.rows.map((row) => row.date);
        const baseLayout = {
            height: 280,
            margin: { l: 48, r: 45, t: 32, b: 40 },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#9AA0A6', size: 11 },
            xaxis: { gridcolor: 'rgba(255,255,255,.05)' },
            yaxis: { gridcolor: 'rgba(255,255,255,.05)' },
            showlegend: false,
            hovermode: 'x unified',
        };
        Plotly.newPlot('replay-stock-price-chart', [{
            x: dates,
            y: data.rows.map((row) => row.close),
            type: 'scatter',
            mode: 'lines',
            line: { color: '#42A5F5', width: 2 },
            name: '收盘价',
        }], { ...baseLayout, title: { text: '价格', font: { color: '#E8EAED', size: 13 } } }, {
            responsive: true,
            displaylogo: false,
        });
        Plotly.newPlot('replay-stock-signal-chart', [{
            x: dates,
            y: data.rows.map((row) => row.score),
            type: 'scatter',
            mode: 'lines',
            line: { color: '#FFB300', width: 1.7 },
            name: '策略分数',
        }, {
            x: dates,
            y: data.rows.map((row) => row.percentile == null ? null : row.percentile * 100),
            type: 'scatter',
            mode: 'lines',
            yaxis: 'y2',
            line: { color: '#00C853', width: 1.3 },
            name: '排名百分位',
        }], {
            ...baseLayout,
            title: { text: '策略分数与排名百分位', font: { color: '#E8EAED', size: 13 } },
            showlegend: true,
            legend: { orientation: 'h', x: 0, y: 1.16 },
            yaxis2: { overlaying: 'y', side: 'right', range: [0, 100], ticksuffix: '%' },
        }, {
            responsive: true,
            displaylogo: false,
        });
    }

    function renderStockFactors(data) {
        const currentDate = state.payload?.date;
        const rowIndex = data.rows.findIndex((row) => row.date === currentDate);
        const index = rowIndex >= 0 ? rowIndex : data.rows.length - 1;
        const rows = (data.factor_ids || []).map((factorId) => {
            const values = data.factors[factorId] || {};
            return `
                <tr>
                    <td class="mono">${escapeHtml(factorId)}</td>
                    <td class="mono num">${number(values.raw?.[index], 6)}</td>
                    <td class="mono num">${number(values.clean?.[index], 4)}</td>
                    <td class="mono num">${number(values.strategy_input?.[index], 4)}</td>
                    <td class="mono num">${number(values.contribution?.[index], 4)}</td>
                </tr>
            `;
        }).join('');
        el('replay-stock-factor-table').innerHTML = `
            <div class="panel-title"><span>${escapeHtml(currentDate || '')} 因子拆解</span></div>
            <div class="table-wrap">
                <table class="data compact">
                    <thead><tr><th>因子</th><th class="num">原始值</th><th class="num">清洗值</th><th class="num">策略输入</th><th class="num">贡献</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
    }

    async function init() {
        try {
            state.meta = await fetchJson(`${apiRoot}/meta`);
            const dates = state.meta.dates || [];
            const dateInput = el('replay-date');
            if (dates.length) {
                dateInput.min = dates[0];
                dateInput.max = dates[dates.length - 1];
            }
            renderTimeline();
            await loadDate(state.meta.latest_date);
        } catch (error) {
            setLoading(true, `初始化失败：${error.message}`);
        }
    }

    el('replay-prev').addEventListener('click', () => stepDate(-1));
    el('replay-next').addEventListener('click', () => stepDate(1));
    el('replay-date').addEventListener('change', (event) => requestDate(event.target.value));
    document.querySelectorAll('.segment').forEach((button) => {
        button.addEventListener('click', () => {
            document.querySelectorAll('.segment').forEach((node) => node.classList.remove('is-active'));
            button.classList.add('is-active');
            state.mode = button.dataset.mode;
            alignToActiveDate();
        });
    });
    ['replay-search', 'replay-eligibility', 'replay-action', 'replay-group'].forEach((id) => {
        el(id).addEventListener(id === 'replay-search' ? 'input' : 'change', applyFilters);
    });
    el('replay-table-body').addEventListener('click', (event) => {
        const expand = event.target.closest('[data-expand]');
        if (expand) {
            const ticker = expand.dataset.expand;
            if (state.expanded.has(ticker)) {
                state.expanded.delete(ticker);
                expand.textContent = '+';
            } else {
                state.expanded.add(ticker);
                expand.textContent = '−';
            }
            applyFilters();
            return;
        }
        const stock = event.target.closest('[data-stock]');
        if (stock) openStock(stock.dataset.stock);
    });
    el('replay-drawer-close').addEventListener('click', closeStock);
    el('replay-drawer-backdrop').addEventListener('click', closeStock);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeStock();
    });

    init();
})();
