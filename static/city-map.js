/* Real boundaries are presentation data. All scores come from the simulator. */
(() => {
  "use strict";
  const COLORS = Object.freeze({ red: "#e7685b", amber: "#e9b64e", green: "#49aa88", gray: "#a3b1b7", up: "#28a596", down: "#dc6878", flat: "#acbac3" });
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const fmt = (v) => finite(v) ? v.toFixed(2) : "—";
  const deltaText = (v) => `${v > 0 ? "+" : ""}${fmt(v)}`;
  const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function values(district, indicator = "D") {
    const before = indicator === "D" ? district?.d_before : district?.indicators_before?.[indicator];
    const after = indicator === "D" ? district?.d_after : district?.indicators_after?.[indicator];
    return finite(before) && finite(after) ? { before, after, delta: Math.round((after - before) * 100) / 100 } : null;
  }

  function appearance(district, indicator = "D", mode = "after") {
    const pair = values(district, indicator);
    if (!pair) return { color: COLORS.gray, label: "Нет данных модели", value: null };
    const value = mode === "delta" ? pair.delta : pair[mode === "before" ? "before" : "after"];
    if (mode === "delta") return {
      value, color: value > 0 ? COLORS.up : value < 0 ? COLORS.down : COLORS.flat,
      label: value > 0 ? "Улучшение" : value < 0 ? "Снижение" : "Без изменений",
    };
    const low = indicator === "D" ? 50 : 40;
    return {
      value, color: value < low ? COLORS.red : value <= 60 ? COLORS.amber : COLORS.green,
      label: value < low ? "Требует внимания" : value <= 60 ? "Есть точки роста" : "Хороший уровень",
    };
  }

  function criticalIndicators(district, mode = "after", threshold = 40) {
    const source = mode === "before" ? district?.indicators_before : district?.indicators_after;
    return Object.entries(source || {}).filter(([, v]) => finite(v) && v < threshold).map(([code]) => code);
  }

  function directionValues(district, config, mode = "after") {
    const source = mode === "before" ? district?.indicators_before : district?.indicators_after;
    return Object.entries(config?.directions || {}).map(([id, name]) => {
      const numbers = (config.indicators || []).filter((i) => i.direction === id).map((i) => source?.[i.code]).filter(finite);
      return { id, name, value: numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : null };
    });
  }

  // Export only pure presentation functions for dependency-free Node tests.
  if (typeof module !== "undefined" && module.exports) module.exports = { COLORS, values, appearance, criticalIndicators, directionValues };
  if (typeof document === "undefined") return;

  const el = (id) => document.getElementById(id);
  const reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  let map, boundaries, latest, config, onSelect, loading = false;
  let mode = "after", selectedId = null, playTimer = null, playing = false;
  let features = [], shapes = new Map(), markers = new Map(), previousLabels = new Map();
  let previousCard = "", lastSignature = "";

  function district(id) { return latest?.result?.districts?.find((d) => d.id === id); }

  function statusText() {
    if (latest?.pending) return "Пересчитываем план · показаны исходные данные";
    if (latest?.error) return "Расчёт недоступен · показаны исходные данные";
    if (!latest?.previewValid) return "Показаны исходные данные";
    if (latest?.hasErrors) return "Предварительный расчёт · план требует исправлений";
    return "Показатели текущего плана · шкала 0–100";
  }

  function stopPlayback() {
    clearTimeout(playTimer);
    playTimer = null;
    playing = false;
    if (el("map-play")) {
      el("map-play").innerHTML = '<span aria-hidden="true">▶</span> Сравнить';
      el("map-play").setAttribute("aria-label", "Анимированное сравнение до и после");
    }
  }

  function setMode(next, fromPlayback = false) {
    if (!["before", "after", "delta"].includes(next)) return;
    if (!fromPlayback) stopPlayback();
    mode = next;
    render();
  }

  function focusDistrict(id) {
    selectedId = id;
    onSelect?.(id);
    render();
    const shape = shapes.get(id);
    if (shape) map.flyToBounds(shape.getBounds(), {
      paddingTopLeft: [45, 75], paddingBottomRight: [45, 45],
      maxZoom: 12, duration: .85, animate: !reducedMotion(),
    });
  }

  function resetView(animate = true) {
    // Fit all district labels, keeping the built-up city readable. Long airport
    // and industrial outskirts remain accessible by panning or zooming out.
    const overview = features.map((f) => [f.properties.label[1], f.properties.label[0]]);
    if (map && overview.length) map.fitBounds(overview, {
      paddingTopLeft: [65, 98], paddingBottomRight: [65, 65], animate: animate && !reducedMotion(), duration: .7,
    });
  }

  function renderLegend() {
    const low = latest?.indicator === "D" ? 50 : 40;
    const entries = mode === "delta"
      ? [[COLORS.down, "Снижение"], [COLORS.flat, "Без изменений"], [COLORS.up, "Улучшение"], [COLORS.gray, "Нет данных"]]
      : [[COLORS.red, `< ${low} · внимание`], [COLORS.amber, `${low}–60 · точки роста`], [COLORS.green, "> 60 · хорошо"], [COLORS.gray, "Нет данных"]];
    el("map-legend").innerHTML = entries.map(([color, label]) => `<span><i style="background:${color}" aria-hidden="true"></i>${escape(label)}</span>`).join("");
    el("map-preview-status").textContent = statusText();
    el("map-phase").textContent = ({ before: "ДО РЕШЕНИЙ", after: "ПОСЛЕ РЕШЕНИЙ", delta: "ЭФФЕКТ РЕШЕНИЙ" })[mode];
    if ((latest?.pending || latest?.error || !latest?.previewValid) && mode !== "before") el("map-phase").textContent = "ИСХОДНЫЕ ДАННЫЕ";
    document.querySelectorAll("[data-map-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mapMode === mode)));
    el("map-play").disabled = !!latest?.pending || !!latest?.error || !latest?.previewValid;
  }

  function renderCard() {
    const scoreKey = mode === "before" ? "d_before" : "d_after";
    const weakest = [...(latest?.result?.districts || [])].filter((d) => finite(d[scoreKey])).sort((a, b) => a[scoreKey] - b[scoreKey])[0];
    const id = selectedId || weakest?.id;
    const dd = district(id);
    const feature = features.find((f) => f.properties.id === id);
    el("map-focus-label").textContent = selectedId ? "ВЫБРАННЫЙ РАЙОН" : "ПРИОРИТЕТ ДЛЯ ГОРОДА";
    if (!id) return;
    let html;
    if (!dd) {
      html = `<h3>${escape(feature?.properties.name || "Район")}</h3><span class="district-status"><i class="district-dot" style="--district-color:${COLORS.gray}" aria-hidden="true"></i>Нет данных модели</span><p>Район показан на реальной карте. В учебном наборе для него пока нет показателей, поэтому он не участвует в расчёте Score.</p><div class="district-score-row"><strong>—</strong><small>/ 100</small></div><p>Выберите один из пяти районов с данными, чтобы изучить эффект решений.</p>`;
    } else {
      const indicator = latest.indicator || "D";
      const info = appearance(dd, indicator, mode);
      const pair = values(dd, indicator);
      const critical = criticalIndicators(dd, mode, config?.crit_threshold ?? 40);
      const indicatorName = indicator === "D" ? "Балл района D" : config?.indicators.find((i) => i.code === indicator)?.name || indicator;
      const profile = latest.districts?.find((d) => d.id === id);
      const bars = directionValues(dd, config, mode).map(({ name, value }) => {
        const color = value < 40 ? "#f59785" : value <= 60 ? "#e8c778" : "#80d4b7";
        return `<div class="district-direction"><span>${escape(name)}</span><div class="district-direction-track"><span style="width:${finite(value) ? Math.min(100, Math.max(0, value)) : 0}%;background:${color}"></span></div><b>${finite(value) ? value.toFixed(0) : "—"}</b></div>`;
      }).join("");
      const baseline = latest.pending || latest.error || !latest.previewValid;
      const caption = baseline ? `${escape(indicatorName)} · исходные данные` : mode === "before" ? `${escape(indicatorName)} · до решений` : mode === "delta" ? `${escape(indicatorName)} · изменение в пунктах` : `${escape(indicatorName)} · после решений`;
      const captionBefore = pair && mode !== "before" ? ` · было ${fmt(pair.before)}` : "";
      const criticalNames = critical.map((code) => config?.indicators.find((i) => i.code === code)?.name || code);
      html = `<h3>${escape(dd.name)}</h3><span class="district-status"><i class="district-dot" style="--district-color:${info.color}" aria-hidden="true"></i>${escape(info.label)}</span>
        <div class="district-score-row"><strong>${mode === "delta" && pair ? deltaText(pair.delta) : fmt(info.value)}</strong>${mode !== "delta" ? '<small>/ 100</small>' : '<small>пунктов</small>'}${pair && mode === "after" ? `<span class="district-score-delta${pair.delta < 0 ? " is-negative" : ""}">${deltaText(pair.delta)}</span>` : ""}</div>
        <p class="score-caption">${caption}${captionBefore}</p>
        <div class="district-directions" aria-label="Средние значения показателей по направлениям${mode === "before" ? " до" : " после"} решений">${bars}</div>
        <div class="district-critical-note">${critical.length ? `<strong>Критических показателей: ${critical.length}.</strong> ${escape(criticalNames.join(", "))}.` : "Показателей ниже критического порога нет."}</div>
        <p>${escape(profile?.profile || "")}</p>`;
    }
    // Preserve unchanged DOM and animate actual bar/value changes only.
    if (html !== previousCard) {
      const oldWidths = [...el("map-district-card").querySelectorAll(".district-direction-track span")].map((bar) => bar.style.width);
      el("map-district-card").innerHTML = html;
      if (!reducedMotion()) el("map-district-card").querySelectorAll(".district-direction-track span").forEach((bar, i) => {
        const target = bar.style.width;
        bar.style.width = oldWidths[i] || "0%";
        requestAnimationFrame(() => requestAnimationFrame(() => { if (bar.isConnected) bar.style.width = target; }));
      });
      previousCard = html;
    }
  }

  function render() {
    if (!latest || !el("map-legend")) return;
    renderLegend();
    renderCard();
    for (const feature of features) {
      const { id, name } = feature.properties;
      const dd = district(id);
      const info = appearance(dd, latest.indicator, mode);
      const selected = id === selectedId;
      const shape = shapes.get(id);
      shape?.setStyle({ fillColor: info.color, color: selected ? "#163e49" : "#ffffff", weight: selected ? 3 : 1.7, fillOpacity: selected ? .55 : .39, dashArray: dd ? null : "5 5" });
      if (selected) shape?.bringToFront();
      const value = info.value === null ? "Нет данных" : mode === "delta" ? deltaText(info.value) : fmt(info.value);
      const critical = criticalIndicators(dd, mode, config?.crit_threshold ?? 40).length;
      const label = `<div class="district-label-inner${selected ? " is-selected" : ""}${critical ? " has-critical" : ""}${mode !== "before" && values(dd, latest.indicator)?.delta ? " has-change" : ""}"><span class="district-label-name">${escape(name)}</span><span class="district-label-value"><i class="district-dot" style="--district-color:${info.color}" aria-hidden="true"></i>${value}${critical ? ` · ! ${critical}` : ""}</span></div>`;
      if (previousLabels.get(id) !== label) {
        markers.get(id)?.setIcon(globalThis.L.divIcon({ className: "district-map-label", html: label, iconSize: [0, 0], iconAnchor: [0, 0] }));
        previousLabels.set(id, label);
      }
      const markerElement = markers.get(id)?.getElement();
      markerElement?.setAttribute("aria-label", `${name}: ${info.label}, ${value}. Выбрать район`);
      markerElement?.setAttribute("aria-pressed", String(selected));
      const button = el("map-district-nav").querySelector(`[data-district="${id}"]`);
      if (button) {
        button.setAttribute("aria-pressed", String(selected));
        button.querySelector("i").style.background = info.color;
      }
    }
  }

  async function init(options = {}) {
    onSelect = options.onSelect || onSelect;
    if (loading || map || !el("city-map")) return;
    loading = true;
    try {
      if (!globalThis.L) throw new Error("Map library unavailable");
      const response = await fetch("data/astana-districts.geojson", { signal: AbortSignal.timeout(12000) });
      if (!response.ok) throw new Error("Boundary snapshot unavailable");
      const geojson = await response.json();
      if (geojson.type !== "FeatureCollection" || !geojson.features?.length) throw new Error("Invalid boundaries");
      features = geojson.features;
      const L = globalThis.L;
      map = L.map("city-map", { zoomControl: false, scrollWheelZoom: false, zoomSnap: .25, zoomDelta: .5, minZoom: 9, maxZoom: 17, preferCanvas: false });
      map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener noreferrer">Leaflet</a>');
      map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>');
      L.control.zoom({ position: "bottomright", zoomInTitle: "Приблизить", zoomOutTitle: "Отдалить" }).addTo(map);
      L.control.scale({ position: "bottomleft", imperial: false, maxWidth: 85 }).addTo(map);
      // Normal on-demand browser tiles. No tile prefetch, service worker or proxy.
      const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, updateWhenIdle: true, keepBuffer: 1 }).addTo(map);
      let tileFailures = 0;
      tiles.on("loading", () => { tileFailures = 0; });
      tiles.on("tileerror", () => { tileFailures += 1; el("map-network").hidden = false; });
      tiles.on("load", () => { el("map-network").hidden = tileFailures === 0; });
      boundaries = L.geoJSON(geojson, {
        style: { className: "district-shape", color: "#fff", weight: 1.7, fillColor: COLORS.gray, fillOpacity: .39 },
        onEachFeature(feature, layer) {
          const { id, name, label } = feature.properties;
          shapes.set(id, layer);
          layer.on("click", () => focusDistrict(id));
          layer.on("mouseover", () => layer.setStyle({ weight: 3, fillOpacity: .57 }));
          layer.on("mouseout", () => render());
          const marker = L.marker([label[1], label[0]], { keyboard: true, title: name, riseOnHover: true, icon: L.divIcon({ className: "district-map-label", html: "", iconSize: [0, 0] }) }).addTo(map);
          marker.on("click", () => focusDistrict(id));
          markers.set(id, marker);
          const button = document.createElement("button");
          button.type = "button";
          button.dataset.district = id;
          button.innerHTML = `<i class="district-dot" aria-hidden="true"></i>${escape(name)}`;
          button.addEventListener("click", () => focusDistrict(id));
          el("map-district-nav").appendChild(button);
        },
      }).addTo(map);
      map.setMaxBounds(boundaries.getBounds().pad(.7));
      resetView(false);
      // Resizing must preserve the user's camera; it must not reset district focus.
      new ResizeObserver(() => map.invalidateSize({ pan: false })).observe(el("city-map"));
      document.querySelectorAll("[data-map-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mapMode)));
      el("map-home").onclick = () => { selectedId = null; onSelect?.(null); resetView(); render(); };
      el("map-play").onclick = () => {
        if (playing) { stopPlayback(); setMode("after"); return; }
        playing = true;
        setMode("before", true);
        el("map-play").innerHTML = '<span aria-hidden="true">■</span> Остановить';
        el("map-play").setAttribute("aria-label", "Остановить сравнение");
        playTimer = setTimeout(() => { stopPlayback(); setMode("after", true); }, reducedMotion() ? 800 : 1700);
      };
      el("map-loading").hidden = true;
      render();
    } catch (_error) {
      // A map outage never blocks budgeting, simulation or AI analysis.
      el("map-loading").textContent = "Карта временно недоступна. Показатели и решения работают ниже.";
      const retry = document.createElement("button");
      retry.className = "map-action";
      retry.type = "button";
      retry.textContent = "Повторить загрузку карты";
      retry.onclick = () => {
        if (map) { map.remove(); map = null; }
        shapes.clear(); markers.clear(); previousLabels.clear(); features = [];
        el("map-district-nav").innerHTML = "";
        el("map-loading").textContent = "Загружаем границы Астаны…";
        init(options);
      };
      el("map-loading").appendChild(retry);
    } finally {
      loading = false;
    }
  }

  function update(snapshot) {
    const signature = JSON.stringify([snapshot.result, snapshot.indicator, snapshot.pending, snapshot.error]);
    // A new scenario invalidates a running comparison, just like analysis.
    if (lastSignature && signature !== lastSignature && playing) { stopPlayback(); mode = "after"; }
    lastSignature = signature;
    latest = snapshot;
    config = snapshot.config;
    selectedId = snapshot.selectedId || null;
    render();
  }

  globalThis.AstanaMap = Object.freeze({ init, update });
})();
