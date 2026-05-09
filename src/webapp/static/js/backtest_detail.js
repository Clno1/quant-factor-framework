/* 回测详情页：轮询状态，完成后刷新整页以渲染图表 */
(function () {
    const tid = window.__TASK_ID__;
    if (!tid) return;

    const elapsedEl = document.getElementById('running-elapsed');
    const startTime = Date.now();

    function renderElapsed() {
        if (!elapsedEl) return;
        const sec = ((Date.now() - startTime) / 1000).toFixed(1);
        elapsedEl.textContent = '耗时：' + sec + ' 秒';
    }
    const elapsedTimer = setInterval(renderElapsed, 200);

    async function poll() {
        try {
            const r = await fetch('/api/backtests/' + tid + '/status', { cache: 'no-store' });
            if (!r.ok) return;
            const data = await r.json();
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
