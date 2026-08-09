(function () {
    "use strict";

    function verdictCell(value) {
        const verdict = String(value || "MISSING").toUpperCase();
        const span = document.createElement("span");
        span.className = "pool-verdict verdict-" + verdict.toLowerCase().replace(/[^a-z_]/g, "");
        span.textContent = verdict;
        return span;
    }

    function appendCell(row, content) {
        const cell = document.createElement("div");
        cell.className = "strategy-evidence-cell";
        if (content instanceof Node) {
            cell.appendChild(content);
        } else {
            cell.textContent = String(content || "");
        }
        row.appendChild(cell);
    }

    window.initStrategyEvidence = function (selectId, targetId, evidenceMap) {
        const select = document.getElementById(selectId);
        const target = document.getElementById(targetId);
        if (!select || !target) return;

        function render() {
            target.replaceChildren();
            const rows = evidenceMap[select.value] || [];
            if (!rows.length) {
                target.textContent = "该策略尚无可用的跨池研究证据。";
                target.classList.add("empty-inline");
                return;
            }
            target.classList.remove("empty-inline");
            const header = document.createElement("div");
            header.className = "strategy-evidence-row strategy-evidence-head";
            ["因子", "SP500", "NASDAQ100", "跨池结论"].forEach(function (label) {
                appendCell(header, label);
            });
            target.appendChild(header);
            rows.forEach(function (item) {
                const row = document.createElement("div");
                row.className = "strategy-evidence-row";
                const factor = document.createElement("span");
                factor.textContent = item.factor_id;
                if (item.direction_mismatch) {
                    factor.title = "策略权重方向与因子研究方向不一致";
                    factor.className = "status-fail";
                }
                appendCell(row, factor);
                appendCell(row, verdictCell(item.sp500_verdict));
                appendCell(row, verdictCell(item.nasdaq100_verdict));
                appendCell(row, verdictCell(item.cross_verdict));
                target.appendChild(row);
            });
        }

        select.addEventListener("change", render);
        render();
    };
})();
