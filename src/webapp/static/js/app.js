/**
 * 将页面中所有带 data-plotly-json 属性的容器渲染为 Plotly 图表。
 * 模板中使用：<div class="plot" data-plotly-json='{{ plot_json|safe }}'></div>
 */
(function () {
    function renderPlots() {
        if (typeof Plotly === 'undefined') return;
        const nodes = document.querySelectorAll('[data-plotly-json]');
        nodes.forEach(function (el) {
            if (el.dataset.rendered === '1') return;
            try {
                const raw = el.getAttribute('data-plotly-json');
                if (!raw) return;
                const spec = JSON.parse(raw);
                Plotly.newPlot(el, spec.data || [], spec.layout || {}, {
                    responsive: true,
                    displaylogo: false,
                    modeBarButtonsToRemove: ['lasso2d', 'select2d']
                });
                el.dataset.rendered = '1';
            } catch (e) {
                console.error('Plotly render failed:', e);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', renderPlots);
    window.addEventListener('resize', function () {
        const nodes = document.querySelectorAll('[data-plotly-json]');
        nodes.forEach(function (el) {
            if (el.dataset.rendered === '1' && typeof Plotly !== 'undefined') {
                Plotly.Plots.resize(el);
            }
        });
    });
})();
