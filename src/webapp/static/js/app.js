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

/** Immediate feedback for same-origin page navigation and form submissions. */
(function () {
    function startNavigation() {
        document.body.classList.add('is-navigating');
    }

    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const link = event.target.closest('a[href]');
        if (!link || link.target || link.hasAttribute('download')) return;
        const target = new URL(link.href, window.location.href);
        if (target.origin !== window.location.origin || target.href === window.location.href) return;
        startNavigation();
    });

    document.addEventListener('submit', startNavigation);
    window.addEventListener('pageshow', function () {
        document.body.classList.remove('is-navigating');
    });
})();

/**
 * <details class="dropdown"> 增强：点外面自动收起、Esc 关闭。
 */
(function () {
    function init() {
        const dropdowns = document.querySelectorAll('details.dropdown');
        if (!dropdowns.length) return;

        // 点击文档其它位置时收起所有 dropdown
        document.addEventListener('click', function (e) {
            dropdowns.forEach(function (d) {
                if (d.open && !d.contains(e.target)) {
                    d.removeAttribute('open');
                }
            });
        });

        // Esc 关闭
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                dropdowns.forEach(function (d) { d.removeAttribute('open'); });
            }
        });
    }
    document.addEventListener('DOMContentLoaded', init);
})();
