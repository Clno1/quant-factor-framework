/* 新建策略表单：勾选因子、填权重、提交 */
(function () {
    const checks = Array.from(document.querySelectorAll('.factor-check'));
    const table = document.getElementById('weights-table');
    const countEl = document.getElementById('selected-count');
    const sumEl = document.getElementById('weight-sum');

    // 当前已选因子 -> {fid, name, category, direction, weight}
    const state = {};

    function renderWeights() {
        const fids = Object.keys(state);
        countEl.textContent = fids.length;
        if (fids.length === 0) {
            table.innerHTML = '<div class="weights-empty">请在左侧勾选至少 1 个因子</div>';
            sumEl.textContent = '0';
            return;
        }
        table.innerHTML = '';
        fids.forEach(fid => {
            const row = document.createElement('div');
            row.className = 'weights-row';
            row.innerHTML = `
                <div class="weights-row-label">
                    <span class="mono">${fid}</span>
                    <span class="weights-row-sub">${state[fid].name}</span>
                </div>
                <input type="number" step="0.01" class="form-input weights-input"
                       data-fid="${fid}" value="${state[fid].weight}">
                <button class="btn small danger weights-rm" data-fid="${fid}" title="移除">×</button>
            `;
            table.appendChild(row);
        });

        table.querySelectorAll('.weights-input').forEach(inp => {
            inp.addEventListener('input', (e) => {
                const fid = e.target.dataset.fid;
                const v = parseFloat(e.target.value);
                state[fid].weight = isNaN(v) ? 0 : v;
                updateSum();
            });
        });
        table.querySelectorAll('.weights-rm').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const fid = e.target.dataset.fid;
                delete state[fid];
                const cb = document.querySelector('.factor-check[value="' + fid + '"]');
                if (cb) cb.checked = false;
                renderWeights();
            });
        });
        updateSum();
    }

    function updateSum() {
        const sum = Object.values(state).reduce((a, b) => a + (parseFloat(b.weight) || 0), 0);
        sumEl.textContent = sum.toFixed(4);
        sumEl.style.color = Math.abs(sum) > 1e-9 ? 'var(--text-1)' : 'var(--red)';
    }

    checks.forEach(cb => {
        cb.addEventListener('change', () => {
            const fid = cb.value;
            if (cb.checked) {
                if (!state[fid]) {
                    state[fid] = {
                        fid, name: cb.dataset.name,
                        category: cb.dataset.category,
                        direction: Number(cb.dataset.direction) || 1,
                        weight: Number(cb.dataset.direction) || 1,
                    };
                }
            } else {
                delete state[fid];
            }
            renderWeights();
        });
    });

    window.equalWeights = function () {
        const fids = Object.keys(state);
        if (fids.length === 0) return;
        const w = 1.0 / fids.length;
        fids.forEach(fid => {
            state[fid].weight = Math.round(
                w * state[fid].direction * 10000
            ) / 10000;
        });
        renderWeights();
    };

    window.normalizeWeights = function () {
        const fids = Object.keys(state);
        if (fids.length === 0) return;
        const total = fids.reduce((a, f) => a + Math.abs(parseFloat(state[f].weight) || 0), 0);
        if (total <= 0) { alert('权重不能全部为 0'); return; }
        fids.forEach(fid => {
            state[fid].weight = Math.round((state[fid].weight / total) * 10000) / 10000;
        });
        renderWeights();
    };

    window.submitStrategy = async function () {
        const btn = document.getElementById('submit-btn');
        const msg = document.getElementById('submit-msg');
        msg.textContent = ''; msg.className = 'submit-msg';

        const name = document.getElementById('strategy-name').value.trim();
        const description = document.getElementById('strategy-desc').value.trim();
        const fids = Object.keys(state);

        if (!name) { msg.textContent = '请填写策略名称'; msg.className = 'submit-msg err'; return; }
        if (fids.length === 0) { msg.textContent = '请至少勾选 1 个因子'; msg.className = 'submit-msg err'; return; }

        const components = fids.map(fid => ({
            factor_id: fid,
            weight: parseFloat(state[fid].weight) || 0,
        }));

        btn.disabled = true; btn.textContent = '提交中...';
        try {
            const r = await fetch('/api/strategies', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description, components }),
            });
            const data = await r.json();
            if (!r.ok) {
                msg.textContent = data.detail || ('HTTP ' + r.status);
                msg.className = 'submit-msg err';
                btn.disabled = false; btn.textContent = '提交创建';
                return;
            }
            window.location.href = '/strategies/' + data.id;
        } catch (e) {
            msg.textContent = '提交失败：' + e;
            msg.className = 'submit-msg err';
            btn.disabled = false; btn.textContent = '提交创建';
        }
    };

    renderWeights();
})();
