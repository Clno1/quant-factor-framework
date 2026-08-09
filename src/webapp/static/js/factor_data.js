(function () {
    'use strict';

    const PAGE_SIZE = 100;
    const initialNode = document.getElementById('factor-data-initial-state');
    if (!initialNode) return;

    const initial = JSON.parse(initialNode.textContent || '{}');
    const state = {
        mode: initial.mode === 'history' ? 'history' : 'snapshot',
        universe: String(initial.universe || '').toUpperCase(),
        factor: String(initial.factor || '').toUpperCase(),
        date: initial.date || 'latest',
        ticker: String(initial.ticker || '').toUpperCase(),
        start: initial.start || '',
        end: initial.end || '',
        status: 'all',
        sort: 'rank',
        order: 'asc',
        offset: 0,
        limit: PAGE_SIZE
    };

    const el = {
        form: document.getElementById('factor-data-filters'),
        universe: document.getElementById('factor-data-universe'),
        factor: document.getElementById('factor-data-factor'),
        date: document.getElementById('factor-data-date'),
        tickerSearch: document.getElementById('factor-data-ticker-search'),
        status: document.getElementById('factor-data-status'),
        historyTicker: document.getElementById('factor-history-ticker'),
        historyStart: document.getElementById('factor-history-start'),
        historyEnd: document.getElementById('factor-history-end'),
        state: document.getElementById('factor-data-state'),
        results: document.getElementById('factor-data-results'),
        publication: document.getElementById('factor-data-publication-status'),
        snapshotView: document.getElementById('factor-snapshot-view'),
        historyView: document.getElementById('factor-history-view'),
        snapshotSummary: document.getElementById('factor-snapshot-summary'),
        snapshotCount: document.getElementById('factor-snapshot-count'),
        snapshotBody: document.getElementById('factor-snapshot-body'),
        historySummary: document.getElementById('factor-history-summary'),
        historyCaption: document.getElementById('factor-history-caption'),
        historyBody: document.getElementById('factor-history-body'),
        historyChart: document.getElementById('factor-history-chart'),
        contract: document.getElementById('factor-data-contract-grid'),
        exportLink: document.getElementById('factor-data-export'),
        previousDate: document.getElementById('factor-date-previous'),
        nextDate: document.getElementById('factor-date-next'),
        previousPage: document.getElementById('factor-page-previous'),
        nextPage: document.getElementById('factor-page-next'),
        pageLabel: document.getElementById('factor-page-label')
    };

    const universeNames = {
        SP500: '标普 500',
        NASDAQ100: '纳斯达克 100',
        MAG7: '科技七巨头'
    };
    const publicationLabels = {
        PUBLISHED: '已发布',
        MISSING: '尚未发布',
        STALE: '需要更新',
        INVALID: '完整性校验失败'
    };
    const statusLabels = Object.assign({
        VALID: '有效',
        NOT_PIT_MEMBER: '非当日成分',
        CALCULATION_WINDOW_INSUFFICIENT: '计算窗口不足',
        RAW_MISSING: '原始值缺失',
        CLEAN_MISSING: '清洗值缺失'
    }, initial.status_labels || {});

    let meta = null;
    let activeUniverse = null;
    let snapshotResult = null;
    let historyResult = null;
    let historyMetric = 'raw';
    let requestSerial = 0;

    function option(value, label) {
        const node = document.createElement('option');
        node.value = value;
        node.textContent = label;
        return node;
    }

    function setState(kind, title, message) {
        el.state.className = `factor-data-state is-${kind}`;
        const strong = document.createElement('strong');
        strong.textContent = title;
        const span = document.createElement('span');
        span.textContent = message || '';
        el.state.replaceChildren(strong, span);
        el.state.hidden = false;
        el.results.hidden = true;
    }

    function showLoading(message) {
        setState('loading', '正在读取正式研究数据', message || '正在校验 publication、generation、行情版本和 PIT 成分。');
    }

    function formatNumber(value, digits) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return Number(value).toFixed(digits === undefined ? 4 : digits);
    }

    function formatPercent(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return `${Number(value).toFixed(1)}%`;
    }

    function shortId(value) {
        const text = String(value || '—');
        return text === '—' ? text : text.slice(0, 12);
    }

    function statusBadge(code) {
        const span = document.createElement('span');
        span.className = `pool-verdict verdict-${String(code || 'invalid').toLowerCase()}`;
        span.textContent = statusLabels[code] || code || '未知';
        span.title = `系统状态：${code || 'UNKNOWN'}`;
        return span;
    }

    function currentUniverseRow() {
        if (!meta) return null;
        return meta.universes.find((item) => item.universe_id === state.universe) || null;
    }

    function currentFactorMeta() {
        const row = currentUniverseRow();
        if (row) {
            const published = row.factors.find((item) => item.factor_id === state.factor);
            if (published) return published;
        }
        return (meta && meta.factor_catalog.find((item) => item.factor_id === state.factor)) || null;
    }

    function populateUniverses() {
        const previous = state.universe;
        el.universe.replaceChildren();
        meta.universes.forEach((item) => {
            const label = universeNames[item.universe_id] || item.universe_id;
            el.universe.appendChild(option(item.universe_id, `${label}（${publicationLabels[item.status] || item.status}）`));
        });
        if (!previous || !meta.universes.some((item) => item.universe_id === previous)) {
            const primary = meta.universes.find((item) => item.role === 'PRIMARY');
            state.universe = primary ? primary.universe_id : (meta.universes[0] || {}).universe_id || '';
        }
        el.universe.value = state.universe;
    }

    function populateFactors() {
        const universeRow = currentUniverseRow();
        const publishedIds = new Set((universeRow && universeRow.factors || []).map((item) => item.factor_id));
        el.factor.replaceChildren(option('', '请选择因子'));
        meta.factor_catalog.forEach((item) => {
            const direction = item.direction > 0 ? '高值优先' : '低值优先';
            const availability = publishedIds.has(item.factor_id) ? '' : ' · 当前池未验证';
            el.factor.appendChild(option(item.factor_id, `${item.display_name}（${item.factor_id}，${direction}${availability}）`));
        });
        if (state.factor && !meta.factor_catalog.some((item) => item.factor_id === state.factor)) {
            state.factor = '';
        }
        el.factor.value = state.factor;
    }

    function renderPublicationStatus() {
        activeUniverse = currentUniverseRow();
        if (!activeUniverse) {
            el.publication.className = 'factor-data-publication-status status-invalid';
            el.publication.textContent = '请选择正式研究股票池';
            return;
        }
        el.publication.className = `factor-data-publication-status status-${activeUniverse.status.toLowerCase()}`;
        const poolName = universeNames[activeUniverse.universe_id] || activeUniverse.universe_id;
        if (activeUniverse.status === 'PUBLISHED') {
            el.publication.textContent = `${poolName} · 数据截止 ${activeUniverse.publication_target_session}`;
        } else {
            el.publication.textContent = `${poolName} · ${publicationLabels[activeUniverse.status] || activeUniverse.status}`;
        }
    }

    function applyDateBounds() {
        const factorMeta = currentFactorMeta();
        const available = meta ? (meta.available_dates || []) : [];
        if (factorMeta && factorMeta.date_start) {
            el.date.min = factorMeta.date_start;
            el.date.max = factorMeta.date_end;
            el.historyStart.min = factorMeta.date_start;
            el.historyStart.max = factorMeta.date_end;
            el.historyEnd.min = factorMeta.date_start;
            el.historyEnd.max = factorMeta.date_end;
            if (!state.start) state.start = factorMeta.date_start;
            if (!state.end) state.end = factorMeta.date_end;
        }
        if (state.date === 'latest' && available.length) {
            el.date.value = available[available.length - 1];
        } else {
            el.date.value = state.date !== 'latest' ? state.date : '';
        }
        el.historyStart.value = state.start;
        el.historyEnd.value = state.end;
    }

    function syncInputs() {
        el.universe.value = state.universe;
        el.factor.value = state.factor;
        el.tickerSearch.value = state.mode === 'snapshot' ? state.ticker : '';
        el.historyTicker.value = state.mode === 'history' ? state.ticker : '';
        el.status.value = state.status;
        applyDateBounds();
    }

    function applyMode() {
        document.querySelectorAll('[data-factor-mode]').forEach((button) => {
            const active = button.dataset.factorMode === state.mode;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('[data-visible-mode]').forEach((node) => {
            node.hidden = node.dataset.visibleMode !== state.mode;
        });
        el.snapshotView.hidden = state.mode !== 'snapshot';
        el.historyView.hidden = state.mode !== 'history';
    }

    function updateUrl(replace) {
        const params = new URLSearchParams();
        params.set('mode', state.mode);
        if (state.universe) params.set('universe', state.universe);
        if (state.factor) params.set('factor', state.factor);
        if (state.mode === 'snapshot') {
            params.set('date', state.date || 'latest');
            if (state.ticker) params.set('ticker', state.ticker);
        } else {
            if (state.ticker) params.set('ticker', state.ticker);
            if (state.start) params.set('start', state.start);
            if (state.end) params.set('end', state.end);
        }
        const url = `${window.location.pathname}?${params.toString()}`;
        window.history[replace ? 'replaceState' : 'pushState']({}, '', url);
    }

    function exportUrl() {
        if (!state.universe || !state.factor) return '#';
        const params = new URLSearchParams({
            mode: state.mode,
            universe: state.universe,
            factor: state.factor
        });
        if (state.mode === 'snapshot') {
            params.set('date', state.date || 'latest');
            params.set('status', state.status);
            params.set('sort', state.sort);
            params.set('order', state.order);
            if (state.ticker) params.set('ticker', state.ticker);
        } else {
            params.set('ticker', state.ticker);
            if (state.start) params.set('start', state.start);
            if (state.end) params.set('end', state.end);
        }
        return `/api/research/factor-data/export?${params.toString()}`;
    }

    async function fetchJson(url) {
        const response = await fetch(url, {headers: {'Accept': 'application/json'}});
        let payload = null;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = null;
        }
        if (!response.ok) {
            const detail = payload && payload.detail ? payload.detail : payload;
            const error = new Error((detail && detail.message) || `请求失败（HTTP ${response.status}）`);
            error.code = detail && detail.code;
            error.details = detail && detail.details || {};
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function publicationFailureMessage() {
        if (!activeUniverse) return '请选择一个正式研究股票池。';
        const error = activeUniverse.error || {};
        if (activeUniverse.status === 'MISSING' && activeUniverse.universe_id === 'NASDAQ100') {
            return '等待纳斯达克100的正式 PIT、行情版本和因子研究发布；不会回退到其他股票池。';
        }
        if (error.message) return error.message;
        if (activeUniverse.status === 'MISSING') return '该股票池尚无正式因子研究发布。';
        if (activeUniverse.status === 'STALE') return '研究数据未更新到当前应发布交易日。';
        return '正式研究版本未通过完整性校验，需要重发完整数据契约。';
    }

    async function loadMeta(options) {
        const serial = ++requestSerial;
        const params = new URLSearchParams();
        if (state.universe) params.set('universe', state.universe);
        if (state.factor) params.set('factor', state.factor);
        try {
            meta = await fetchJson(`/api/research/factor-data/meta?${params.toString()}`);
            if (serial !== requestSerial) return;
            populateUniverses();
            populateFactors();
            renderPublicationStatus();
            syncInputs();
            applyMode();
            el.exportLink.href = exportUrl();
            if (!state.factor) {
                setState('empty', '请选择一个因子', '因子不会被自动替你选择；选择后才能查询 raw、clean 和单因子排名。');
                return;
            }
            if (!activeUniverse || activeUniverse.status !== 'PUBLISHED') {
                setState('blocked', publicationLabels[(activeUniverse || {}).status] || '研究不可用', publicationFailureMessage());
                return;
            }
            const published = activeUniverse.factors.some((item) => item.factor_id === state.factor);
            if (!published) {
                setState('blocked', '当前研究池未发布该因子', '不会临时重算，也不会从其他股票池或旧文件回退。');
                return;
            }
            if (options && options.runQuery) await runQuery();
        } catch (error) {
            if (serial !== requestSerial) return;
            setState('error', '无法读取研究元数据', error.message);
        }
    }

    function summaryItem(label, value, note) {
        const item = document.createElement('div');
        const name = document.createElement('span');
        const strong = document.createElement('strong');
        name.textContent = label;
        strong.textContent = value;
        item.append(name, strong);
        if (note) {
            const small = document.createElement('small');
            small.textContent = note;
            item.appendChild(small);
        }
        return item;
    }

    function renderContract(contract) {
        const direction = contract.direction > 0 ? '正向：清洗值越高越优' : '负向：清洗值越低越优';
        const rows = [
            ['研究股票池', `${universeNames[contract.universe] || contract.universe}（${contract.universe}）`],
            ['因子与方向', `${contract.factor_name}（${contract.factor_id}）· ${direction}`],
            ['研究发布日期', contract.publication_target_session],
            ['研究发布版本', contract.publication_id],
            ['因子 generation', contract.factor_generation_id],
            ['行情数据版本', contract.dataset_version_id],
            ['因子 manifest SHA-256', contract.factor_manifest_sha256],
            ['行情 manifest SHA-256', contract.dataset_manifest_sha256],
            ['PIT membership SHA-256', contract.membership_sha256 || '固定名单，无独立 membership 文件'],
            ['矩阵区间', `${contract.date_start} 至 ${contract.date_end}`]
        ];
        el.contract.replaceChildren();
        rows.forEach(([label, value]) => {
            const wrapper = document.createElement('div');
            const dt = document.createElement('dt');
            const dd = document.createElement('dd');
            dt.textContent = label;
            dd.textContent = value || '—';
            if (/版本|SHA|generation/.test(label)) dd.className = 'mono break-value';
            wrapper.append(dt, dd);
            el.contract.appendChild(wrapper);
        });
    }

    function tickerHistoryLink(row, contract) {
        const link = document.createElement('a');
        const params = new URLSearchParams({
            mode: 'history',
            universe: contract.universe,
            factor: contract.factor_id,
            ticker: row.ticker,
            start: contract.date_start,
            end: contract.date_end
        });
        link.href = `/research/factor-data?${params.toString()}`;
        link.className = 'mono';
        link.textContent = row.ticker;
        link.addEventListener('click', (event) => {
            event.preventDefault();
            state.mode = 'history';
            state.ticker = row.ticker;
            state.start = contract.date_start;
            state.end = contract.date_end;
            state.offset = 0;
            applyMode();
            syncInputs();
            updateUrl(false);
            runQuery();
        });
        return link;
    }

    function renderSnapshot(payload) {
        snapshotResult = payload;
        historyResult = null;
        const summary = payload.summary;
        const contract = payload.contract;
        el.snapshotSummary.replaceChildren(
            summaryItem('观测日期', summary.observation_date),
            summaryItem('研究股票池', universeNames[contract.universe] || contract.universe),
            summaryItem('因子方向', contract.direction > 0 ? '高值优先' : '低值优先'),
            summaryItem('PIT 成分数', String(summary.pit_member_count)),
            summaryItem('raw 有效数', String(summary.raw_valid_count)),
            summaryItem('clean 有效数', String(summary.clean_valid_count)),
            summaryItem('排名分母', String(summary.eligible_count)),
            summaryItem('clean 覆盖率', formatPercent(summary.coverage * 100))
        );
        el.snapshotCount.textContent = `当前筛选结果 ${payload.total_rows} 条；完整 generation 共 ${payload.generation_total_rows} 只历史证券；本页显示 ${payload.rows.length} 条。`;
        el.snapshotBody.replaceChildren();
        payload.rows.forEach((row) => {
            const tr = document.createElement('tr');
            if (state.ticker && row.ticker === state.ticker) tr.classList.add('highlight');
            const rank = document.createElement('td');
            rank.textContent = row.factor_rank === null ? '—' : `${row.factor_rank} / ${row.eligible_count}`;
            const stock = document.createElement('td');
            stock.appendChild(tickerHistoryLink(row, contract));
            const company = document.createElement('div');
            company.className = 'cell-note';
            company.textContent = row.name || '—';
            stock.appendChild(company);
            const cells = [
                row.sector || '未知',
                formatNumber(row.raw_value, 6),
                formatNumber(row.clean_value, 4),
                formatNumber(row.oriented_value, 4),
                formatPercent(row.factor_percentile),
                row.quintile || '—',
                row.pit_member ? '是' : '否'
            ].map((value) => {
                const td = document.createElement('td');
                td.textContent = value;
                return td;
            });
            const status = document.createElement('td');
            status.appendChild(statusBadge(row.status));
            tr.append(rank, stock, ...cells, status);
            el.snapshotBody.appendChild(tr);
        });
        const first = payload.total_rows ? payload.offset + 1 : 0;
        const last = Math.min(payload.offset + payload.limit, payload.total_rows);
        el.pageLabel.textContent = `${first}-${last} / ${payload.total_rows}`;
        el.previousPage.disabled = payload.offset <= 0;
        el.nextPage.disabled = payload.offset + payload.limit >= payload.total_rows;
        el.previousDate.disabled = !payload.previous_date;
        el.nextDate.disabled = !payload.next_date;
        el.previousDate.dataset.date = payload.previous_date || '';
        el.nextDate.dataset.date = payload.next_date || '';
        el.date.value = summary.observation_date;
        renderContract(contract);
    }

    function historyDateLink(row, contract) {
        const link = document.createElement('a');
        link.href = `/research/factor-data?mode=snapshot&universe=${encodeURIComponent(contract.universe)}&factor=${encodeURIComponent(contract.factor_id)}&date=${encodeURIComponent(row.date)}&ticker=${encodeURIComponent(row.ticker)}`;
        link.className = 'mono';
        link.textContent = row.date;
        link.addEventListener('click', (event) => {
            event.preventDefault();
            state.mode = 'snapshot';
            state.date = row.date;
            state.ticker = row.ticker;
            state.offset = 0;
            applyMode();
            syncInputs();
            updateUrl(false);
            runQuery();
        });
        return link;
    }

    function renderHistoryChart() {
        if (!historyResult || typeof Plotly === 'undefined') return;
        const fieldMap = {
            raw: ['raw_value', '原始值 raw'],
            clean: ['clean_value', '清洗后因子值 clean'],
            rank: ['factor_rank', '因子排名'],
            percentile: ['factor_percentile', '排名百分位']
        };
        const [field, label] = fieldMap[historyMetric];
        const rows = historyResult.rows;
        const values = rows.map((row) => row.pit_member ? row[field] : null);
        const custom = rows.map((row) => [statusLabels[row.status] || row.status, row.eligible_count]);
        const layout = {
            height: 360,
            margin: {l: 56, r: 20, t: 24, b: 46},
            paper_bgcolor: '#0E1117',
            plot_bgcolor: '#0E1117',
            font: {color: '#E8EAED'},
            hovermode: 'x',
            xaxis: {gridcolor: '#262B3A', title: '交易日'},
            yaxis: {gridcolor: '#262B3A', title: label},
            showlegend: false
        };
        if (historyMetric === 'rank') layout.yaxis.autorange = 'reversed';
        if (historyMetric === 'percentile') layout.yaxis.range = [0, 100];
        Plotly.react(el.historyChart, [{
            x: rows.map((row) => row.date),
            y: values,
            customdata: custom,
            mode: 'lines+markers',
            line: {color: '#42A5F5', width: 1.8},
            marker: {size: 4},
            connectgaps: false,
            hovertemplate: `日期 %{x}<br>${label} %{y}<br>排名分母 %{customdata[1]}<br>状态 %{customdata[0]}<extra></extra>`
        }], layout, {responsive: true, displaylogo: false});
    }

    function renderHistory(payload) {
        historyResult = payload;
        snapshotResult = null;
        const latest = payload.summary.latest_valid;
        el.historySummary.replaceChildren(
            summaryItem('股票', `${payload.ticker} · ${payload.name}`),
            summaryItem('最近有效日', payload.summary.latest_valid_observation_date || '无有效观测', payload.summary.requested_end !== payload.summary.latest_valid_observation_date ? `请求结束日 ${payload.summary.requested_end}` : ''),
            summaryItem('raw', latest ? formatNumber(latest.raw_value, 6) : '—'),
            summaryItem('clean', latest ? formatNumber(latest.clean_value, 4) : '—'),
            summaryItem('因子排名', latest ? `${latest.factor_rank} / ${latest.eligible_count}` : '—'),
            summaryItem('百分位', latest ? formatPercent(latest.factor_percentile) : '—'),
            summaryItem('分位组', latest ? latest.quintile : '—'),
            summaryItem('区间覆盖率', formatPercent(payload.summary.coverage * 100))
        );
        el.historyCaption.textContent = `${payload.actual_start} 至 ${payload.actual_end}，共 ${payload.summary.total_sessions} 个正式交易日；有效 ${payload.summary.valid_sessions} 日。`;
        el.historyBody.replaceChildren();
        payload.rows.slice().reverse().forEach((row) => {
            const tr = document.createElement('tr');
            const dateCell = document.createElement('td');
            dateCell.appendChild(historyDateLink(row, payload.contract));
            const values = [
                formatNumber(row.raw_value, 6),
                formatNumber(row.clean_value, 4),
                row.factor_rank === null ? '—' : `${row.factor_rank} / ${row.eligible_count}`,
                formatPercent(row.factor_percentile),
                row.quintile || '—',
                row.pit_member ? '是' : '否'
            ].map((value) => {
                const td = document.createElement('td');
                td.textContent = value;
                return td;
            });
            const status = document.createElement('td');
            status.appendChild(statusBadge(row.status));
            tr.append(dateCell, ...values, status);
            el.historyBody.appendChild(tr);
        });
        renderContract(payload.contract);
        renderHistoryChart();
    }

    function showResults() {
        el.state.hidden = true;
        el.results.hidden = false;
        applyMode();
        el.exportLink.href = exportUrl();
    }

    async function runSnapshot(serial) {
        const params = new URLSearchParams({
            universe: state.universe,
            factor: state.factor,
            date: state.date || 'latest',
            status: state.status,
            sort: state.sort,
            order: state.order,
            offset: String(state.offset),
            limit: String(state.limit)
        });
        if (state.ticker) params.set('ticker', state.ticker);
        const payload = await fetchJson(`/api/research/factor-data/snapshot?${params.toString()}`);
        if (serial !== requestSerial) return;
        renderSnapshot(payload);
        showResults();
    }

    async function runHistory(serial) {
        if (!state.ticker) {
            setState('empty', '请输入股票代码', '单股历史必须指定当前 generation 中出现过的股票。');
            return;
        }
        const params = new URLSearchParams({
            universe: state.universe,
            factor: state.factor,
            ticker: state.ticker
        });
        if (state.start) params.set('start', state.start);
        if (state.end) params.set('end', state.end);
        const payload = await fetchJson(`/api/research/factor-data/history?${params.toString()}`);
        if (serial !== requestSerial) return;
        renderHistory(payload);
        showResults();
    }

    async function runQuery() {
        if (!state.universe || !state.factor) {
            setState('empty', '查询条件不完整', '请选择研究股票池和因子。');
            return;
        }
        activeUniverse = currentUniverseRow();
        if (!activeUniverse || activeUniverse.status !== 'PUBLISHED') {
            setState('blocked', publicationLabels[(activeUniverse || {}).status] || '研究不可用', publicationFailureMessage());
            return;
        }
        const serial = ++requestSerial;
        showLoading(state.mode === 'snapshot' ? '正在构造完整日期截面并计算单因子排名。' : '正在从完整 PIT 截面派生逐日排名。');
        try {
            if (state.mode === 'snapshot') await runSnapshot(serial);
            else await runHistory(serial);
        } catch (error) {
            if (serial !== requestSerial) return;
            let title = '查询未完成';
            let message = error.message;
            if (error.code === 'DATE_NOT_AVAILABLE') {
                title = '该日期没有正式观测';
                const previous = error.details.previous_date || '';
                const next = error.details.next_date || '';
                el.previousDate.dataset.date = previous;
                el.nextDate.dataset.date = next;
                el.previousDate.disabled = !previous;
                el.nextDate.disabled = !next;
                const alternatives = [
                    previous ? `前一个正式观测日 ${previous}` : '',
                    next ? `后一个正式观测日 ${next}` : ''
                ].filter(Boolean);
                if (alternatives.length) message = `${error.message} 可改看：${alternatives.join('；')}。`;
            }
            if (error.code === 'TICKER_NOT_IN_GENERATION') title = '股票不在当前 generation';
            if (error.code === 'PUBLICATION_CHANGED') title = '研究发布刚刚切换';
            setState('error', title, message);
        }
    }

    function collectFormState() {
        state.universe = el.universe.value;
        state.factor = el.factor.value;
        if (state.mode === 'snapshot') {
            state.date = el.date.value || 'latest';
            state.ticker = el.tickerSearch.value.trim().toUpperCase();
            state.status = el.status.value;
        } else {
            state.ticker = el.historyTicker.value.trim().toUpperCase();
            state.start = el.historyStart.value;
            state.end = el.historyEnd.value;
        }
        state.offset = 0;
    }

    function changeMode(mode) {
        if (mode === state.mode) return;
        state.mode = mode;
        state.offset = 0;
        if (mode === 'history') {
            state.ticker = (el.tickerSearch.value || state.ticker).trim().toUpperCase();
        }
        applyMode();
        syncInputs();
        updateUrl(false);
        if (state.factor) runQuery();
        else setState('empty', '请输入查询条件', mode === 'history' ? '请选择股票后查看逐日 raw、clean 和完整截面排名。' : '请选择因子后查看日期截面。');
    }

    function setRange(range) {
        const factorMeta = currentFactorMeta();
        if (!factorMeta || !factorMeta.date_end) return;
        const end = new Date(`${factorMeta.date_end}T00:00:00`);
        let start = new Date(end);
        if (range === '3m') start.setMonth(start.getMonth() - 3);
        if (range === '1y') start.setFullYear(start.getFullYear() - 1);
        if (range === '3y') start.setFullYear(start.getFullYear() - 3);
        state.start = range === 'all' ? factorMeta.date_start : start.toISOString().slice(0, 10);
        if (state.start < factorMeta.date_start) state.start = factorMeta.date_start;
        state.end = factorMeta.date_end;
        el.historyStart.value = state.start;
        el.historyEnd.value = state.end;
    }

    document.querySelectorAll('[data-factor-mode]').forEach((button) => {
        button.addEventListener('click', () => changeMode(button.dataset.factorMode));
    });
    document.querySelectorAll('[data-history-range]').forEach((button) => {
        button.addEventListener('click', () => setRange(button.dataset.historyRange));
    });
    document.querySelectorAll('[data-history-metric]').forEach((button) => {
        button.addEventListener('click', () => {
            historyMetric = button.dataset.historyMetric;
            document.querySelectorAll('[data-history-metric]').forEach((item) => item.classList.toggle('is-active', item === button));
            renderHistoryChart();
        });
    });
    document.querySelectorAll('[data-factor-sort]').forEach((button) => {
        button.addEventListener('click', () => {
            const selected = button.dataset.factorSort;
            state.order = state.sort === selected && state.order === 'asc' ? 'desc' : 'asc';
            state.sort = selected;
            state.offset = 0;
            runQuery();
        });
    });

    el.form.addEventListener('submit', (event) => {
        event.preventDefault();
        collectFormState();
        updateUrl(false);
        el.exportLink.href = exportUrl();
        runQuery();
    });
    el.universe.addEventListener('change', () => {
        state.universe = el.universe.value;
        state.offset = 0;
        updateUrl(false);
        loadMeta({runQuery: Boolean(state.factor)});
    });
    el.factor.addEventListener('change', () => {
        state.factor = el.factor.value;
        state.date = 'latest';
        state.start = '';
        state.end = '';
        state.offset = 0;
        updateUrl(false);
        loadMeta({runQuery: Boolean(state.factor)});
    });
    el.previousDate.addEventListener('click', () => {
        if (!el.previousDate.dataset.date) return;
        state.date = el.previousDate.dataset.date;
        state.offset = 0;
        updateUrl(false);
        runQuery();
    });
    el.nextDate.addEventListener('click', () => {
        if (!el.nextDate.dataset.date) return;
        state.date = el.nextDate.dataset.date;
        state.offset = 0;
        updateUrl(false);
        runQuery();
    });
    el.previousPage.addEventListener('click', () => {
        state.offset = Math.max(0, state.offset - state.limit);
        runQuery();
    });
    el.nextPage.addEventListener('click', () => {
        state.offset += state.limit;
        runQuery();
    });
    el.exportLink.addEventListener('click', (event) => {
        if (el.exportLink.href.endsWith('#')) event.preventDefault();
    });

    window.addEventListener('popstate', () => window.location.reload());

    applyMode();
    syncInputs();
    loadMeta({runQuery: Boolean(state.factor)});
})();
