(() => {
  "use strict";
  const root = document.getElementById("rotation-main");
  if (!root) return;
  const el = id => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const pct = v => typeof v === "number" && Number.isFinite(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : "—";
  const actions = {
    unavailable:"数据不足", focus:"持续领先", defensive:"相对抗跌", recover:"正在修复",
    weak:"持续落后", neutral:"与基准同步",
    priority:"优先核对", price_watch:"价格领先／待广度", watch:"转强观察",
    extended:"延伸偏大", caution:"降温或落后", wait:"等待确认"
  };
  const riskZh = {EXTENDED:"延伸偏大", BELOW_ABSOLUTE_MA20:"低于绝对20日均线", ABSOLUTE_DOWNTREND:"绝对下跌"};
  const gapZh = {
    ETF_HOLDINGS_NOT_LINKED:"真实广度未接入", SMALL_BASKET:"小样本篮子",
    LOW_PARTICIPATION:"参与度低", STATE_CONFIRMING:"新状态确认中",
    NO_QUALIFIED_CANDIDATE:"扫描正常，暂无合格形态", LINKAGE_FAILED:"个股关联失败",
    INSUFFICIENT_HISTORY:"历史不足", HOLDINGS_OBSERVATION_STALE:"持仓观测过期未用",
    HOLDINGS_MEASUREMENT_FAILED:"持仓已接入但成员价格不足",
    PARTIAL_HOLDINGS_COVERAGE:"仅部分持仓观察", HOLDINGS_NOT_POINT_IN_TIME:"当前持仓不用于历史时点"
  };
  const v3Head = ["主题","强弱","速度","5日相对","20日相对","60日相对","趋势","成交活跃","真实广度","净申赎","风险","研究优先级"];
  const v2Head = ["主题 / 状态","5日相对","20日相对","60日相对","真实广度","研究优先级"];
  let data, selection, detailSequence = 0;
  const query = new URLSearchParams(location.search);
  async function get(url) { const r = await fetch(url, {credentials:"same-origin"}); const j = await r.json(); if (!r.ok) throw new Error(j.detail || "快照读取失败"); return j; }
  function card(title, value, note) { const c = node("article", undefined, "rotation-card"); c.append(node("h2", title), node("strong", value), node("p", note)); return c; }
  function isLegacy() { return Boolean(data && data.schema_legacy); }
  function actionName(action) { return actions[action] || action || "—"; }
  function fillHead() {
    const row = document.querySelector(".rotation-table thead tr");
    if (!row) return;
    row.replaceChildren(...(isLegacy() ? v2Head : v3Head).map(title => node("th", title)));
  }
  function riskText(p) {
    const flags = Array.isArray(p.risk_flags) ? p.risk_flags : [];
    return flags.length ? flags.map(f => riskZh[f] || f).join(" · ") : "—";
  }
  function amountText(p) {
    if (p.amount_label) {
      const multiple = typeof p.amount_ratio === "number" && Number.isFinite(p.amount_ratio) ? ` ${p.amount_ratio.toFixed(2)}×` : "";
      return p.amount_label + multiple;
    }
    return "—";
  }
  function holdingsBreadth(r) { return r.holdings_breadth || {}; }
  function breadthText(r, p) {
    const hb = holdingsBreadth(r);
    if (hb.breadth_kind === "etf_holdings_observation") {
      const equal = hb.breadth_equal_weight_pct, weighted = hb.breadth_weighted_pct;
      if (typeof equal !== "number" || !Number.isFinite(equal)) return "当前持仓观测 · 成员价格不足";
      let text = `样本内参与 等权${equal.toFixed(0)}%`;
      if (typeof weighted === "number" && Number.isFinite(weighted)) text += ` / 加权${weighted.toFixed(0)}%`;
      if (hb.breadth_eligible_members != null && hb.breadth_mapped_members != null) {
        text += ` · 样本${hb.breadth_eligible_members}/${hb.breadth_mapped_members}`;
      }
      if (typeof hb.measured_fund_weight_pct === "number" && Number.isFinite(hb.measured_fund_weight_pct)) {
        text += ` · 已测基金权重${hb.measured_fund_weight_pct.toFixed(0)}%`;
      }
      if (hb.measurement_complete === false) text += " · 仅部分持仓观察";
      return text;
    }
    if (hb.status === "HOLDINGS_OBSERVATION_STALE") return "持仓过期未用";
    if (hb.status === "HOLDINGS_NOT_POINT_IN_TIME") return "当前持仓不用于历史时点";
    if (p.breadth === null || p.breadth === undefined) return (hb.breadth_kind === "member_above_ma" || (r.definition && r.definition.members && r.definition.members.length)) ? "无有效样本" : "未接入";
    return `${p.breadth.toFixed(0)}% · 样本${p.breadth_n}/${p.breadth_expected}${p.breadth_n < 5 ? "（小样本）" : ""}`;
  }
  function flowText(r) {
    const flow = r.net_creation || {};
    if (flow.status === "not_applicable") return "不适用";
    if (flow.status === "available" && typeof flow.daily === "number" && Number.isFinite(flow.daily)) {
      const label = flow.label ? `${flow.label} · ` : "";
      return `${label}${flow.daily.toFixed(0)}`;
    }
    return "—";
  }
  function displayGaps(r, p) {
    const hb = holdingsBreadth(r);
    let gaps = [...(r.evidence_gaps || p.evidence_gaps || [])];
    const overlayStatus = hb.status || "";
    if (hb.breadth_kind === "etf_holdings_observation"
        || overlayStatus === "HOLDINGS_OBSERVATION_STALE"
        || overlayStatus === "HOLDINGS_MEASUREMENT_FAILED"
        || overlayStatus === "HOLDINGS_NOT_POINT_IN_TIME") {
      gaps = gaps.filter(gap => gap !== "ETF_HOLDINGS_NOT_LINKED");
    }
    for (const extra of hb.observation_gaps || []) {
      if (!gaps.includes(extra)) gaps.push(extra);
    }
    return gaps;
  }
  function appendTd(tr, text, label, cls) {
    const td = node("td", text, cls); td.dataset.label = label; tr.append(td); return td;
  }
  function subtitle(r, p) {
    if (isLegacy()) {
      return `${r.proxy || "等权篮子"} · 已确认：${p.state_name}${p.boundary ? " · 边界附近" : ""}${p.confirmation_count ? ` · 新状态待确认 ${p.confirmation_count}/${p.confirmation_required}` : ""}`;
    }
    const combined = p.combined_label || p.state_name || "";
    const count = p.strength_confirmation_count || p.confirmation_count;
    const required = p.confirmation_required || 2;
    const badge = count ? ` · 新强弱待确认 ${count}/${required}` : "";
    return `${r.proxy || "等权篮子"} · ${combined}${badge}`;
  }
  function render() {
    const cohort = el("rotation-cohort").value, sort = el("rotation-sort").value;
    const rows = data.rows.filter(r => r.cohort === cohort).sort((a,b) => (b.production[sort] ?? -Infinity) - (a.production[sort] ?? -Infinity) || a.id.localeCompare(b.id));
    const focus = rows.filter(r => ["focus","priority","price_watch"].includes(r.production.action));
    const recover = rows.filter(r => ["recover","watch"].includes(r.production.action));
    el("rotation-cards").replaceChildren(
      card("持续领先", focus.slice(0,2).map(r=>r.name).join("、") || "暂无合格方向", "价格持续跑赢基准且绝对趋势向上；不是买点"),
      card("正在修复", recover.slice(0,2).map(r=>r.name).join("、") || "等待新的改善", "落后但近端加速，仍需月度强弱及个股形态确认"),
      card("背景与分歧", data.context.label, "独立实验层，不改变主题分数或核心仓位")
    );
    fillHead();
    const tbody = el("rotation-rows"); tbody.replaceChildren();
    for (const r of rows) {
      const p = r.production, tr = node("tr"); if (selection === r.id) tr.className = "selected";
      const first = node("td"), button = node("button", r.name); button.type = "button"; button.setAttribute("aria-pressed", selection === r.id ? "true" : "false"); button.addEventListener("click", () => select(r.id));
      first.append(button, node("span", subtitle(r, p), "rotation-sub")); first.dataset.label = "主题"; tr.append(first);
      if (isLegacy()) {
        for (const [key,label] of [["rs5","5日相对"],["rs20","20日相对"],["rs60","60日相对"]]) {
          appendTd(tr, pct(p[key]), label, p[key]>0?"rotation-positive":p[key]<0?"rotation-negative":"");
        }
        appendTd(tr, breadthText(r, p), "站20日线广度");
        appendTd(tr, actionName(p.action), "研究优先级");
      } else {
        appendTd(tr, p.strength_label || "—", "强弱");
        appendTd(tr, p.speed_label || "—", "速度");
        for (const [key,label] of [["rs5","5日相对"],["rs20","20日相对"],["rs60","60日相对"]]) {
          appendTd(tr, pct(p[key]), label, p[key]>0?"rotation-positive":p[key]<0?"rotation-negative":"");
        }
        appendTd(tr, r.compatibility?.source_trend || "—", "趋势");
        appendTd(tr, amountText(p), "成交活跃");
        appendTd(tr, breadthText(r, p), "真实广度");
        appendTd(tr, flowText(r), "净申赎");
        appendTd(tr, riskText(p), "风险", riskText(p) !== "—" ? "rotation-risk" : "");
        appendTd(tr, actionName(p.action), "研究优先级");
      }
      tbody.append(tr);
    }
    if (!rows.some(r=>r.id===selection)) { selection=undefined; el("rotation-detail").replaceChildren(node("p","点击主题，查看历史、个股候选与原版对账。")); detailSequence++; }
  }
  function chart(history) {
    const width=Math.min(640,Math.max(280,root.clientWidth-46));
    const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox",`0 0 ${width} 170`);svg.setAttribute("role","img");svg.setAttribute("aria-label","主题相对基准价格比历史，起点归一为100；缺失处断线");
    const valid=history.filter(r=>r.ratio!==null);if(valid.length<2)return node("p","连续历史不足，暂不绘图。");
    const base=valid[0].ratio, values=valid.map(r=>r.ratio/base*100), lo=Math.min(...values), hi=Math.max(...values), span=Math.max(hi-lo,1);
    let segment=[];
    const flush=()=>{if(segment.length){const line=document.createElementNS(ns,"polyline");line.setAttribute("points",segment.join(" "));line.setAttribute("fill","none");line.setAttribute("stroke","currentColor");line.setAttribute("stroke-width","2");svg.append(line);segment=[];}};
    history.forEach((r,i)=>{if(r.ratio===null){flush();return;}segment.push(`${48+i/(history.length-1)*(width-65)},${132-(r.ratio/base*100-lo)/span*105}`);});flush();
    for(const [x,y,t] of [[2,26,hi.toFixed(1)],[2,135,lo.toFixed(1)],[48,162,history[0].date],[width-95,162,history.at(-1).date]]){const n=document.createElementNS(ns,"text");n.setAttribute("x",x);n.setAttribute("y",y);n.textContent=t;svg.append(n);}return svg;
  }
  async function select(id) {
    selection=id;render();const seq=++detailSequence, box=el("rotation-detail");box.replaceChildren(node("p","正在读取固定快照详情…"));
    try {
      const detail=await get(`/api/group-analytics/rotation/${encodeURIComponent(id)}?run=${encodeURIComponent(data.run_id)}`);if(seq!==detailSequence)return;
      const r=detail.theme,p=r.production,c=r.compatibility;
      const heading = p.combined_label || p.state_name;
      box.replaceChildren(node("h2",r.name),node("p",`${heading} · ${actionName(p.action)} · 本次回放窗口内确认起点：${p.state_since || "尚未确认"}`));
      const count = p.strength_confirmation_count || p.confirmation_count;
      if(count)box.append(node("p",`新强弱候选确认 ${count}/${p.confirmation_required || 2}；尚未替换已确认月度强弱。速度按当日直出。`));
      const back=node("button","↑ 返回主题列表");back.type="button";back.addEventListener("click",()=>el("rotation-cohort").scrollIntoView({block:"start"}));box.append(back);
      box.append(node("p",`绝对5日 ${pct(p.abs5)}，绝对20日 ${pct(p.abs20)}。${r.price_response || "背景不足，独立观察价格"}`));
      if (p.amount_label) box.append(node("p", `成交活跃：${amountText(p)}`));
      const hb = holdingsBreadth(r);
      if (hb.breadth_kind === "etf_holdings_observation") {
        box.append(node("p", `${hb.note || "当前持仓观测广度（持仓生效日未披露）"}：${breadthText(r, p)}`));
      } else if (hb.note) {
        box.append(node("p", `真实广度：${breadthText(r, p)}。${hb.note}`));
      }
      const flow = r.net_creation || {};
      if (flow.status === "not_applicable") box.append(node("p", "净申赎：不适用（自建篮子没有ETF份额）"));
      else if (flow.status === "available") box.append(node("p", `净申赎：${flowText(r)} · 数据日 ${flow.asof || "—"}`));
      else box.append(node("p", flow.note || "净申赎未接入；不是成交额"));
      const flags = Array.isArray(p.risk_flags) ? p.risk_flags : [];
      if (flags.length) box.append(node("p", "风险标记：" + flags.map(f => riskZh[f] || f).join("、")));
      const gaps = displayGaps(r, p);
      if (gaps.length) {
        const list = node("ul");
        for (const gap of gaps) list.append(node("li", gapZh[gap] || gap));
        box.append(node("h3","数据完备度"), list);
      }
      if(data.context.accepted?.length){
        const bg=node("details");bg.append(node("summary","背景证据清单（实验，不是胜率）"));
        bg.append(node("p",`目标基准 ${data.context.target_benchmark || "不明确"} · 支持过线家族 ${data.context.support ?? "—"} · 压制过线家族 ${data.context.pressure ?? "—"}；独立于主题评分。`));
        const family={rates:"利率",dollar:"美元",credit:"信用",growth:"增长",inflation:"通胀",liquidity:"流动性",events:"事件"};
        for(const e of data.context.accepted)bg.append(node("p",`${family[e.family] || e.family} · ${e.direction==="support"?"支持":"压制"} · 所供证据强度 ${e.strength.toFixed(2)} · 已知时点 ${e.known_at} · 规则 ${e.rule_version}`));
        box.append(bg);
      }
      box.append(node("h3","相对基准价格轨迹 · 首个可见点=100"),chart(r.history));
      box.append(node("h3","下一步与失效"),node("p",r.confirmation || "观察月度相对优势及个股突破确认"),node("p",r.invalidation || "相对优势消失或突破形态失效时重新评估"));
      box.append(node("h3","关联动量突破候选"),node("p",r.candidate_basis || "尚未关联"));
      if(!r.candidates?.length)box.append(node("p",data.candidate_linkage?.status==="available"?"暂无已关联的合格个股形态；不代表未覆盖股票一定弱。":data.candidate_linkage?.reason || "同日候选尚未接入"));
      for(const stock of r.candidates || []){const item=node("div",undefined,"rotation-candidate"),a=node("a",stock.ticker);a.href=stock.href;item.append(a,node("span",`${stock.status_name} · 原分数 ${stock.score ?? "—"} · 既有参考位 ${stock.pivot.toFixed(2)}`),node("p",stock.confirmation));box.append(item);}
      if(r.members.length){box.append(node("h3","篮子成员"),node("p",r.members.map(m=>`${m.ticker} ${m.above_ma20===null?"数据不足":m.above_ma20?"站上20日线":"未站上20日线"}`).join(" · ")));}
      const warning=node("ul");for(const w of r.warnings)warning.append(node("li",w));box.append(warning);
      const compare=node("details"),summary=node("summary","公开基础版规则对账（参考版规则对照，非本项目结论）");compare.append(summary);
      compare.append(node("p",`${r.compatibility_scope==="public_v1"?"公开v1规则":"本项目行业ETF扩展，非作者原11主题"} · 原版分数 ${c.score ?? "—"} · 原版标签 ${c.source_state}`));
      const parts=node("div",undefined,"rotation-components");for(const [key,label] of [["trend_points","趋势"],["acceleration_points","改善"],["volume_points","量能"],["breadth_points","广度/代理"],["extension_points","未延伸"]])parts.append(node("span",`${label} ${c[key]}`));compare.append(parts);
      compare.append(node("p",`原版金额倍数 ${c.amount_ratio?.toFixed(2) ?? "—"}；广度读数 ${c.breadth?.toFixed(0) ?? "—"}${r.proxy?"（ETF趋势70/30代理，非成员百分比）":"（篮子成员比例）"}。`));
      const json=node("a","查看完整对账中间值 JSON");json.href=`/api/group-analytics/rotation/${encodeURIComponent(id)}?run=${encodeURIComponent(data.run_id)}`;json.target="_blank";json.rel="noopener";compare.append(json);box.append(compare);
      box.scrollIntoView({block:"start"});
    }catch(e){if(seq===detailSequence)box.replaceChildren(node("p",e.message));}
  }
  el("rotation-cohort").addEventListener("change",render);el("rotation-sort").addEventListener("change",render);
  const pinned = Boolean(query.get("run"));
  const POLL_MS = 5 * 60 * 1000;
  function nyParts(date) {
    const map = {};
    for (const part of new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", hourCycle: "h23", weekday: "short",
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
    }).formatToParts(date)) map[part.type] = part.value;
    return map;
  }
  function nextPriceUpdate(now) {
    const names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const t = nyParts(now);
    const dow = names.indexOf(t.weekday);
    const past = Number(t.hour) > 17 || (Number(t.hour) === 17 && Number(t.minute) >= 30);
    let add = 0, nextDow = dow;
    if (!(dow >= 1 && dow <= 5) || past) {
      add = 1;
      nextDow = (dow + 1) % 7;
      while (nextDow === 0 || nextDow === 6) { add += 1; nextDow = (nextDow + 1) % 7; }
    }
    const utc = Date.UTC(Number(t.year), Number(t.month) - 1, Number(t.day) + add);
    const day = new Date(utc);
    const y = day.getUTCFullYear();
    const m = String(day.getUTCMonth() + 1).padStart(2, "0");
    const d = String(day.getUTCDate()).padStart(2, "0");
    return `${y}-${m}-${d} 17:30 America/New_York`;
  }
  function ageLabel(iso, now) {
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return "生成时间未知";
    const mins = Math.max(0, Math.round((now - then) / 60000));
    if (mins < 60) return `${mins} 分钟前`;
    const hours = Math.floor(mins / 60);
    if (hours < 48) return `${hours} 小时前`;
    return `${Math.floor(hours / 24)} 天前`;
  }
  function fillStatus(now) {
    if (!data) return;
    const warn = data.freshness !== "current" || data.last_attempt?.status === "FAILED";
    el("rotation-asof").textContent = `数据截至 ${data.source_session} 完整收盘 · 有效主题 ${data.valid_theme_count}/${data.total_theme_count} · 不含实时盘前行情`;
    const generated = el("rotation-generated");
    generated.hidden = false;
    generated.textContent = `快照生成于 ${data.generated_at || "未知"} · 距今 ${ageLabel(data.generated_at, now)}`;
    const next = el("rotation-next");
    next.hidden = false;
    next.textContent = pinned
      ? "当前为固定历史快照，不会随最新发布自动切换"
      : `下次价格层更新约 ${nextPriceUpdate(now)}；关联层 13:15 Asia/Singapore，盘前 07:00 ET 可重试`;
    el("rotation-status").classList.toggle("warn", warn);
    if (data.freshness !== "current") {
      el("rotation-asof").textContent += " · 历史/新鲜度未确认，勿当作今日信号";
    }
    if (data.last_attempt?.status === "FAILED") {
      el("rotation-asof").textContent += " · 最近构建失败，仍展示上次成功快照";
    }
    if (data.schema_legacy) {
      el("rotation-asof").textContent += " · 当前为 v2.1 快照，主表已降级展示";
    }
  }
  function showFrozen() {
    el("rotation-frozen").hidden = !pinned;
  }
  async function checkLatest() {
    if (document.visibilityState === "hidden" || !data) return;
    try {
      const latest = await get("/api/group-analytics/rotation");
      if (latest.run_id && latest.run_id !== data.run_id) el("rotation-refresh").hidden = false;
      if (pinned) return;
      if (latest.run_id && latest.run_id === data.run_id) {
        data.freshness = latest.freshness;
        data.last_attempt = latest.last_attempt;
        data.valid_theme_count = latest.valid_theme_count;
        data.total_theme_count = latest.total_theme_count;
        fillStatus(new Date());
      }
    } catch (_error) { /* keep the visible snapshot; retry on the next poll */ }
  }
  el("rotation-refresh-btn").addEventListener("click", () => { location.href = "/group-analytics"; });
  get("/api/group-analytics/rotation"+(query.get("run")?"?run="+encodeURIComponent(query.get("run")):""))
    .then(snapshot=>{
      data=snapshot;
      el("rotation-audit").textContent=`快照 ${data.run_id} · 两个profile独立保存 · ${data.notes.join("；")}`;
      el("rotation-content").hidden=false;
      fillStatus(new Date());
      showFrozen();
      render();
      document.addEventListener("visibilitychange", () => { fillStatus(new Date()); checkLatest(); });
      setInterval(() => { fillStatus(new Date()); checkLatest(); }, POLL_MS);
    })
    .catch(e=>{el("rotation-asof").textContent="轮动数据尚未就绪";el("rotation-error").textContent=e.message;el("rotation-error").hidden=false;});
})();
