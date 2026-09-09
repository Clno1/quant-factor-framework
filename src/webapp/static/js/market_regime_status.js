(function () {
    const chartNode = document.getElementById('market-regime-chart');
    const initialNode = document.getElementById('market-regime-initial');
    if (!chartNode || !initialNode) return;

    const elements = {
        message: document.getElementById('market-chart-message'),
        period: document.getElementById('market-period'),
        showSignals: document.getElementById('show-market-signals'),
        showOutcomes: document.getElementById('show-market-outcomes'),
        episodeBody: document.getElementById('market-episode-body'),
        episodeCount: document.getElementById('market-episode-count'),
        instrumentButtons: Array.from(document.querySelectorAll('[data-market-instrument]')),
    };
    const state = {
        instrument: 'spx',
        period: elements.period ? elements.period.value : 'wf_2020_2021',
        payload: null,
        requestNumber: 0,
        controller: null,
    };

    function finite(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function percent(value, digits = 1) {
        const parsed = finite(value);
        return parsed === null ? '—' : `${(parsed * 100).toFixed(digits)}%`;
    }

    function setMessage(message, kind = 'neutral') {
        if (!elements.message) return;
        elements.message.textContent = message;
        elements.message.dataset.kind = kind;
        elements.message.hidden = !message;
    }

    function appendCell(row, value, className = '') {
        const cell = document.createElement('td');
        cell.textContent = value;
        if (className) cell.className = className;
        row.appendChild(cell);
    }

    function renderEpisodes(payload) {
        const rows = payload.signal_episodes || [];
        elements.episodeBody.replaceChildren();
        elements.episodeCount.textContent = `${rows.length} 个`;
        if (!rows.length) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 7;
            cell.className = 'empty-table-cell';
            cell.textContent = payload.period === 'recent'
                ? '近期每日影子评分尚未运行，因此没有可展示的实时候选。'
                : '所选研究窗口没有候选报警。';
            row.appendChild(cell);
            elements.episodeBody.appendChild(row);
            return;
        }
        rows.forEach((episode) => {
            const row = document.createElement('tr');
            const range = episode.start_date === episode.end_date
                ? episode.start_date
                : `${episode.start_date} 至 ${episode.end_date}`;
            appendCell(row, range, 'mono');
            appendCell(row, episode.date, 'mono');
            appendCell(row, percent(episode.feature_value, 2), 'mono num is-negative');
            appendCell(row, percent(episode.model_probability), 'mono num');
            appendCell(row, percent(episode.baseline_probability), 'mono num');
            appendCell(row, episode.outcome ? '命中' : '未命中', episode.outcome ? 'is-positive' : 'is-negative');
            appendCell(row, episode.touch_day == null ? '—' : `第 ${episode.touch_day} 日`, 'mono num');
            elements.episodeBody.appendChild(row);
        });
    }

    function markerTrace(episodes, candleByDate, kind) {
        const isSignal = kind === 'signal';
        const visible = episodes.filter((item) => candleByDate.has(item.date));
        return {
            type: 'scatter',
            mode: 'markers',
            name: isSignal ? '候选报警' : '事后底部标签',
            x: visible.map((item) => item.date),
            y: visible.map((item) => {
                const candle = candleByDate.get(item.date);
                return isSignal ? candle.low * 0.985 : candle.high * 1.015;
            }),
            customdata: visible.map((item) => [
                item.start_date,
                item.end_date,
                item.feature_value,
                item.model_probability,
                item.baseline_probability,
                item.touch_day,
            ]),
            marker: isSignal
                ? { color: '#FFB300', size: 11, symbol: 'triangle-up', line: { color: '#0E1117', width: 1 } }
                : { color: '#26C6DA', size: 10, symbol: 'circle-open', line: { color: '#26C6DA', width: 2 } },
            hovertemplate: isSignal
                ? '<b>Stage 1 候选</b><br>%{x}<br>SPX 5日收益 %{customdata[2]:.2%}<br>模型概率 %{customdata[3]:.1%}<br>训练基准 %{customdata[4]:.1%}<extra></extra>'
                : '<b>事后评估标签</b><br>%{x}<br>首次触及：第 %{customdata[5]} 日<br>使用未来路径，不是实时信号<extra></extra>',
        };
    }

    function renderChart(payload) {
        if (typeof Plotly === 'undefined') {
            setMessage('Plotly 图表库未加载，请检查网络连接后刷新。', 'error');
            return;
        }
        const candles = payload.candles || [];
        const candleByDate = new Map(candles.map((item) => [item.date, item]));
        const traces = [{
            type: 'candlestick',
            name: payload.instrument_label,
            x: candles.map((item) => item.date),
            open: candles.map((item) => item.open),
            high: candles.map((item) => item.high),
            low: candles.map((item) => item.low),
            close: candles.map((item) => item.close),
            increasing: { line: { color: '#41C786', width: 1 }, fillcolor: '#41C786' },
            decreasing: { line: { color: '#F06470', width: 1 }, fillcolor: '#F06470' },
            hoverlabel: { namelength: 0 },
        }];
        if (elements.showSignals.checked) {
            traces.push(markerTrace(payload.signal_episodes || [], candleByDate, 'signal'));
        }
        if (elements.showOutcomes.checked) {
            traces.push(markerTrace(payload.outcome_episodes || [], candleByDate, 'outcome'));
        }
        const layout = {
            autosize: true,
            height: window.innerWidth <= 640 ? 390 : 480,
            margin: { l: 58, r: 20, t: 22, b: 48 },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(10,13,20,0.42)',
            font: { family: 'Roboto, sans-serif', color: '#9AA0A6', size: 11 },
            hovermode: 'x unified',
            dragmode: 'pan',
            showlegend: true,
            legend: { orientation: 'h', x: 0, y: 1.08, font: { size: 11 } },
            xaxis: {
                type: 'date',
                rangeslider: { visible: false },
                rangebreaks: [{ bounds: ['sat', 'mon'] }],
                gridcolor: 'rgba(255,255,255,0.04)',
                linecolor: 'rgba(255,255,255,0.10)',
                fixedrange: false,
            },
            yaxis: {
                title: { text: '指数点位', standoff: 8 },
                gridcolor: 'rgba(255,255,255,0.06)',
                linecolor: 'rgba(255,255,255,0.10)',
                fixedrange: false,
            },
        };
        Plotly.react(chartNode, traces, layout, {
            responsive: true,
            displaylogo: false,
            scrollZoom: true,
            modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
        });
        setMessage(
            `${payload.instrument_label} · ${payload.date_start} 至 ${payload.date_end} · 不含 2022 年后的封存集`,
            'success',
        );
    }

    function render(payload) {
        state.payload = payload;
        renderChart(payload);
        renderEpisodes(payload);
    }

    async function loadChart() {
        state.requestNumber += 1;
        const requestNumber = state.requestNumber;
        if (state.controller) state.controller.abort();
        state.controller = new AbortController();
        setMessage('正在校验并读取研究产物…', 'loading');
        chartNode.setAttribute('aria-busy', 'true');
        try {
            const query = new URLSearchParams({
                instrument: state.instrument,
                period: state.period,
            });
            const response = await fetch(`/api/research/market-regime/chart?${query}`, {
                signal: state.controller.signal,
                headers: { Accept: 'application/json' },
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
            if (requestNumber === state.requestNumber) render(payload);
        } catch (error) {
            if (error.name === 'AbortError' || requestNumber !== state.requestNumber) return;
            state.payload = null;
            if (typeof Plotly !== 'undefined') Plotly.purge(chartNode);
            setMessage(`图表不可用：${error.message}`, 'error');
            elements.episodeBody.replaceChildren();
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 7;
            cell.className = 'empty-table-cell is-error';
            cell.textContent = '研究产物未通过完整性校验，已停止展示。';
            row.appendChild(cell);
            elements.episodeBody.appendChild(row);
            elements.episodeCount.textContent = '不可用';
        } finally {
            if (requestNumber === state.requestNumber) chartNode.removeAttribute('aria-busy');
        }
    }

    elements.instrumentButtons.forEach((button) => {
        button.setAttribute('aria-pressed', button.classList.contains('is-active') ? 'true' : 'false');
        button.addEventListener('click', () => {
            const instrument = button.dataset.marketInstrument;
            if (!instrument || instrument === state.instrument) return;
            state.instrument = instrument;
            elements.instrumentButtons.forEach((item) => {
                const active = item === button;
                item.classList.toggle('is-active', active);
                item.setAttribute('aria-pressed', active ? 'true' : 'false');
            });
            loadChart();
        });
    });
    elements.period.addEventListener('change', () => {
        state.period = elements.period.value;
        loadChart();
    });
    elements.showSignals.addEventListener('change', () => {
        if (state.payload) renderChart(state.payload);
    });
    elements.showOutcomes.addEventListener('change', () => {
        if (state.payload) renderChart(state.payload);
    });
    let resizeFrame = null;
    window.addEventListener('resize', () => {
        if (!state.payload || typeof Plotly === 'undefined') return;
        if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
        resizeFrame = window.requestAnimationFrame(() => {
            Plotly.relayout(chartNode, {
                height: window.innerWidth <= 640 ? 390 : 480,
            });
            Plotly.Plots.resize(chartNode);
            resizeFrame = null;
        });
    });

    try {
        JSON.parse(initialNode.textContent || '{}');
    } catch (error) {
        setMessage('页面初始状态无法解析，已停止加载。', 'error');
        return;
    }
    loadChart();
})();
