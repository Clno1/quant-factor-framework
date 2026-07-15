(function () {
    const ticker = window.__BREAKOUT_TICKER__;
    if (!ticker) return;

    const buttons = Array.from(document.querySelectorAll('.interval-btn'));
    const message = document.getElementById('intraday-message');
    const source = document.getElementById('intraday-source');
    const chart = document.getElementById('intraday-chart');

    function money(value) {
        return Number.isFinite(Number(value)) ? '$' + Number(value).toFixed(2) : '—';
    }

    function setText(id, value) {
        const node = document.getElementById(id);
        if (node) node.textContent = value;
    }

    function renderOpeningRanges(data) {
        [1, 5, 30, 60].forEach(function (minutes) {
            const item = (data.opening_ranges || {})[String(minutes)];
            const card = document.getElementById('or-card-' + minutes);
            if (!item) {
                setText('or-high-' + minutes, '—');
                setText('or-state-' + minutes, '尚未形成');
                if (card) card.classList.remove('is-triggered');
                return;
            }
            setText('or-high-' + minutes, money(item.high));
            const state = item.triggered
                ? (item.current_above ? '已触发 · 仍在上方' : '已触发 · 当前回落')
                : '未触发';
            setText('or-state-' + minutes, state);
            if (card) card.classList.toggle('is-triggered', Boolean(item.triggered));
        });
    }

    function renderChart(data) {
        if (!chart || typeof Plotly === 'undefined') return;
        const bars = data.bars || [];
        const x = bars.map((bar) => bar.date);
        const traces = [{
            type: 'candlestick',
            x: x,
            open: bars.map((bar) => bar.open),
            high: bars.map((bar) => bar.high),
            low: bars.map((bar) => bar.low),
            close: bars.map((bar) => bar.close),
            name: ticker,
            increasing: { line: { color: '#00C853' } },
            decreasing: { line: { color: '#FF5252' } }
        }];
        [10, 20, 50].forEach(function (window) {
            traces.push({
                type: 'scatter',
                mode: 'lines',
                x: x,
                y: bars.map((bar) => bar['ma' + window]),
                name: 'MA' + window + ' · ' + data.interval + 'm bar',
                line: {
                    width: 1.5,
                    color: window === 10 ? '#42A5F5' : (window === 20 ? '#FFB300' : '#26C6DA')
                },
                connectgaps: false
            });
        });
        Plotly.react(chart, traces, {
            paper_bgcolor: '#0E1117',
            plot_bgcolor: '#1A1F2E',
            font: { color: '#E8EAED' },
            height: 520,
            margin: { l: 46, r: 24, t: 22, b: 40 },
            hovermode: 'x unified',
            xaxis: { gridcolor: '#262B3A', rangeslider: { visible: false } },
            yaxis: { gridcolor: '#262B3A' },
            legend: { orientation: 'h', y: 1.03, x: 0 }
        }, {
            responsive: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['lasso2d', 'select2d']
        });
    }

    async function load(interval) {
        buttons.forEach((button) => button.disabled = true);
        if (message) {
            message.hidden = false;
            message.textContent = '正在读取 FMP 分钟行情…';
            message.classList.remove('is-error');
        }
        try {
            const response = await fetch('/api/breakouts/' + encodeURIComponent(ticker) + '/intraday?interval=' + interval, { cache: 'no-store' });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || '分钟行情请求失败');
            if (data.error) throw new Error(data.error);

            if (source) source.textContent = data.session_date + ' · ' + data.source;
            setText('intraday-last', money(data.last_price));
            setText('intraday-time', data.last_timestamp || '—');
            setText('intraday-low', money(data.day_low));
            setText('intraday-risk', Number.isFinite(Number(data.stop_width))
                ? '止损宽度 ' + Number(data.stop_width).toFixed(2) + '%'
                : '—');
            renderOpeningRanges(data);
            renderChart(data);
            if (message) message.hidden = true;
        } catch (error) {
            if (source) source.textContent = '不可用';
            if (message) {
                message.hidden = false;
                message.textContent = error.message || String(error);
                message.classList.add('is-error');
            }
        } finally {
            buttons.forEach((button) => button.disabled = false);
        }
    }

    buttons.forEach(function (button) {
        button.addEventListener('click', function () {
            buttons.forEach((candidate) => candidate.classList.toggle('is-active', candidate === button));
            load(Number(button.dataset.interval || 5));
        });
    });
    load(5);
})();
