/* 回测详情页：轮询状态，完成后刷新整页以渲染图表 */
(function () {
    const tid = window.__TASK_ID__;
    if (!tid) return;

    const elapsedEl = document.getElementById('running-elapsed');
    const messageEl = document.getElementById('running-message');
    const badgeEl = document.getElementById('status-badge');
    let timing = null;
    let timingSyncedAt = Date.now();
    const statusLabels = {
        pending: '待执行',
        waiting_for_data: '等待行情',
        running: '运行中'
    };
    const statusMessages = {
        pending: '回测已进入执行队列...',
        waiting_for_data: '正在等待统一行情服务补齐并发布数据...',
        running: '回测运行中...'
    };

    function renderElapsed() {
        if (!elapsedEl || !timing) return;
        const delta = timing.active
            ? Math.max(0, (Date.now() - timingSyncedAt) / 1000)
            : 0;
        const total = Number(timing.total_elapsed_sec || 0) + delta;
        const stage = Number(timing.stage_elapsed_sec || 0) + delta;
        const stageLabels = {
            queue: '排队',
            waiting_for_data: '等待行情',
            running: '实际计算'
        };
        const stageLabel = stageLabels[timing.stage] || '当前阶段';
        elapsedEl.textContent = '总历时：' + total.toFixed(1)
            + ' 秒 · ' + stageLabel + '：' + stage.toFixed(1) + ' 秒';
    }
    const elapsedTimer = setInterval(renderElapsed, 200);

    async function poll() {
        try {
            const r = await fetch('/api/backtests/' + tid + '/status', { cache: 'no-store' });
            if (!r.ok) return;
            const data = await r.json();
            if (data.timing) {
                timing = data.timing;
                timingSyncedAt = Date.now();
                renderElapsed();
            }
            if (statusLabels[data.status]) {
                if (badgeEl) {
                    badgeEl.textContent = statusLabels[data.status];
                    badgeEl.className = 'status-pill status-' + data.status;
                }
                if (messageEl) messageEl.textContent = statusMessages[data.status];
            }
            if (data.status === 'success' || data.status === 'failed') {
                clearInterval(pollTimer);
                clearInterval(elapsedTimer);
                // 成功/失败后重新加载整页以渲染图表和指标
                window.location.reload();
            }
        } catch (e) {
            // 轮询失败暂时忽略，下轮继续
        }
    }
    const pollTimer = setInterval(poll, 1000);
    poll();  // 立即跑一次
})();
