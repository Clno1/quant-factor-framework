(() => {
  "use strict";
  const root = document.querySelector("[data-ga-page]");
  if (!root) return;

  const errorBox = document.getElementById("ga-error");
  const showError = (message) => {
    if (!errorBox) return;
    errorBox.textContent = message;
    errorBox.hidden = false;
  };
  const clearError = () => {
    if (errorBox) {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }
  };
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const pct = (value, digits = 2) => finite(value) ? `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%` : "—";
  const ratio = (value, digits = 1) => finite(value) ? `${(value * 100).toFixed(digits)}%` : "—";
  const num = (value, digits = 3) => finite(value) ? value.toFixed(digits) : "—";
  const text = (value) => value === null || value === undefined || value === "" ? "—" : String(value);

  async function getJSON(url) {
    const response = await fetch(url, { headers: { "Accept": "application/json" } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.error && body.error.message ? body.error.message : `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return body;
  }

  const cell = (row, value, className = "") => {
    const td = document.createElement("td");
    td.textContent = value;
    if (className) td.className = className;
    row.appendChild(td);
    return td;
  };
  const returnClass = (value) => finite(value) ? (value > 0 ? "ga-positive" : value < 0 ? "ga-negative" : "") : "ga-muted";
  const driverText = (driver) => driver && driver.ticker ? `${driver.ticker} ${pct(driver.contribution, 3)}` : "—";
  const reasonText = (reasons) => Array.isArray(reasons) && reasons.length ? reasons.join(", ") : "无";

  function renderDefinitions(element, values, append = false) {
    if (!element) return;
    if (!append) element.replaceChildren();
    Object.entries(values).forEach(([label, value]) => {
      const wrapper = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = text(value);
      wrapper.append(dt, dd);
      element.appendChild(wrapper);
    });
  }

  function formatInstant(value, timeZone, suffix) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    try {
      const formatted = new Intl.DateTimeFormat("zh-CN", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).format(parsed);
      return `${formatted} ${suffix}`;
    } catch (_error) {
      return text(value);
    }
  }

  function setupTabs() {
    document.querySelectorAll(".ga-tab:not(:disabled)[data-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        document.querySelectorAll(".ga-tab[data-tab]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", String(active));
        });
        document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
          panel.hidden = panel.dataset.tabPanel !== button.dataset.tab;
        });
      });
    });
  }

  let lastHeatPayload = null;

  function detailURL(payload, item) {
    const query = new URLSearchParams({
      data_run_id: payload.data_run_id,
      level: payload.taxonomy_level,
    });
    return `/group-analytics/groups/${encodeURIComponent(item.group_id)}?${query}`;
  }

  function renderHeatMap(payload) {
    const container = document.getElementById("ga-heatmap");
    if (!container) return;
    container.replaceChildren();
    const rows = payload.rows || [];
    const automatic = Boolean(document.getElementById("ga-auto-scale")?.checked);
    const observed = rows.reduce((maximum, item) => (
      finite(item.robust_ew_return_1d) ? Math.max(maximum, Math.abs(item.robust_ew_return_1d)) : maximum
    ), 0);
    const scale = automatic ? Math.max(observed, 0.0001) : 0.05;
    const scaleLabel = document.getElementById("ga-scale-label");
    if (scaleLabel) {
      scaleLabel.textContent = `颜色/长度范围：${automatic ? "自动" : "固定"} ±${(scale * 100).toFixed(2)}%`;
    }

    rows.forEach((item) => {
      const value = item.robust_ew_return_1d;
      const link = document.createElement("a");
      link.className = "ga-heat-item";
      link.href = detailURL(payload, item);
      link.setAttribute("role", "listitem");
      link.setAttribute("aria-label", `${text(item.group_name)}，截尾等权一日收益 ${pct(value, 4)}，有效成员 ${text(item.n_valid)} / ${text(item.n_expected)}`);
      link.title = `${text(item.group_name)} · 截尾等权 ${pct(value, 4)} · 原始等权 ${pct(item.raw_ew_return_1d, 4)} · 覆盖 ${ratio(item.count_coverage, 2)}`;

      const rank = document.createElement("span");
      rank.className = "ga-heat-rank";
      rank.textContent = text(item.view_rank);
      const name = document.createElement("span");
      name.className = "ga-heat-name";
      name.textContent = text(item.group_name);
      const track = document.createElement("span");
      track.className = "ga-heat-track";
      const center = document.createElement("span");
      center.className = "ga-heat-center";
      track.appendChild(center);
      if (finite(value) && value !== 0) {
        const width = Math.min(Math.abs(value) / scale, 1) * 50;
        const fill = document.createElement("span");
        fill.className = `ga-heat-fill ${value >= 0 ? "positive" : "negative"}`;
        fill.style.left = value >= 0 ? "50%" : `${50 - width}%`;
        fill.style.width = `${width}%`;
        track.appendChild(fill);
      }
      const valueLabel = document.createElement("span");
      valueLabel.className = `ga-heat-value ${returnClass(value)}`;
      valueLabel.textContent = pct(value);
      link.append(rank, name, track, valueLabel);
      container.appendChild(link);
    });
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "ga-muted";
      empty.textContent = "没有符合当前筛选条件的分类";
      container.appendChild(empty);
    }
  }

  function renderHeat(payload) {
    lastHeatPayload = payload;
    renderHeatMap(payload);
    const tbody = document.getElementById("ga-heat-rows");
    tbody.replaceChildren();
    (payload.rows || []).forEach((item) => {
      const tr = document.createElement("tr");
      cell(tr, `${text(item.view_rank)} / ${text(item.headline_rank)}`);
      const nameCell = cell(tr, "");
      const link = document.createElement("a");
      link.textContent = text(item.group_name);
      link.href = detailURL(payload, item);
      nameCell.appendChild(link);
      cell(tr, `${text(item.snapshot_quality_grade)} · ${reasonText(item.reason_codes)}`, "ga-quality");
      cell(tr, `${text(item.n_valid)}/${text(item.n_expected)}`);
      cell(tr, ratio(item.count_coverage));
      cell(tr, pct(item.robust_ew_return_1d), returnClass(item.robust_ew_return_1d));
      cell(tr, pct(item.raw_ew_return_1d), returnClass(item.raw_ew_return_1d));
      cell(tr, pct(item.median_return_1d), returnClass(item.median_return_1d));
      cell(tr, pct(item.cap_return_1d), returnClass(item.cap_return_1d));
      cell(tr, text(item.cap_type));
      cell(tr, ratio(item.up_pct));
      cell(tr, num(item.breadth_net));
      cell(tr, pct(item.dispersion_mad));
      cell(tr, pct(item.headline_relative_return_1d), returnClass(item.headline_relative_return_1d));
      cell(tr, driverText(item.top_driver), returnClass(item.top_driver && item.top_driver.contribution));
      cell(tr, driverText(item.bottom_driver), returnClass(item.bottom_driver && item.bottom_driver.contribution));
      cell(tr, text(item.date || payload.asof));
      tbody.appendChild(tr);
    });
    if (!(payload.rows || []).length) {
      const tr = document.createElement("tr");
      const td = cell(tr, "没有符合当前筛选条件的分类", "ga-muted");
      td.colSpan = 17;
      tbody.appendChild(tr);
    }
    const title = document.getElementById("ga-heat-title");
    if (title) {
      const labels = { robust_ew_return_1d: "截尾等权 1D", up_pct: "上涨比例", n_valid: "有效成员", group_name: "分类名称" };
      const view = payload.sort && payload.sort.view;
      const suffix = view === "top" ? " · Top" : view === "bottom" ? " · Bottom" : "";
      title.textContent = `按 ${labels[payload.sort && payload.sort.sort_by] || "截尾等权 1D"} ${payload.sort && payload.sort.sort_order === "asc" ? "升序" : "降序"}${suffix}`;
    }
  }

  function renderStatus(payload) {
    const axes = [
      ["ga-attempt-status", "任务", payload.last_attempt_status],
      ["ga-freshness-status", "新鲜度", payload.freshness_status],
      ["ga-quality-status", "质量", payload.quality_status],
    ];
    axes.forEach(([id, label, value]) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = `${label} ${text(value)}`;
      el.classList.remove("ok", "warn", "bad");
      el.classList.add(["SUCCESS", "FRESH", "OK"].includes(value) ? "ok" : ["FAILED", "STALE", "NO_DATA"].includes(value) ? "bad" : "warn");
    });
    const summary = document.getElementById("ga-run-summary");
    if (summary) {
      summary.textContent = `数据日 ${text(payload.asof)} · run ${text(payload.data_run_id)} · ${text(payload.taxonomy)}/${text(payload.taxonomy_level)} · ${text(payload.methodology && payload.methodology.headline_method)}`;
    }
    const qualitySummary = payload.quality_summary || {};
    renderDefinitions(document.getElementById("ga-status-details"), {
      "数据日 / 源最大日": `${text(payload.asof)} / ${text(payload.source_max_date)}`,
      "成员覆盖": `${text(qualitySummary.n_valid)}/${text(qualitySummary.n_expected)} · ${ratio(qualitySummary.count_coverage)}`,
      "合格 / 低置信分类": `${text(qualitySummary.n_groups_ranked)} / ${text(qualitySummary.n_groups_low_confidence)}`,
      "算法版本": payload.algorithm_version,
      "分类 / ID 映射版本": `${text(payload.taxonomy_version)} / ${text(payload.group_id_mapping_version)}`,
      [`基准 ${text(payload.benchmark)} 1D`]: pct(payload.benchmark_return_1d),
      "快照时间 UTC": formatInstant(payload.snapshot_time, "UTC", "UTC"),
      "快照时间美东": formatInstant(payload.snapshot_time, "America/New_York", "ET"),
      "快照时间上海": formatInstant(payload.snapshot_time, "Asia/Shanghai", "CST"),
    });

    const quality = document.getElementById("ga-quality-details");
    renderDefinitions(quality, {
      "数据 run": payload.data_run_id,
      "最后尝试": payload.last_attempt_run_id,
      "参数 hash": payload.parameter_hash,
      "分类版本": payload.taxonomy_version,
      "ID 映射版本": payload.group_id_mapping_version,
      "整体覆盖": ratio(qualitySummary.count_coverage),
      "合格分类": qualitySummary.n_groups_ranked,
      "低置信分类": qualitySummary.n_groups_low_confidence,
    });
  }

  async function renderRunQuality(payload) {
    const quality = document.getElementById("ga-quality-details");
    const runId = payload.last_attempt_run_id;
    if (!quality || !runId) return;
    const attempt = await getJSON(`/api/group-analytics/runs/${encodeURIComponent(runId)}`);
    const counts = attempt.diagnostic_counts || {};
    renderDefinitions(quality, {
      "执行结果": attempt.execution_result || attempt.last_attempt_status,
      "输入 universe/returns": `${text(attempt.input_row_counts && attempt.input_row_counts.universe)}/${text(attempt.input_row_counts && attempt.input_row_counts.returns)}`,
      "缺失成员诊断": counts.missing_members || 0,
      "低置信分类诊断": counts.low_confidence_groups || 0,
      "分类诊断": counts.classification_diagnostics || 0,
      "最近错误": attempt.error ? `${text(attempt.error.code)} · ${text(attempt.error.summary)}` : "—",
    }, true);
  }

  async function loadHeat() {
    clearError();
    const form = document.getElementById("ga-filters");
    const data = new FormData(form);
    const apiParams = new URLSearchParams();
    for (const [key, value] of data.entries()) apiParams.set(key, value);
    apiParams.set("sort_by", form.elements.sort_by.value);
    apiParams.set("sort_order", form.elements.sort_order.value);
    apiParams.set("show_low_confidence", String(form.elements.show_low_confidence.checked));
    apiParams.set("mode", "eod");
    apiParams.set("asof", "latest");
    const browserParams = new URLSearchParams(apiParams);
    browserParams.set("color_scale", document.getElementById("ga-auto-scale")?.checked ? "auto" : "fixed");
    history.replaceState({}, "", `${location.pathname}?${browserParams}`);
    try {
      const payload = await getJSON(`/api/group-analytics/heat?${apiParams}`);
      renderStatus(payload);
      renderHeat(payload);
      try {
        await renderRunQuality(payload);
      } catch (error) {
        showError(`数据质量读取失败：${error.message}`);
      }
    } catch (error) {
      showError(error.message);
    }
  }

  async function initHeat() {
    setupTabs();
    const form = document.getElementById("ga-filters");
    const current = new URLSearchParams(location.search);
    ["level", "view", "sort_by", "sort_order", "view_min_members"].forEach((key) => {
      if (current.has(key) && form.elements[key]) form.elements[key].value = current.get(key);
    });
    form.elements.show_low_confidence.checked = current.get("show_low_confidence") === "true";
    const autoScale = document.getElementById("ga-auto-scale");
    autoScale.checked = current.get("color_scale") === "auto";
    autoScale.addEventListener("change", () => {
      const params = new URLSearchParams(location.search);
      params.set("color_scale", autoScale.checked ? "auto" : "fixed");
      history.replaceState({}, "", `${location.pathname}?${params}`);
      if (lastHeatPayload) renderHeatMap(lastHeatPayload);
    });
    const syncHeadlineView = () => {
      const frozen = form.elements.view.value !== "all";
      if (frozen) {
        form.elements.sort_by.value = "robust_ew_return_1d";
        form.elements.sort_order.value = "desc";
      }
      form.elements.sort_by.disabled = frozen;
      form.elements.sort_order.disabled = frozen;
    };
    form.elements.view.addEventListener("change", syncHeadlineView);
    syncHeadlineView();
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      loadHeat();
    });
    try {
      const metadata = await getJSON("/api/group-analytics/metadata");
      const enabledLevels = new Set((metadata.available_combinations || []).map((item) => item.level));
      [...form.elements.level.options].forEach((option) => {
        option.disabled = !enabledLevels.has(option.value);
      });
      if (!enabledLevels.has(form.elements.level.value) && enabledLevels.size) {
        form.elements.level.value = [...enabledLevels][0];
      }
      await loadHeat();
    } catch (error) {
      showError(error.message);
    }
  }

  let detailState = null;
  let lastDetailPayload = null;

  function syncDetailURL() {
    const params = new URLSearchParams({
      level: detailState.level,
      page: String(detailState.page),
      page_size: String(detailState.pageSize),
      member_sort_by: detailState.sortBy,
      member_sort_order: detailState.sortOrder,
    });
    if (detailState.runId) params.set("data_run_id", detailState.runId);
    if (detailState.search) params.set("member_search", detailState.search);
    history.replaceState({}, "", `${location.pathname}?${params}`);
  }

  function renderMemberRows(payload) {
    const tbody = document.getElementById("ga-member-rows");
    tbody.replaceChildren();
    const query = detailState.search.trim().toLocaleLowerCase();
    const sourceRows = payload.members.rows || [];
    const visibleRows = query ? sourceRows.filter((item) => (
      [item.ticker, item.name, item.security_id, item.counting_unit_id]
        .some((value) => String(value || "").toLocaleLowerCase().includes(query))
    )) : sourceRows;
    visibleRows.forEach((item) => {
      const tr = document.createElement("tr");
      const tickerCell = cell(tr, "");
      const tickerLink = document.createElement("a");
      tickerLink.textContent = text(item.ticker);
      tickerLink.href = `/stock/${encodeURIComponent(item.ticker)}?universe=SP500`;
      tickerCell.appendChild(tickerLink);
      cell(tr, text(item.name));
      cell(tr, text(item.security_id));
      cell(tr, text(item.counting_unit_id));
      cell(tr, item.is_valid_for_headline ? "是" : "否");
      cell(tr, pct(item.raw_return_1d), returnClass(item.raw_return_1d));
      cell(tr, pct(item.winsorized_return_1d), returnClass(item.winsorized_return_1d));
      cell(tr, item.was_winsorized ? "是" : "否");
      cell(tr, ratio(item.headline_weight));
      cell(tr, pct(item.headline_contribution, 3), returnClass(item.headline_contribution));
      cell(tr, num(item.contribution_bps, 2), returnClass(item.headline_contribution));
      cell(tr, text(item.data_asof));
      cell(tr, (item.reason_codes || []).join(", ") || "—");
      tbody.appendChild(tr);
    });
    if (!visibleRows.length) {
      const tr = document.createElement("tr");
      const td = cell(tr, query ? "当前页没有匹配成员" : "该分类没有成员", "ga-muted");
      td.colSpan = 13;
      tbody.appendChild(tr);
    }
    document.getElementById("ga-page-label").textContent = `第 ${payload.members.page} 页 · 共 ${payload.members.total} 项 · 当前页显示 ${visibleRows.length}/${sourceRows.length}`;
  }

  function renderDetail(payload) {
    lastDetailPayload = payload;
    detailState.runId = payload.data_run_id;
    document.getElementById("ga-detail-title").textContent = text(payload.summary && payload.summary.group_name);
    document.getElementById("ga-detail-run").textContent = `run ${text(payload.data_run_id)} · 数据日 ${text(payload.asof)} · 算法 ${text(payload.algorithm_version)}`;
    const cards = document.getElementById("ga-detail-summary");
    cards.replaceChildren();
    const summary = payload.summary || {};
    const values = [
      ["截尾等权", pct(summary.robust_ew_return_1d)],
      ["原始等权", pct(summary.raw_ew_return_1d)],
      ["市值加权 / 类型", `${pct(summary.cap_return_1d)} / ${text(summary.cap_type)}`],
      ["成员", `${text(summary.n_valid)}/${text(summary.n_expected)}`],
      ["覆盖", ratio(summary.count_coverage)],
      ["总榜排名", text(summary.headline_rank)],
      ["质量", text(summary.snapshot_quality_grade)],
      ["中位数", pct(payload.distribution && payload.distribution.median_return_1d)],
      ["MAD", pct(payload.distribution && payload.distribution.dispersion_mad)],
      ["截尾下界", pct(payload.distribution && payload.distribution.winsor_lower)],
      ["截尾上界", pct(payload.distribution && payload.distribution.winsor_upper)],
    ];
    values.forEach(([label, value]) => {
      const card = document.createElement("div");
      card.className = "ga-summary-card";
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = value;
      span.textContent = label;
      card.append(strong, span);
      cards.appendChild(card);
    });
    const reasons = document.getElementById("ga-detail-reasons");
    if (reasons) {
      reasons.textContent = `质量原因：${reasonText(summary.reason_codes)} · 被截尾 ${text(payload.distribution && payload.distribution.n_winsorized)} 只${payload.distribution && payload.distribution.winsorized_tickers && payload.distribution.winsorized_tickers.length ? `（${payload.distribution.winsorized_tickers.join(", ")}）` : ""}`;
    }
    const provenance = payload.provenance || {};
    renderDefinitions(document.getElementById("ga-detail-provenance"), {
      "run / schema": `${text(payload.data_run_id)} / ${text(payload.schema_version)}`,
      "股票池 / 版本": `${text(provenance.universe)} / ${text(provenance.universe_version)}`,
      "分类 / 层级": `${text(provenance.taxonomy)} / ${text(provenance.taxonomy_level)}`,
      "分类版本": provenance.taxonomy_version,
      "分类日期 / 提供方": `${text(provenance.classification_asof)} / ${text(provenance.classification_provider)}`,
      "分类 hash": provenance.classification_hash,
      "稳定 ID 映射": provenance.group_id_mapping_version,
      "回退 / 拉取时间": `${text(provenance.fallback)} / ${text(provenance.fetched_at)}`,
      "PIT universe / classification": `${text(provenance.pit_universe_applied)} / ${text(provenance.pit_classification_applied)}`,
      "快照 UTC": formatInstant(payload.snapshot_time, "UTC", "UTC"),
    });
    const renderDrivers = (id, rows) => {
      const list = document.getElementById(id);
      if (!list) return;
      list.replaceChildren();
      (rows || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${text(item.ticker)} · ${pct(item.contribution, 3)} (${num(item.contribution_bps, 2)} bp)`;
        list.appendChild(li);
      });
      if (!(rows || []).length) {
        const li = document.createElement("li");
        li.textContent = "—";
        list.appendChild(li);
      }
    };
    renderDrivers("ga-top-positive", payload.contribution_drivers && payload.contribution_drivers.top_positive);
    renderDrivers("ga-top-negative", payload.contribution_drivers && payload.contribution_drivers.top_negative);
    renderMemberRows(payload);
    document.getElementById("ga-prev-page").disabled = payload.members.page <= 1;
    document.getElementById("ga-next-page").disabled = !payload.members.has_next;
    syncDetailURL();
  }

  async function initDetail() {
    const current = new URLSearchParams(location.search);
    const allowedSorts = new Set(["ticker", "raw_return_1d", "headline_contribution", "is_valid_for_headline"]);
    const allowedPageSizes = new Set([25, 50, 100, 200]);
    const parsedPage = Number.parseInt(current.get("page") || "1", 10);
    const parsedSize = Number.parseInt(current.get("page_size") || "50", 10);
    const requestedSort = current.get("member_sort_by") || "ticker";
    const requestedOrder = current.get("member_sort_order") || "asc";
    detailState = {
      page: Number.isInteger(parsedPage) && parsedPage > 0 ? parsedPage : 1,
      pageSize: allowedPageSizes.has(parsedSize) ? parsedSize : 50,
      sortBy: allowedSorts.has(requestedSort) ? requestedSort : "ticker",
      sortOrder: ["asc", "desc"].includes(requestedOrder) ? requestedOrder : "asc",
      search: current.get("member_search") || "",
      groupId: root.dataset.groupId,
      runId: root.dataset.runId,
      level: root.dataset.level,
    };
    const sortBy = document.getElementById("ga-member-sort-by");
    const sortOrder = document.getElementById("ga-member-sort-order");
    const pageSize = document.getElementById("ga-member-page-size");
    const search = document.getElementById("ga-member-search");
    sortBy.value = detailState.sortBy;
    sortOrder.value = detailState.sortOrder;
    pageSize.value = String(detailState.pageSize);
    search.value = detailState.search;

    const load = async () => {
      clearError();
      const params = new URLSearchParams({
        page: String(detailState.page),
        page_size: String(detailState.pageSize),
        member_sort_by: detailState.sortBy,
        member_sort_order: detailState.sortOrder,
        universe: "SP500",
        taxonomy: "FMP",
        level: detailState.level,
        mode: "eod",
      });
      if (detailState.runId) params.set("data_run_id", detailState.runId);
      try {
        renderDetail(await getJSON(`/api/group-analytics/groups/${encodeURIComponent(detailState.groupId)}?${params}`));
      } catch (error) {
        showError(error.message);
      }
    };
    const reloadFromControls = () => {
      detailState.page = 1;
      detailState.sortBy = sortBy.value;
      detailState.sortOrder = sortOrder.value;
      detailState.pageSize = Number(pageSize.value);
      load();
    };
    sortBy.addEventListener("change", reloadFromControls);
    sortOrder.addEventListener("change", reloadFromControls);
    pageSize.addEventListener("change", reloadFromControls);
    search.addEventListener("input", () => {
      detailState.search = search.value;
      syncDetailURL();
      if (lastDetailPayload) renderMemberRows(lastDetailPayload);
    });
    document.getElementById("ga-prev-page").addEventListener("click", () => {
      if (detailState.page > 1) {
        detailState.page -= 1;
        load();
      }
    });
    document.getElementById("ga-next-page").addEventListener("click", () => {
      detailState.page += 1;
      load();
    });
    await load();
  }

  root.dataset.gaPage === "heat" ? initHeat() : initDetail();
})();
