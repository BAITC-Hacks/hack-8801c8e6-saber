(() => {
  "use strict";

  const SLOT_COUNT = 5;

  const state = {
    config: null,
    districts: [],
    measures: [],
    districtsById: {},
    measuresById: {},
    baseResult: null,
    baseNCrit: 2,
    slots: Array.from({ length: SLOT_COUNT }, () => ({ measureId: "", districtId: "" })),
    lastSimulate: null, // {result, errors}
    indicatorView: "D",
    selectedTileId: null,
    leaderboard: [],
  };

  const el = (id) => document.getElementById(id);

  function fmt2(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtSigned2(n) {
    const sign = n > 0 ? "+" : n < 0 ? "" : "+";
    return sign + fmt2(n);
  }

  async function api(path, method, body) {
    const res = await fetch(path, {
      method: method || "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let json;
    try {
      json = await res.json();
    } catch (e) {
      throw new Error("Некорректный ответ сервера");
    }
    if (!res.ok) {
      const err = new Error("Ошибка запроса");
      err.status = res.status;
      err.body = json;
      throw err;
    }
    return json;
  }

  // ---------- Availability / rule checks (mirrors app/engine.py rules) ----------

  function activeSlotDecisions(excludeIndex) {
    return state.slots
      .map((s, i) => ({ ...s, i }))
      .filter((s) => s.i !== excludeIndex && s.measureId);
  }

  function directionCounts(excludeIndex) {
    const counts = {};
    for (const s of activeSlotDecisions(excludeIndex)) {
      const m = state.measuresById[s.measureId];
      if (!m) continue;
      counts[m.direction] = (counts[m.direction] || 0) + 1;
    }
    return counts;
  }

  function totalCost(excludeIndex) {
    return activeSlotDecisions(excludeIndex).reduce((sum, s) => {
      const m = state.measuresById[s.measureId];
      return sum + (m ? m.cost : 0);
    }, 0);
  }

  function measureAvailability(slotIndex) {
    const others = activeSlotDecisions(slotIndex);
    const otherIds = new Set(others.map((s) => s.measureId));
    const counts = directionCounts(slotIndex);
    const spent = totalCost(slotIndex);
    const budget = state.config.budget;
    const maxPerDir = state.config.max_per_direction;
    const incompat = state.config.incompatibilities;

    const result = {};
    for (const m of state.measures) {
      let disabled = false;
      let reason = "";

      if (otherIds.has(m.id)) {
        disabled = true;
        reason = "уже выбрано";
      } else if ((counts[m.direction] || 0) >= maxPerDir) {
        disabled = true;
        reason = `лимит направления ${maxPerDir}/${maxPerDir}`;
      } else {
        const budgetLeft = budget - spent;
        if (m.cost > budgetLeft) {
          disabled = true;
          reason = `не хватает ${fmt2(m.cost - budgetLeft)} усл. ед.`;
        } else {
          for (const inc of incompat) {
            const [a, b] = inc.pair;
            const other = a === m.id ? b : b === m.id ? a : null;
            if (!other) continue;
            if (!inc.same_district_only && otherIds.has(other)) {
              disabled = true;
              reason = `несовместимо с ${other}`;
              break;
            }
          }
        }
      }

      result[m.id] = { disabled, reason };
    }
    return result;
  }

  function districtAvailability(slotIndex, measureId) {
    const others = activeSlotDecisions(slotIndex);
    const incompat = state.config.incompatibilities.filter((i) => i.same_district_only && i.pair.includes(measureId));
    const result = {};
    for (const d of state.districts) {
      let disabled = false;
      let reason = "";
      for (const inc of incompat) {
        const [a, b] = inc.pair;
        const other = a === measureId ? b : a;
        const conflicting = others.find((s) => s.measureId === other && s.districtId === d.id);
        if (conflicting) {
          disabled = true;
          reason = `несовместимо с ${other} в этом районе`;
          break;
        }
      }
      result[d.id] = { disabled, reason };
    }
    return result;
  }

  function synergyHint(slotIndex) {
    const slot = state.slots[slotIndex];
    if (!slot.measureId) return "";
    const others = activeSlotDecisions(slotIndex).map((s) => s.measureId);
    for (const s of state.config.synergies) {
      const [a, b] = s.pair;
      let partner = null;
      if (a === slot.measureId) partner = b;
      else if (b === slot.measureId) partner = a;
      if (!partner || others.includes(partner)) continue;
      const partnerMeasure = state.measuresById[partner];
      const indName = indicatorName(s.indicator);
      const isHost = s.host === slot.measureId;
      if (isHost) {
        return `С «${partnerMeasure.name}» +${s.bonus} к ${indName} в этом районе`;
      }
      return `Добавьте «${partnerMeasure.name}» в том же районе — даст +${s.bonus} к ${indName}`;
    }
    return "";
  }

  function indicatorName(code) {
    const ind = state.config.indicators.find((i) => i.code === code);
    return ind ? ind.name : code;
  }

  function directionName(code) {
    return state.config.directions[code] || code;
  }

  // ---------- Rendering ----------

  function renderDirectionCounters() {
    const counts = directionCounts(-1);
    const maxPerDir = state.config.max_per_direction;
    const parts = Object.entries(state.config.directions).map(([code, name]) => {
      const n = counts[code] || 0;
      const full = n >= maxPerDir;
      return `<span class="${full ? "dir-full" : ""}">${name} ${n}/${maxPerDir}</span>`;
    });
    el("direction-counters").innerHTML = parts.join(" · ");
  }

  function buildMeasureOptionsHtml(slotIndex) {
    const avail = measureAvailability(slotIndex);
    const byDirection = {};
    for (const m of state.measures) {
      (byDirection[m.direction] = byDirection[m.direction] || []).push(m);
    }
    let html = '<option value="">— выберите меру —</option>';
    for (const [dirCode, list] of Object.entries(byDirection)) {
      html += `<optgroup label="${directionName(dirCode)}">`;
      for (const m of list) {
        const a = avail[m.id];
        const selected = state.slots[slotIndex].measureId === m.id ? "selected" : "";
        const disabled = a.disabled && !selected ? "disabled" : "";
        const label = `${m.name} (${m.cost} усл. ед., лаг ${m.lag} кв.)${a.disabled ? ` — ${a.reason}` : ""}`;
        html += `<option value="${m.id}" ${selected} ${disabled}>${label}</option>`;
      }
      html += "</optgroup>";
    }
    return html;
  }

  function buildDistrictOptionsHtml(slotIndex, measureId) {
    const avail = districtAvailability(slotIndex, measureId);
    let html = '<option value="">— выберите район —</option>';
    for (const d of state.districts) {
      const a = avail[d.id];
      const selected = state.slots[slotIndex].districtId === d.id ? "selected" : "";
      const disabled = a.disabled && !selected ? "disabled" : "";
      const label = `${d.name}${a.disabled ? ` — ${a.reason}` : ""}`;
      html += `<option value="${d.id}" ${selected} ${disabled}>${label}</option>`;
    }
    return html;
  }

  function effectsText(slotIndex) {
    const slot = state.slots[slotIndex];
    const m = state.measuresById[slot.measureId];
    if (!m) return "";
    const H = state.config.horizon_quarters;
    const share = (H - m.lag) / H;
    const rows = Object.entries(m.effects).map(([code, val]) => {
      const sign = val >= 0 ? "+" : "";
      return `<span class="eff-row">${indicatorName(code)} ${sign}${fmt2(val)} (${Math.round(share * 100)}% эффекта, лаг ${m.lag} кв.)</span>`;
    });
    return rows.join("");
  }

  function renderSlots() {
    const container = el("slots");
    container.innerHTML = "";
    for (let i = 0; i < SLOT_COUNT; i++) {
      const slot = state.slots[i];
      const measure = state.measuresById[slot.measureId];
      const wrap = document.createElement("div");
      wrap.className = "slot";

      const isCity = measure && measure.scope === "city";
      const isDistrict = measure && measure.scope === "district";

      wrap.innerHTML = `
        <div class="slot-title">Решение ${i + 1}</div>
        <select data-role="measure" data-slot="${i}">${buildMeasureOptionsHtml(i)}</select>
        ${
          isCity
            ? `<div class="slot-district-static">Весь город</div>`
            : `<select data-role="district" data-slot="${i}" ${isDistrict ? "" : "disabled"} ${isDistrict ? "" : 'style="visibility:hidden"'}>${buildDistrictOptionsHtml(i, slot.measureId)}</select>`
        }
        <div class="slot-effects">${effectsText(i)}</div>
        <div class="slot-hint">${synergyHint(i)}</div>
      `;
      container.appendChild(wrap);
    }

    container.querySelectorAll('select[data-role="measure"]').forEach((sel) => {
      sel.addEventListener("change", onMeasureChange);
    });
    container.querySelectorAll('select[data-role="district"]').forEach((sel) => {
      sel.addEventListener("change", onDistrictChange);
    });

    renderDirectionCounters();
  }

  function onMeasureChange(e) {
    const slotIndex = Number(e.target.dataset.slot);
    state.slots[slotIndex] = { measureId: e.target.value, districtId: "" };
    renderSlots();
    scheduleSimulate();
  }

  function onDistrictChange(e) {
    const slotIndex = Number(e.target.dataset.slot);
    state.slots[slotIndex].districtId = e.target.value;
    renderSlots();
    scheduleSimulate();
  }

  function colorClassForD(v) {
    if (v < 50) return "red";
    if (v <= 60) return "yellow";
    return "green";
  }

  function colorClassForIndicator(v) {
    if (v < 40) return "red";
    if (v <= 60) return "yellow";
    return "green";
  }

  function renderIndicatorSelect() {
    const sel = el("indicator-select");
    let html = '<option value="D">Балл D</option>';
    for (const ind of state.config.indicators) {
      html += `<option value="${ind.code}">${ind.name}</option>`;
    }
    sel.innerHTML = html;
    sel.value = state.indicatorView;
    sel.addEventListener("change", () => {
      state.indicatorView = sel.value;
      renderTiles();
    });
  }

  function currentResult() {
    return (state.lastSimulate && state.lastSimulate.result) || state.baseResult;
  }

  function renderTiles() {
    const result = currentResult();
    const container = el("district-tiles");
    container.innerHTML = "";
    if (!result) return;

    const byId = {};
    for (const dd of result.districts) byId[dd.id] = dd;

    for (const d of state.districts) {
      const dd = byId[d.id];
      let before, after, colorClass, unit;
      if (state.indicatorView === "D") {
        before = dd.d_before;
        after = dd.d_after;
        colorClass = colorClassForD(after);
      } else {
        before = dd.indicators_before[state.indicatorView];
        after = dd.indicators_after[state.indicatorView];
        colorClass = colorClassForIndicator(after);
      }

      const critical = result.critical.filter((c) => c.district_id === d.id);
      const tile = document.createElement("div");
      tile.className = `tile tile-${colorClass}`;
      tile.dataset.districtId = d.id;
      tile.innerHTML = `
        <div class="tile-name">${d.name}</div>
        <div class="tile-pop">${Math.round(d.population_share * 100)}% населения</div>
        <div class="tile-value">${fmt2(before)} → <span class="val-${colorClass}">${fmt2(after)}</span>${colorClass === "red" && state.indicatorView !== "D" ? " (критично)" : ""}</div>
        ${critical.length ? `<div class="tile-critical">Критично: ${critical.length}</div>` : ""}
        <div class="tile-profile">${d.profile}</div>
      `;
      tile.addEventListener("click", () => {
        state.selectedTileId = state.selectedTileId === d.id ? null : d.id;
        renderDistrictDetail();
      });
      container.appendChild(tile);
    }

    renderDistrictDetail();
  }

  function renderDistrictDetail() {
    const box = el("district-detail");
    if (!state.selectedTileId) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const result = currentResult();
    const dd = result.districts.find((d) => d.id === state.selectedTileId);
    if (!dd) {
      box.hidden = true;
      return;
    }
    let rows = "";
    for (const ind of state.config.indicators) {
      const before = dd.indicators_before[ind.code];
      const after = dd.indicators_after[ind.code];
      rows += `<tr><td>${ind.name}</td><td>${fmt2(before)}</td><td>${fmt2(after)}</td></tr>`;
    }
    box.hidden = false;
    box.innerHTML = `
      <h3>${dd.name}: показатели до/после</h3>
      <table>
        <thead><tr><th>Показатель</th><th>До</th><th>После</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function renderHeader() {
    const result = currentResult();
    if (!result) return;

    const spent = result.total_cost;
    const budget = state.config.budget;
    const over = spent > budget;
    const pct = Math.min(100, (spent / budget) * 100);
    el("budget-bar-fill").style.width = pct + "%";
    el("budget-bar-fill").classList.toggle("over", over);
    el("budget-bar-label").textContent = `${fmt2(spent)} / ${budget} усл. ед.`;

    const scoreBefore = state.baseResult.score_after;
    const scoreAfter = result.score_after;
    const delta = scoreAfter - scoreBefore;
    const deltaClass = delta >= 0 ? "delta-pos" : "delta-neg";
    el("score-display").innerHTML =
      `Score: ${fmt2(scoreBefore)} → ${fmt2(scoreAfter)} (<span class="${deltaClass}">${fmtSigned2(delta)}</span>)`;

    const critEl = el("crit-counter");
    critEl.textContent = `Критических: ${state.baseNCrit} → ${result.n_crit}`;
    critEl.classList.toggle("improved", result.n_crit < state.baseNCrit);
  }

  function renderContributionsChart() {
    const result = currentResult();
    const canvas = el("contrib-chart");
    if (!result || !window.Chart) return;

    const entries = Object.entries(result.contributions || {});
    const labels = entries.map(([mid]) => (state.measuresById[mid] ? state.measuresById[mid].name : mid));
    const values = entries.map(([, v]) => v);

    if (window.__contribChart) {
      window.__contribChart.data.labels = labels;
      window.__contribChart.data.datasets[0].data = values;
      window.__contribChart.update();
      return;
    }

    window.__contribChart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels,
        datasets: [{ label: "Вклад в Score", data: values, backgroundColor: "#00A3E0" }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } },
      },
    });
  }

  function renderCalcButton() {
    const filled = state.slots.every((s) => s.measureId);
    const errors = (state.lastSimulate && state.lastSimulate.errors) || [];
    const blocking = errors.filter((e) => e.code !== "INVALID_COUNT");
    const ok = filled && blocking.length === 0;
    el("btn-calc").disabled = !ok;
    el("btn-agent").disabled = !ok;

    const errBox = el("calc-errors");
    if (!filled) {
      errBox.textContent = "";
    } else if (blocking.length) {
      errBox.innerHTML = blocking.map((e) => e.message).join("<br>");
    } else {
      errBox.textContent = "";
    }
  }

  let simulateTimer = null;
  function scheduleSimulate() {
    clearTimeout(simulateTimer);
    simulateTimer = setTimeout(runSimulate, 150);
  }

  async function runSimulate() {
    const decisions = state.slots
      .filter((s) => s.measureId)
      .map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
    try {
      const body = await api("/api/simulate", "POST", { decisions });
      state.lastSimulate = body;
    } catch (e) {
      state.lastSimulate = null;
    }
    renderHeader();
    renderTiles();
    renderContributionsChart();
    renderSynergyBadges();
    renderCalcButton();
  }

  function renderSynergyBadges() {
    const result = currentResult();
    const box = el("synergy-badges");
    if (!result) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = (result.synergies_applied || [])
      .map((s) => `<span class="badge">Синергия: ${s.pair.map((m) => (state.measuresById[m] || {}).name || m).join(" + ")}</span>`)
      .join("");
  }

  function renderAnalysis(analysis, aiMode) {
    const box = el("analysis-result");
    const list = (arr) => (arr || []).map((x) => `<li>${x}</li>`).join("");
    box.innerHTML = `
      ${aiMode === "fallback" ? '<span class="badge badge-fallback">AI офлайн: шаблонный анализ</span>' : ""}
      <section><h3>Итог</h3><p>${analysis.summary}</p></section>
      <section><h3>Сильные стороны</h3><ul>${list(analysis.strengths)}</ul></section>
      <section><h3>Риски</h3><ul>${list(analysis.risks)}</ul></section>
      <section><h3>Последствия</h3><ul>${list(analysis.consequences)}</ul></section>
      <section><h3>Главный компромисс</h3><p>${analysis.main_tradeoff}</p></section>
    `;
  }

  async function onCalcClick() {
    const btn = el("btn-calc");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Считаем…';
    try {
      const decisions = state.slots.map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
      const team = el("team-input").value.trim() || "Команда";
      const body = await api("/api/scenario", "POST", { team, decisions });
      state.lastSimulate = { result: body.result, errors: [] };
      renderHeader();
      renderTiles();
      renderContributionsChart();
      renderSynergyBadges();
      renderAnalysis(body.analysis, body.ai_mode);
      await refreshLeaderboard();
    } catch (e) {
      el("analysis-result").innerHTML = `<p class="error-text">Не удалось получить ответ, попробуйте ещё раз.</p>`;
    } finally {
      btn.textContent = originalText;
      renderCalcButton();
    }
  }

  function renderAgentResult(body) {
    const box = el("agent-result");
    const hyps = (body.hypotheses || [])
      .map((h) => {
        const ok = h.valid;
        return `<li class="hypothesis-item">
          <span>${h.idea}</span>
          <span class="${ok ? "hypothesis-ok" : "hypothesis-bad"}">${ok ? "Score " + fmt2(h.score) + " ✓" : h.error || "отклонено"}</span>
        </li>`;
      })
      .join("");
    const delta = body.best_score - body.original_score;
    box.innerHTML = `
      ${body.ai_mode === "fallback" ? '<span class="badge badge-fallback">AI офлайн: шаблонный поиск</span>' : ""}
      <ul class="hypothesis-list">${hyps}</ul>
      <p><strong>${fmt2(body.original_score)} → ${fmt2(body.best_score)} (${fmtSigned2(delta)})</strong></p>
      <p>${body.explanation}</p>
      <button id="btn-apply" class="btn btn-primary btn-block">Применить</button>
    `;
    el("btn-apply").addEventListener("click", () => applyDecisions(body.best_decisions));
  }

  function applyDecisions(decisions) {
    state.slots = decisions.map((d) => ({ measureId: d.measure_id, districtId: d.district_id || "" }));
    renderSlots();
    scheduleSimulate();
  }

  async function onAgentClick() {
    const btn = el("btn-agent");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Ищем…';
    try {
      const decisions = state.slots.map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
      const body = await api("/api/optimize", "POST", { decisions });
      renderAgentResult(body);
    } catch (e) {
      el("agent-result").innerHTML = `<p class="error-text">Не удалось получить ответ, попробуйте ещё раз.</p>`;
    } finally {
      btn.textContent = originalText;
      renderCalcButton();
    }
  }

  async function refreshLeaderboard() {
    try {
      const body = await api("/api/leaderboard");
      state.leaderboard = body.leaderboard;
      renderLeaderboard();
    } catch (e) {
      // ignore
    }
  }

  function renderLeaderboard() {
    const tbody = el("leaderboard-body");
    tbody.innerHTML = "";
    for (const row of state.leaderboard) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${row.rank}</td><td>${row.team}</td><td>${fmt2(row.score)}</td><td>${fmt2(row.total_cost)}</td><td>${new Date(row.created_at).toLocaleString("ru-RU")}</td>`;
      const detailTr = document.createElement("tr");
      detailTr.className = "leaderboard-detail";
      detailTr.hidden = true;
      const decisionsText = row.decisions
        .map((d) => {
          const m = state.measuresById[d.measure_id];
          const name = m ? m.name : d.measure_id;
          const districtName = d.district_id && state.districtsById[d.district_id] ? state.districtsById[d.district_id].name : "";
          return districtName ? `${name} (${districtName})` : name;
        })
        .join(", ");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = decisionsText;
      detailTr.appendChild(td);
      tr.addEventListener("click", () => {
        detailTr.hidden = !detailTr.hidden;
      });
      tbody.appendChild(tr);
      tbody.appendChild(detailTr);
    }
  }

  async function init() {
    const stateBody = await api("/api/state");
    state.config = stateBody.config;
    state.districts = stateBody.districts;
    state.measures = stateBody.measures;
    for (const d of state.districts) state.districtsById[d.id] = d;
    for (const m of state.measures) state.measuresById[m.id] = m;
    state.baseResult = stateBody.result;
    state.baseNCrit = stateBody.result.n_crit;
    state.lastSimulate = { result: stateBody.result, errors: [] };

    renderIndicatorSelect();
    renderSlots();
    renderHeader();
    renderTiles();
    renderContributionsChart();
    renderSynergyBadges();
    renderCalcButton();

    el("btn-calc").addEventListener("click", onCalcClick);
    el("btn-agent").addEventListener("click", onAgentClick);

    await refreshLeaderboard();
  }

  document.addEventListener("DOMContentLoaded", () => {
    init().catch((e) => {
      console.error(e);
    });
  });
})();
