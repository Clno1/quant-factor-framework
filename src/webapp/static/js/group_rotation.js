(() => {
  "use strict";
  const root = document.getElementById("rotation-main");
  if (!root) return;
  const el = id => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const pct = v => typeof v === "number" && Number.isFinite(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : "—";
  const actions = {priority:"优先核对",price_watch:"价格领先／待广度",watch:"转强观察",extended:"延伸偏大",caution:"降温或落后",wait:"等待确认",unavailable:"数据不足"};
  let data, selection, detailSequence = 0;
  const query = new URLSearchParams(location.search);
  async function get(url) { const r = await fetch(url, {credentials:"same-origin"}); const j = await r.json(); if (!r.ok) throw new Error(j.detail || "快照读取失败"); return j; }
  function card(title, value, note) { const c = node("article", undefined, "rotation-card"); c.append(node("h2", title), node("strong", value), node("p", note)); return c; }
  function render() {
    const cohort = el("rotation-cohort").value, sort = el("rotation-sort").value;
    const rows = data.rows.filter(r => r.cohort === cohort).sort((a,b) => (b.production[sort] ?? -Infinity) - (a.production[sort] ?? -Infinity) || a.id.localeCompare(b.id));
    const candidates = rows.filter(r => ["priority","price_watch"].includes(r.production.action));
    const improving = rows.filter(r => r.production.action === "watch");
    el("rotation-cards").replaceChildren(
      card("持续领先 · 先核对", candidates.slice(0,2).map(r=>r.name).join("、") || "暂无合格方向", "须区分价格领先与成员广度确认；不是买点"),
      card("转强观察", improving.slice(0,2).map(r=>r.name).join("、") || "等待新的改善", "短期修复仍需月度强弱及个股形态确认"),
      card("背景与分歧", data.context.label, "独立实验层，不改变主题分数或核心仓位")
    );
    const tbody = el("rotation-rows"); tbody.replaceChildren();
    for (const r of rows) {
      const p = r.production, tr = node("tr"); if (selection === r.id) tr.className = "selected";
      const first = node("td"), button = node("button", r.name); button.type = "button"; button.setAttribute("aria-pressed", selection === r.id ? "true" : "false"); button.addEventListener("click", () => select(r.id));
      first.append(button, node("span", `${r.proxy || "等权篮子"} · ${p.state_name}${p.boundary ? " · 边界附近" : ""}`, "rotation-sub")); tr.append(first);
      for (const [key,label] of [["rs5","5日相对"],["rs20","20日相对"],["rs60","60日相对"]]) { const td=node("td", pct(p[key]), p[key]>0?"rotation-positive":p[key]<0?"rotation-negative":"");td.dataset.label=label;tr.append(td); }
      const breadth=node("td", p.breadth === null ? (r.members.length?"无有效样本":"未接入") : `${p.breadth.toFixed(0)}% · 样本${p.breadth_n}/${p.breadth_expected}`);breadth.dataset.label="站20日线广度";tr.append(breadth);
      const action=node("td",actions[p.action]);action.dataset.label="研究优先级";tr.append(action);tbody.append(tr);
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
      const r=detail.theme,p=r.production,c=r.compatibility;box.replaceChildren(node("h2",r.name),node("p",`${p.state_name} · ${actions[p.action]} · 本次回放窗口内确认起点：${p.state_since || "尚未确认"}`));
      if(p.confirmation_count)box.append(node("p",`新候选确认 ${p.confirmation_count}/${p.confirmation_required}；尚未替换已确认状态。`));
      const back=node("button","↑ 返回主题列表");back.type="button";back.addEventListener("click",()=>el("rotation-cohort").scrollIntoView({block:"start"}));box.append(back);
      box.append(node("p",`绝对5日 ${pct(p.abs5)}，绝对20日 ${pct(p.abs20)}。${r.price_response || "背景不足，独立观察价格"}`));
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
      const compare=node("details"),summary=node("summary","公开基础版规则对账（不是生产交易分数）");compare.append(summary);
      compare.append(node("p",`${r.compatibility_scope==="public_v1"?"公开v1规则":"本项目行业ETF扩展，非作者原11主题"} · 原版分数 ${c.score ?? "—"} · 原版标签 ${c.source_state}`));
      const parts=node("div",undefined,"rotation-components");for(const [key,label] of [["trend_points","趋势"],["acceleration_points","改善"],["volume_points","量能"],["breadth_points","广度/代理"],["extension_points","未延伸"]])parts.append(node("span",`${label} ${c[key]}`));compare.append(parts);
      compare.append(node("p",`原版金额倍数 ${c.amount_ratio?.toFixed(2) ?? "—"}；广度读数 ${c.breadth?.toFixed(0) ?? "—"}${r.proxy?"（ETF趋势70/30代理，非成员百分比）":"（篮子成员比例）"}。`));
      const json=node("a","查看完整对账中间值 JSON");json.href=`/api/group-analytics/rotation/${encodeURIComponent(id)}?run=${encodeURIComponent(data.run_id)}`;json.target="_blank";json.rel="noopener";compare.append(json);box.append(compare);
      box.scrollIntoView({block:"start"});
    }catch(e){if(seq===detailSequence)box.replaceChildren(node("p",e.message));}
  }
  el("rotation-cohort").addEventListener("change",render);el("rotation-sort").addEventListener("change",render);
  get("/api/group-analytics/rotation"+(query.get("run")?"?run="+encodeURIComponent(query.get("run")):""))
    .then(snapshot=>{data=snapshot;el("rotation-status").textContent=`截至 ${data.source_session} 完整收盘 · 有效主题 ${data.valid_theme_count}/${data.total_theme_count} · 不含实时盘前行情${data.freshness!=="current"?" · 历史/新鲜度未确认，勿当作今日信号":""}${data.last_attempt?.status==="FAILED"?" · 最近构建失败，仍展示上次成功快照":""}`;if(data.freshness!=="current" || data.last_attempt?.status==="FAILED")el("rotation-status").classList.add("warn");el("rotation-audit").textContent=`快照 ${data.run_id} · 两个profile独立保存 · ${data.notes.join("；")}`;el("rotation-content").hidden=false;render();})
    .catch(e=>{el("rotation-status").textContent="轮动数据尚未就绪";el("rotation-error").textContent=e.message;el("rotation-error").hidden=false;});
})();
