/**
 * Watchlist 新建/编辑页。
 * 交互：文件上传解析 + 搜索下拉 + 手动添加 + 权重表格 + 一键等权/归一化 + 提交。
 * 初始状态从 window.__WL_INIT__ 读取（edit 模式）。
 */

// ---------- 状态 ----------
const state = {
    // items 内部用 0-1 小数存权重，UI 展示为百分比
    items: [],  // [{ticker, name, weight}]
};

function loadInitial() {
    if (window.__WL_INIT__ && Array.isArray(window.__WL_INIT__.items)) {
        state.items = window.__WL_INIT__.items.map(it => ({
            ticker: String(it.ticker || "").toUpperCase(),
            name: String(it.name || ""),
            weight: Number(it.weight) || 0,
        }));
    }
    render();
}

// ---------- 渲染 ----------
function render() {
    const tbl = document.getElementById("items-table");
    const count = document.getElementById("items-count");
    const sum = document.getElementById("weight-sum");
    count.textContent = state.items.length;

    if (state.items.length === 0) {
        tbl.innerHTML = '<div class="weights-empty">请从左侧上传文件或搜索添加</div>';
        sum.textContent = "0%";
        return;
    }

    tbl.innerHTML = state.items.map((it, idx) => `
        <div class="weights-row" data-idx="${idx}">
            <div class="weights-row-label">
                <div class="mono" style="color: var(--text-0); font-weight: 500;">${escapeHtml(it.ticker)}</div>
                <div class="weights-row-sub">${escapeHtml(it.name || "")}</div>
            </div>
            <div style="display: flex; align-items: center; gap: 4px;">
                <input class="form-input weights-input mono" type="number"
                       step="0.01" min="0" max="100"
                       value="${(it.weight * 100).toFixed(2)}"
                       onchange="updateWeight(${idx}, this.value)">
                <span style="color: var(--text-2); font-size: 12px;">%</span>
            </div>
            <button class="btn small weights-rm" onclick="removeItem(${idx})" title="删除">×</button>
        </div>
    `).join("");

    const total = state.items.reduce((s, it) => s + (Number(it.weight) || 0), 0);
    sum.textContent = (total * 100).toFixed(2) + "%";
    sum.style.color = Math.abs(total - 1) < 0.001 ? "var(--green)" : "var(--amber)";
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;", "'": "&#39;",
    })[c]);
}

// ---------- 添加 / 删除 ----------
function addItem(ticker, name = "", weight = 0) {
    ticker = String(ticker || "").trim().toUpperCase();
    if (!ticker) return false;
    if (state.items.some(it => it.ticker === ticker)) {
        showMsg("upload-msg", `${ticker} 已在列表中，跳过`, "warn");
        return false;
    }
    state.items.push({ ticker, name, weight: Number(weight) || 0 });
    render();
    return true;
}

function removeItem(idx) {
    state.items.splice(idx, 1);
    render();
}

function updateWeight(idx, value) {
    const pct = Number(value);
    if (!Number.isFinite(pct) || pct < 0) return;
    state.items[idx].weight = pct / 100;
    render();
}

function equalWeights() {
    if (state.items.length === 0) return;
    const w = 1 / state.items.length;
    state.items.forEach(it => it.weight = w);
    render();
}

function normalizeWeights() {
    const total = state.items.reduce((s, it) => s + (Number(it.weight) || 0), 0);
    if (total <= 0) {
        equalWeights();
        return;
    }
    state.items.forEach(it => it.weight = it.weight / total);
    render();
}

function clearAll() {
    if (state.items.length === 0) return;
    if (!confirm(`确定清空当前 ${state.items.length} 只股票？`)) return;
    state.items = [];
    render();
}

// ---------- 文件上传 ----------
function parseFileContent(text) {
    // 返回 [{ticker, weight?}]
    const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    const rows = [];
    let hasHeader = false;
    if (lines.length > 0) {
        const first = lines[0].toLowerCase();
        if (first.includes("ticker") || first.includes("symbol") || first.includes("code")) {
            hasHeader = true;
        }
    }
    const startIdx = hasHeader ? 1 : 0;
    for (let i = startIdx; i < lines.length; i++) {
        const parts = lines[i].split(/[,;\t\s]+/).filter(Boolean);
        if (parts.length === 0) continue;
        const ticker = parts[0].toUpperCase();
        const weight = parts.length > 1 ? parseFloat(parts[1]) : NaN;
        rows.push({
            ticker,
            weight: Number.isFinite(weight) ? weight : null,
        });
    }
    return rows;
}

async function handleFile(file) {
    if (!file) return;
    const text = await file.text();
    const rows = parseFileContent(text);
    if (rows.length === 0) {
        showMsg("upload-msg", "文件为空或解析不出 ticker", "err");
        return;
    }

    showMsg("upload-msg", `正在校验 ${rows.length} 个 ticker ...`, "");
    const btns = document.querySelectorAll(".btn");
    btns.forEach(b => b.disabled = true);

    const added = [];
    const invalid = [];
    const skipped = [];
    // 串行校验（FMP 限流，且 verify_ticker 本身很快）
    for (const row of rows) {
        if (state.items.some(it => it.ticker === row.ticker)) {
            skipped.push(row.ticker);
            continue;
        }
        try {
            const r = await fetch(`/api/ticker_verify?ticker=${encodeURIComponent(row.ticker)}`);
            if (!r.ok) { invalid.push(row.ticker); continue; }
            const info = await r.json();
            if (!info.exists) { invalid.push(row.ticker); continue; }
            // 权重：文件中的 weight 可能是 0-1 小数也可能是 0-100 百分数——自动推断
            let w = row.weight;
            if (w === null) w = 0;
            else if (w > 1.5) w = w / 100;  // 0-100 百分数
            state.items.push({
                ticker: info.ticker,
                name: info.name || "",
                weight: Number(w) || 0,
            });
            added.push(info.ticker);
        } catch (e) {
            invalid.push(row.ticker);
        }
    }
    btns.forEach(b => b.disabled = false);
    render();

    const parts = [];
    if (added.length) parts.push(`✓ 添加 ${added.length} 只`);
    if (skipped.length) parts.push(`· 跳过重复 ${skipped.length} 只（${skipped.slice(0, 5).join(",")}${skipped.length > 5 ? "..." : ""}）`);
    if (invalid.length) parts.push(`✗ 无效 ${invalid.length} 只（${invalid.slice(0, 5).join(",")}${invalid.length > 5 ? "..." : ""}）`);
    showMsg("upload-msg", parts.join(" "), invalid.length ? "warn" : "ok");
}

function initFileDrop() {
    const drop = document.getElementById("file-drop");
    const input = document.getElementById("file-input");
    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", e => {
        handleFile(e.target.files[0]);
        input.value = "";  // 允许重复选同一文件
    });
    ["dragenter", "dragover"].forEach(ev => {
        drop.addEventListener(ev, e => {
            e.preventDefault();
            drop.classList.add("dragover");
        });
    });
    ["dragleave", "drop"].forEach(ev => {
        drop.addEventListener(ev, e => {
            e.preventDefault();
            drop.classList.remove("dragover");
        });
    });
    drop.addEventListener("drop", e => {
        handleFile(e.dataTransfer.files[0]);
    });
}

// ---------- 搜索添加 ----------
let _searchTimer = null;
let _searchSeq = 0;

function initSearch() {
    const input = document.getElementById("symbol-search");
    const box = document.getElementById("symbol-suggest");
    input.addEventListener("input", () => {
        clearTimeout(_searchTimer);
        const q = input.value.trim();
        if (q.length < 1) { box.innerHTML = ""; box.style.display = "none"; return; }
        _searchTimer = setTimeout(() => doSearch(q), 260);
    });
    input.addEventListener("blur", () => {
        setTimeout(() => { box.style.display = "none"; }, 200);
    });
    input.addEventListener("focus", () => {
        if (box.children.length > 0) box.style.display = "block";
    });
}

async function doSearch(q) {
    const seq = ++_searchSeq;
    const box = document.getElementById("symbol-suggest");
    box.innerHTML = '<div class="symbol-suggest-item muted">搜索中…</div>';
    box.style.display = "block";
    try {
        const r = await fetch(`/api/symbol_search?q=${encodeURIComponent(q)}&limit=20`);
        if (seq !== _searchSeq) return;
        if (!r.ok) {
            box.innerHTML = '<div class="symbol-suggest-item muted">搜索失败</div>';
            return;
        }
        const list = await r.json();
        if (!Array.isArray(list) || list.length === 0) {
            box.innerHTML = '<div class="symbol-suggest-item muted">没有结果</div>';
            return;
        }
        box.innerHTML = list.map(it => {
            const exists = state.items.some(x => x.ticker === it.ticker);
            return `<div class="symbol-suggest-item ${exists ? "disabled" : ""}"
                         data-ticker="${escapeHtml(it.ticker)}"
                         data-name="${escapeHtml(it.name)}">
                    <span class="mono" style="color: var(--accent-2); font-weight: 500;">${escapeHtml(it.ticker)}</span>
                    <span class="suggest-name">${escapeHtml(it.name)}</span>
                    <span class="suggest-exch">${escapeHtml(it.exchange)}</span>
                    ${exists ? '<span class="suggest-added">已添加</span>' : ""}
                </div>`;
        }).join("");
        box.querySelectorAll(".symbol-suggest-item[data-ticker]").forEach(el => {
            if (el.classList.contains("disabled")) return;
            el.addEventListener("mousedown", e => {
                e.preventDefault();
                addItem(el.dataset.ticker, el.dataset.name, 0);
                document.getElementById("symbol-search").value = "";
                box.style.display = "none";
            });
        });
    } catch (e) {
        box.innerHTML = '<div class="symbol-suggest-item muted">网络错误</div>';
    }
}

// ---------- 手动添加（校验后添加） ----------
async function manualAdd() {
    const input = document.getElementById("manual-ticker");
    const t = (input.value || "").trim().toUpperCase();
    if (!t) return;
    if (state.items.some(it => it.ticker === t)) {
        alert(`${t} 已在列表中`);
        return;
    }
    const btn = input.nextElementSibling;
    btn.disabled = true;
    btn.textContent = "校验中...";
    try {
        const r = await fetch(`/api/ticker_verify?ticker=${encodeURIComponent(t)}`);
        if (!r.ok) throw new Error(r.status);
        const info = await r.json();
        if (!info.exists) {
            alert(`${t} 在 FMP 中不存在`);
            return;
        }
        addItem(info.ticker, info.name || "", 0);
        input.value = "";
    } catch (e) {
        alert(`校验失败: ${e}`);
    } finally {
        btn.disabled = false;
        btn.textContent = "校验并添加";
    }
}

// ---------- 提交 ----------
async function submitWatchlist() {
    const name = document.getElementById("wl-name").value.trim();
    const desc = document.getElementById("wl-desc").value.trim();
    if (!name) { showMsg("submit-msg", "请填写名称", "err"); return; }
    if (state.items.length === 0) {
        showMsg("submit-msg", "请至少添加 1 只股票", "err");
        return;
    }

    const payload = {
        name,
        description: desc,
        items: state.items.map(it => ({
            ticker: it.ticker,
            name: it.name,
            weight: Number(it.weight) || 0,
        })),
    };

    const btn = document.getElementById("submit-btn");
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "提交中...";
    showMsg("submit-msg", "", "");

    try {
        let url = "/api/watchlists";
        let method = "POST";
        if (window.__WL_MODE__ === "edit" && window.__WL_ID__) {
            url = `/api/watchlists/${window.__WL_ID__}`;
            method = "PUT";
        }
        const r = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await r.json();
        if (!r.ok) {
            showMsg("submit-msg", "提交失败: " + (data.detail || r.status), "err");
            return;
        }
        showMsg("submit-msg", "✓ 保存成功，跳转中...", "ok");
        setTimeout(() => window.location.href = "/watchlists", 600);
    } catch (e) {
        showMsg("submit-msg", "网络错误: " + e, "err");
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

function showMsg(id, text, kind) {
    const el = document.getElementById(id);
    el.className = "submit-msg" + (kind === "err" ? " err" : kind === "ok" ? " ok" : kind === "warn" ? " warn" : "");
    el.textContent = text;
}

// ---------- Bootstrap ----------
document.addEventListener("DOMContentLoaded", () => {
    loadInitial();
    initFileDrop();
    initSearch();
    // 回车提交 manual
    document.getElementById("manual-ticker").addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); manualAdd(); }
    });
});
