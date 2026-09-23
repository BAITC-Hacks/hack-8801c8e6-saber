(() => {
  "use strict";

  let SLOT_COUNT = 5;
  const DRAFT_KEY = "akim-draft-v1";
  const OBJECTIVES = {
    score: { label: "Максимальный Score", short: "Score", description: "Приоритет — общий Score города. Итоговый балл не станет ниже вашего плана." },
    weakest: { label: "Поддержка слабейшего района", short: "Слабейший район", description: "Приоритет — наибольший балл самого слабого района после решений. При равенстве выбираем больший Score. Общий Score может снизиться." },
    critical: { label: "Меньше критических показателей", short: "Критические", description: "Сначала сокращаем число показателей ниже 40, при равенстве выбираем больший Score. Общий Score может снизиться." },
  };
  const EXAMPLE = [
    { measureId: "M7", districtId: "nura" }, { measureId: "M8", districtId: "nura" },
    { measureId: "M10", districtId: "nura" }, { measureId: "M12", districtId: "" },
    { measureId: "M5", districtId: "saryarka" },
  ];

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
    leaderboardRequest: 0,
    revision: 0,
    pending: false,
    previewError: false,
    busy: {},
    undo: [],
    simulateController: null,
    ready: false,
    objective: "score",
    optimizationEpoch: 0,
    optimization: null,
    activeStrategy: null,
  };

  const el = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const decisionsOf = (slots) => slots.filter((s) => s.measureId).map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
  const sameDecision = (a, b) => a.measure_id === b.measure_id && a.district_id === b.district_id;

  function remember() {
    state.undo.push(state.slots.map((s) => ({ ...s })));
    if (state.undo.length > 20) state.undo.shift();
  }

  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({ slots: state.slots, team: el("team-input").value, objective: state.objective }));
      el("draft-status").textContent = "Черновик сохранён в этом браузере";
    } catch {
      el("draft-status").textContent = "Автосохранение недоступно в этом браузере";
    }
  }

  function restoreDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem(DRAFT_KEY));
      if (!draft || !Array.isArray(draft.slots) || draft.slots.length !== SLOT_COUNT) return;
      if (!draft.slots.every((s) => s && typeof s.measureId === "string" && typeof s.districtId === "string" &&
        (!s.measureId || state.measuresById[s.measureId]) && (!s.districtId || state.districtsById[s.districtId]))) return;
      state.slots = draft.slots.map((s) => ({ measureId: s.measureId, districtId: s.districtId, locked: s.locked === true && completeSlot(s) }));
      if (typeof draft.team === "string") el("team-input").value = draft.team.slice(0, 40);
      if (Object.hasOwn(OBJECTIVES, draft.objective)) state.objective = draft.objective;
      el("draft-status").textContent = "Черновик восстановлен";
    } catch { /* Ignore an unavailable or obsolete local draft. */ }
  }

  function completeSlot(s) {
    const m = state.measuresById[s.measureId];
    return Boolean(m && (m.scope === "city" || state.districtsById[s.districtId]));
  }

  function decisionLabel(d) {
    const m = state.measuresById[d.measure_id];
    const district = state.districtsById[d.district_id];
    return `${m ? m.name : d.measure_id}${district ? " · " + district.name : " · весь город"}`;
  }

  function clearOptimization() {
    state.optimizationEpoch += 1;
    state.optimization = null;
    state.activeStrategy = null;
    delete state.busy.agent;
    el("agent-result").textContent = "";
    el("strategies-panel").hidden = true;
  }

  function renderObjective() {
    el("objective-select").value = state.objective;
    el("objective-help").textContent = OBJECTIVES[state.objective].description;
  }

  function changeObjective(objective) {
    if (!Object.hasOwn(OBJECTIVES, objective) || objective === state.objective) return;
    state.objective = objective;
    clearOptimization();
    renderObjective();
    saveDraft();
    renderCalcButton();
    el("agent-result").innerHTML = '<p class="empty-state">Цель изменена. Сравните стратегии заново — советник учтёт новый приоритет.</p>';
  }

  function fmt2(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    if (Object.is(n, -0)) n = 0;
    return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtSigned2(n) {
    const sign = n > 0 ? "+" : n < 0 ? "" : "+";
    return sign + fmt2(n);
  }

  async function api(path, method, body, signal) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
    const timeoutMs = path === "/api/scenario" || path === "/api/optimize" ? 120000 : 10000;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      const res = await fetch(path, {
        method: method || "GET",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      let json;
      try {
        json = await res.json();
      } catch {
        throw new Error("Некорректный ответ сервера");
      }
      if (!res.ok) {
        const err = new Error("Ошибка запроса");
        err.status = res.status;
        err.body = json;
        throw err;
      }
      return json;
    } catch (error) {
      if (timedOut) {
        const timeout = new Error("Сервер не ответил вовремя");
        timeout.name = "TimeoutError";
        throw timeout;
      }
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
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
      wrap.className = `slot${slot.locked ? " slot-locked" : ""}`;

      const isCity = measure && measure.scope === "city";
      const isDistrict = measure && measure.scope === "district";

      wrap.innerHTML = `
        <div class="slot-title">Решение ${i + 1}
          <label class="lock-control"><input type="checkbox" data-role="lock" data-slot="${i}" ${slot.locked ? "checked" : ""} ${completeSlot(slot) ? "" : "disabled"} aria-label="Закрепить решение ${i + 1}"> Закрепить</label>
        </div>
        <select aria-label="Мера ${i + 1}" data-role="measure" data-slot="${i}" ${slot.locked ? "disabled" : ""}>${buildMeasureOptionsHtml(i)}</select>
        ${
          isCity
            ? `<div class="slot-district-static">Весь город</div>`
            : `<select aria-label="Район ${i + 1}" data-role="district" data-slot="${i}" ${isDistrict && !slot.locked ? "" : "disabled"} ${isDistrict ? "" : 'style="visibility:hidden"'}>${buildDistrictOptionsHtml(i, slot.measureId)}</select>`
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
    container.querySelectorAll('input[data-role="lock"]').forEach((input) => {
      input.addEventListener("change", () => {
        remember();
        state.slots[Number(input.dataset.slot)].locked = input.checked;
        renderSlots();
        scheduleSimulate();
      });
    });

    renderDirectionCounters();
  }

  function onMeasureChange(e) {
    remember();
    const slotIndex = Number(e.target.dataset.slot);
    state.slots[slotIndex] = { measureId: e.target.value, districtId: "" };
    renderSlots();
    scheduleSimulate();
  }

  function onDistrictChange(e) {
    remember();
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
      const tile = document.createElement("button");
      tile.type = "button";
      tile.setAttribute("aria-expanded", String(state.selectedTileId === d.id));
      tile.className = `tile tile-${colorClass}${state.selectedTileId === d.id ? " selected" : ""}`;
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
        container.querySelectorAll("button").forEach((button) => {
          const selected = button.dataset.districtId === state.selectedTileId;
          button.setAttribute("aria-expanded", String(selected));
          button.classList.toggle("selected", selected);
        });
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

    const spent = totalCost(-1);
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
      `${fmt2(scoreBefore)} → ${fmt2(scoreAfter)} <span class="${deltaClass}">(${fmtSigned2(delta)})</span>`;
    if (state.pending || state.previewError || !state.lastSimulate?.result) {
      el("score-display").textContent = state.pending ? "Пересчёт…" : "Завершите выбор";
    }

    const critEl = el("crit-counter");
    critEl.textContent = `Критических: ${state.baseNCrit} → ${result.n_crit}`;
    critEl.classList.toggle("improved", result.n_crit < state.baseNCrit);
  }

  function renderContributionsChart() {
    const result = currentResult();
    if (!result) return;
    const entries = Object.entries(result.contributions || {}).sort((a, b) => b[1] - a[1]);
    const max = Math.max(0.01, ...entries.map(([, value]) => Math.abs(value)));
    el("contrib-chart").innerHTML = entries.map(([mid, value]) => `
      <div class="contribution"><div><span>${esc(state.measuresById[mid]?.name || mid)}</span><strong class="${value < 0 ? "delta-neg" : "delta-pos"}">${fmtSigned2(value)}</strong></div>
      <div class="contribution-track"><span class="${value < 0 ? "negative" : ""}" style="width:${Math.abs(value) / max * 100}%"></span></div></div>
    `).join("") || '<p class="empty-state">Здесь появится вклад выбранных мер.</p>';
    const names = { average: "Средний по городу", weakest: "Слабейший район", critical: "Критические показатели" };
    el("score-breakdown").innerHTML = `<div class="table-scroll"><table class="comparison"><thead><tr><th>Компонент</th><th>До</th><th>После</th><th>Δ</th></tr></thead><tbody>${
      Object.entries(result.score_components || {}).map(([key, value]) => `<tr><td>${names[key]}</td><td>${fmt2(value.before)}</td><td>${fmt2(value.after)}</td><td>${fmtSigned2(value.delta)}</td></tr>`).join("")
    }</tbody></table></div>`;
  }

  function renderCalcButton() {
    const filled = state.slots.length === SLOT_COUNT && state.slots.every(completeSlot);
    const errors = (state.lastSimulate && state.lastSimulate.errors) || [];
    const blocking = errors;
    const busy = Object.values(state.busy).some((revision) => revision === state.revision);
    const ok = filled && blocking.length === 0 && !state.pending && !state.previewError && Boolean(state.lastSimulate?.result) && !busy;
    el("btn-calc").disabled = !ok;
    const lockedCount = state.slots.filter((s) => s.locked).length;
    el("btn-agent").disabled = !ok || lockedCount === SLOT_COUNT;
    el("btn-calc").textContent = state.busy.calc === state.revision ? "Анализируем…" : "Рассчитать и проанализировать";
    el("btn-agent").textContent = state.busy.agent === state.revision ? "Сравниваем стратегии…" : "Сравнить стратегии";
    el("btn-undo").disabled = state.undo.length === 0;
    el("lock-summary").textContent = lockedCount === SLOT_COUNT ? "Все решения закреплены. Снимите хотя бы одно закрепление для поиска." : `Закреплено ${lockedCount} из ${SLOT_COUNT}. Советник сохранит эти меры и районы.`;

    const errBox = el("calc-errors");
    if (!filled) {
      errBox.textContent = "";
    } else if (blocking.length) {
      errBox.textContent = blocking.map((e) => e.message).join("\n");
    } else {
      errBox.textContent = "";
    }
  }

  let simulateTimer = null;
  function scheduleSimulate() {
    clearTimeout(simulateTimer);
    state.simulateController?.abort();
    state.revision += 1;
    state.pending = true;
    state.previewError = false;
    state.lastSimulate = null;
    el("analysis-result").innerHTML = '<p class="empty-state">План изменён. Запустите анализ для текущих решений.</p>';
    clearOptimization();
    el("app-status").textContent = "Проверяем текущий план…";
    el("btn-retry").hidden = true;
    saveDraft();
    renderHeader();
    renderCalcButton();
    el("center-column").setAttribute("aria-busy", "true");
    simulateTimer = setTimeout(runSimulate, 150);
  }

  async function runSimulate() {
    const revision = state.revision;
    const decisions = decisionsOf(state.slots);
    state.simulateController = new AbortController();
    try {
      const body = await api("/api/simulate", "POST", { decisions }, state.simulateController.signal);
      if (revision !== state.revision) return;
      state.lastSimulate = body;
      state.previewError = false;
    } catch (e) {
      if (revision !== state.revision) return;
      state.lastSimulate = null;
      state.previewError = true;
    }
    state.pending = false;
    el("center-column").setAttribute("aria-busy", "false");
    el("app-status").textContent = state.previewError ? "Не удалось проверить план. Расчёт и поиск временно недоступны — повторите запрос." :
      !state.lastSimulate?.result ? "Выберите район для каждой районной меры. Пока показаны исходные показатели." :
      state.lastSimulate.errors.length ? "Предварительный результат: исправьте ограничения, чтобы сохранить сценарий." : "Показатели обновлены. План автоматически проверен.";
    el("btn-retry").hidden = !state.previewError;
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

  function renderAnalysis(analysis, aiMode, aiStatus) {
    const box = el("analysis-result");
    const list = (arr) => (arr || []).map((x) => `<li>${esc(x)}</li>`).join("");
    box.innerHTML = `
      <span class="badge ${aiMode === "fallback" ? "badge-fallback" : ""}">${aiMode === "fallback" ? "Шаблонный анализ · без LLM" : "AI-анализ · расчёт проверен движком"}</span>
      ${aiMode === "fallback" && typeof aiStatus?.message === "string" ? `<p class="muted">${esc(aiStatus.message)}</p>` : ""}
      <section><h3>Итог</h3><p>${esc(analysis.summary)}</p></section>
      <section><h3>Сильные стороны</h3><ul>${list(analysis.strengths)}</ul></section>
      <section><h3>Риски</h3><ul>${list(analysis.risks)}</ul></section>
      <section><h3>Последствия</h3><ul>${list(analysis.consequences)}</ul></section>
      <section><h3>Главный компромисс</h3><p>${esc(analysis.main_tradeoff)}</p></section>
    `;
  }

  async function onCalcClick() {
    if (el("btn-calc").disabled) return;
    const revision = state.revision;
    state.busy.calc = revision;
    renderCalcButton();
    try {
      const decisions = state.slots.map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
      const team = el("team-input").value.trim() || "Команда";
      const body = await api("/api/scenario", "POST", { team, decisions });
      if (revision !== state.revision) return;
      state.lastSimulate = { result: body.result, errors: [] };
      renderHeader();
      renderTiles();
      renderContributionsChart();
      renderSynergyBadges();
      renderAnalysis(body.analysis, body.ai_mode, body.ai_status);
      // The saved analysis is ready; a secondary read must not block the plan.
      void refreshLeaderboard();
    } catch (e) {
      if (revision !== state.revision) return;
      const message = e.name === "TimeoutError" ? "Сервер не ответил вовремя. Сохранение могло завершиться — проверьте таблицу лидеров перед повторной отправкой." : "Не удалось получить ответ. Если запрос уже дошёл до сервера, сценарий мог сохраниться — проверьте таблицу лидеров перед повторной отправкой.";
      el("analysis-result").innerHTML = `<p class="error-text">${message}</p>`;
      void refreshLeaderboard();
    } finally {
      if (state.busy.calc === revision) delete state.busy.calc;
      renderCalcButton();
    }
  }

  function renderAgentResult(body, originalDecisions, revision, epoch = state.optimizationEpoch) {
    state.optimization = { body, originalDecisions, revision, epoch };
    state.activeStrategy = body.objective || "score";
    renderStrategies();
  }

  function selectStrategy(objective) {
    const context = state.optimization;
    if (!context || context.revision !== state.revision || context.epoch !== state.optimizationEpoch) return;
    if (!context.body.strategies?.some((s) => s.objective === objective)) return;
    state.activeStrategy = objective;
    renderStrategies();
    const selectedButton = el("strategy-options").querySelector(`button[data-objective="${objective}"]`);
    selectedButton?.focus({ preventScroll: true });
  }

  function renderStrategies() {
    const context = state.optimization;
    if (!context) return;
    const { body, originalDecisions, revision, epoch } = context;
    const strategies = body.strategies || [{ objective: body.objective || "score", decisions: body.best_decisions, result: body.best_result, source: body.source, explanation: body.explanation }];
    const selected = strategies.find((s) => s.objective === state.activeStrategy) || strategies[0];
    const before = body.original_result;
    el("strategies-panel").hidden = false;
    el("strategy-options").innerHTML = strategies.map((s) => `<button type="button" class="strategy-option" data-objective="${esc(s.objective)}" aria-pressed="${s.objective === selected.objective}">${esc(OBJECTIVES[s.objective].label)}</button>`).join("");
    el("strategy-options").querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => selectStrategy(button.dataset.objective));
    });
    const metrics = [
      { label: "Score", key: "score_after", format: fmt2, higher: true },
      { label: "Слабейший район: балл", key: "d_min", format: fmt2, higher: true },
      { label: "Критических значений", key: "n_crit", format: String, higher: false },
      { label: "Бюджет", key: "total_cost", format: fmt2 },
    ];
    el("strategies-comparison").innerHTML = `<div class="table-scroll"><table class="comparison strategies-table"><caption>Один исходный план и одинаковые закрепления</caption><thead><tr><th scope="col">Показатель</th><th scope="col">Ваш план</th>${strategies.map((s) => `<th scope="col" class="${s.objective === selected.objective ? "selected-strategy" : ""}">${esc(OBJECTIVES[s.objective].short)}</th>`).join("")}</tr></thead><tbody>${metrics.map((m) => `<tr><th scope="row">${m.label}</th><td>${m.format(before[m.key])}</td>${strategies.map((s) => {
      const value = s.result[m.key];
      const changed = value !== before[m.key];
      const better = m.higher ? value > before[m.key] : value < before[m.key];
      const tone = changed && m.higher !== undefined ? (better ? "delta-pos" : "delta-neg") : "";
      return `<td class="${tone} ${s.objective === selected.objective ? "selected-strategy" : ""}">${m.format(value)}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`;
    const groups = new Map();
    for (const strategy of strategies) {
      const key = JSON.stringify(strategy.decisions.map((d) => `${d.measure_id}@${d.district_id || ""}`).sort());
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(OBJECTIVES[strategy.objective].label);
    }
    const identical = [...groups.values()].filter((names) => names.length > 1);
    el("strategy-coincidence").textContent = identical.length ? `Совпали планы: ${identical.map((names) => names.map((n) => `«${n}»`).join(" и ")).join("; ")}. Разные цели могут приводить к одному набору решений.` : "Каждая цель привела к своему набору решений.";
    const requestedLabel = OBJECTIVES[body.objective || "score"].label;
    const fallbackReason = body.ai_mode === "fallback" && typeof body.ai_status?.message === "string" ? ` ${body.ai_status.message}` : "";
    el("strategy-search-note").textContent = `${body.ai_mode === "llm" ? `AI участвовал в поиске для цели «${requestedLabel}».` : "Сравнение рассчитано без LLM."}${fallbackReason} Все три стратегии проверены движком. Поиск локальный: глобальный оптимум не гарантирован. Суммы и баллы округлены.`;
    renderStrategyDetail(body, originalDecisions, revision, epoch, selected);
  }

  function renderStrategyDetail(body, originalDecisions, revision, epoch, strategy) {
    const box = el("agent-result");
    const isRequested = strategy.objective === (body.objective || "score");
    const hypotheses = isRequested ? body.hypotheses || [] : [];
    const hyps = hypotheses
      .map((h) => {
        const ok = h.valid;
        const metrics = [`Score ${fmt2(h.score)}`];
        if (typeof h.d_min === "number") metrics.push(`Мин. район ${fmt2(h.d_min)}`);
        if (typeof h.n_crit === "number") metrics.push(`Критических ${h.n_crit}`);
        return `<li class="hypothesis-item">
          <span>${esc(h.idea)}</span>
          <span class="${ok ? "hypothesis-ok" : "hypothesis-bad"}">${ok ? metrics.join(" · ") + " ✓" : esc(h.error || "отклонено")}</span>
        </li>`;
      })
      .join("");
    const removed = originalDecisions.filter((d) => !strategy.decisions.some((best) => sameDecision(d, best)));
    const added = strategy.decisions.filter((d) => !originalDecisions.some((original) => sameDecision(d, original)));
    const before = body.original_result;
    const after = strategy.result;
    const tradeoffs = [];
    if (after.score_after < before.score_after) tradeoffs.push(`Score ниже вашего плана: ${fmt2(before.score_after)} → ${fmt2(after.score_after)}.`);
    if (after.n_crit > before.n_crit) tradeoffs.push(`Критических значений станет больше: ${before.n_crit} → ${after.n_crit}.`);
    if (after.d_min < before.d_min) tradeoffs.push(`Балл слабейшего района снизится: ${fmt2(before.d_min)} → ${fmt2(after.d_min)}.`);
    const explanation = strategy.explanation || (isRequested ? body.explanation : OBJECTIVES[strategy.objective].description);
    const rows = [
      ["Score", before.score_after, after.score_after],
      ["Бюджет", before.total_cost, after.total_cost],
      ["Критических значений", before.n_crit, after.n_crit],
      ["Слабейший район: балл", before.d_min, after.d_min],
      ...before.districts.map((d) => [d.name, d.d_after, after.districts.find((best) => best.id === d.id).d_after]),
    ];
    box.innerHTML = `
      <h3 class="strategy-detail-title">${esc(OBJECTIVES[strategy.objective].label)}</h3>
      <span class="badge ${strategy.source === "baseline" ? "badge-fallback" : ""}">${strategy.source === "agent" ? "Предложение AI" : "Детерминированный поиск"} · проверено движком</span>
      <p><strong>Score: ${fmt2(before.score_after)} → ${fmt2(after.score_after)}</strong></p>
      ${tradeoffs.length ? `<div class="strategy-tradeoff"><strong>Цена выбранного приоритета</strong><p>${tradeoffs.map(esc).join(" ")}</p></div>` : ""}
      <div class="table-scroll"><table class="comparison"><caption>Ваш план и предложение</caption><thead><tr><th>Показатель</th><th>Ваш план</th><th>Предложение</th></tr></thead><tbody>${rows.map(([name, a, b]) => `<tr><td>${esc(name)}</td><td>${fmt2(a)}</td><td>${fmt2(b)}</td></tr>`).join("")}</tbody></table></div>
      ${removed.length ? `<div class="decision-diff"><h3>Убрать</h3><ul>${removed.map((d) => `<li>${esc(decisionLabel(d))}</li>`).join("")}</ul><h3>Добавить</h3><ul>${added.map((d) => `<li>${esc(decisionLabel(d))}</li>`).join("")}</ul></div>` : '<p>Текущий план сохранён: улучшение не найдено.</p>'}
      <p>${esc(explanation)}</p>
      ${isRequested ? `<details><summary>Проверенные шаги поиска (${hypotheses.length})</summary><ul class="hypothesis-list">${hyps || '<li>Нет шагов с улучшением.</li>'}</ul></details>` : '<p class="muted">Этот вариант найден обычным поиском. Для участия AI в поиске по этой цели выберите её выше и сравните стратегии заново.</p>'}
      <button id="btn-apply" class="btn btn-primary btn-block" ${removed.length ? "" : "disabled"}>Применить эту стратегию</button>
    `;
    el("btn-apply").addEventListener("click", () => {
      if (revision === state.revision && epoch === state.optimizationEpoch) {
        state.objective = strategy.objective;
        renderObjective();
        applyDecisions(strategy.decisions);
      }
    });
  }

  function applyDecisions(decisions) {
    remember();
    const locked = decisionsOf(state.slots.filter((s) => s.locked));
    state.slots = decisions.map((d) => ({ measureId: d.measure_id, districtId: d.district_id || "", locked: locked.some((original) => sameDecision(original, d)) }));
    renderSlots();
    scheduleSimulate();
  }

  async function onAgentClick() {
    if (el("btn-agent").disabled) return;
    const revision = state.revision;
    clearOptimization();
    const epoch = state.optimizationEpoch;
    state.busy.agent = revision;
    renderCalcButton();
    try {
      const decisions = state.slots.map((s) => ({ measure_id: s.measureId, district_id: s.districtId || null }));
      const locked_decisions = decisionsOf(state.slots.filter((s) => s.locked));
      const body = await api("/api/optimize", "POST", { decisions, locked_decisions, objective: state.objective });
      if (revision !== state.revision || epoch !== state.optimizationEpoch) return;
      renderAgentResult(body, decisions, revision, epoch);
    } catch (e) {
      if (revision !== state.revision || epoch !== state.optimizationEpoch) return;
      const message = e.name === "TimeoutError" ? "Сервер не ответил вовремя. План сохранён в браузере — сравнение можно запустить ещё раз." : "Не удалось получить ответ, попробуйте ещё раз.";
      el("agent-result").innerHTML = `<p class="error-text">${message}</p>`;
    } finally {
      if (state.busy.agent === revision && epoch === state.optimizationEpoch) delete state.busy.agent;
      renderCalcButton();
    }
  }

  async function refreshLeaderboard() {
    const request = ++state.leaderboardRequest;
    try {
      const body = await api("/api/leaderboard");
      if (request !== state.leaderboardRequest) return;
      state.leaderboard = body.leaderboard;
      renderLeaderboard();
    } catch (e) {
      // ignore
    }
  }

  const MEDALS = { 1: "🥇", 2: "🥈", 3: "🥉" };

  function renderLeaderboard() {
    const tbody = el("leaderboard-body");
    tbody.innerHTML = "";

    if (!state.leaderboard.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "leaderboard-empty";
      td.textContent = "Пока нет ни одного сохранённого сценария — нажмите «Рассчитать и проанализировать»";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const row of state.leaderboard) {
      const tr = document.createElement("tr");
      const rankLabel = MEDALS[row.rank] ? `${MEDALS[row.rank]} ${row.rank}` : row.rank;
      tr.innerHTML = `<td>${rankLabel}</td><td>${esc(row.team)}</td><td>${fmt2(row.score)}</td><td>${fmt2(row.total_cost)}</td><td>${new Date(row.created_at).toLocaleString("ru-RU")}</td>`;
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
    el("app-status").textContent = "Загружаем город…";
    const stateBody = await api("/api/state");
    state.config = stateBody.config;
    SLOT_COUNT = state.config.decisions_count;
    state.slots = Array.from({ length: SLOT_COUNT }, () => ({ measureId: "", districtId: "", locked: false }));
    state.districts = stateBody.districts;
    state.measures = stateBody.measures;
    for (const d of state.districts) state.districtsById[d.id] = d;
    for (const m of state.measures) state.measuresById[m.id] = m;
    state.baseResult = stateBody.result;
    state.baseNCrit = stateBody.result.n_crit;
    state.lastSimulate = { result: stateBody.result, errors: [] };
    restoreDraft();
    renderObjective();

    renderIndicatorSelect();
    renderSlots();
    renderHeader();
    renderTiles();
    renderContributionsChart();
    renderSynergyBadges();
    renderCalcButton();

    el("btn-calc").onclick = onCalcClick;
    el("btn-agent").onclick = onAgentClick;
    el("objective-select").onchange = (event) => changeObjective(event.target.value);
    el("team-input").oninput = saveDraft;
    el("btn-example").onclick = () => {
      remember();
      state.slots = EXAMPLE.map((s) => ({ ...s, locked: false }));
      renderSlots();
      scheduleSimulate();
    };
    el("btn-reset").onclick = () => {
      remember();
      state.slots = Array.from({ length: SLOT_COUNT }, () => ({ measureId: "", districtId: "", locked: false }));
      renderSlots();
      scheduleSimulate();
    };
    el("btn-undo").onclick = () => {
      if (!state.undo.length) return;
      state.slots = state.undo.pop();
      renderSlots();
      scheduleSimulate();
    };

    state.ready = true;
    document.body.classList.add("app-ready");
    scheduleSimulate();

    await refreshLeaderboard();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const start = () => init().catch(() => {
      el("app-status").textContent = "Не удалось загрузить данные города. Проверьте соединение и повторите загрузку.";
      el("btn-retry").hidden = false;
    });
    el("btn-retry").onclick = () => state.ready ? scheduleSimulate() : start();
    start();
  });
})();
