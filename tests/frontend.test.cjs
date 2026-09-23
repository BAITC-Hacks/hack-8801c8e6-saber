// No browser dependency: exercise request races and unsafe text at the UI boundary.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness({ respectAbort = false } = {}) {
  const elements = new Map();
  function element() {
    return { innerHTML: '', textContent: '', value: 'Test', children: [], disabled: false,
      handlers: {},
      style: {}, dataset: {}, classList: { add() {}, toggle() {} },
      appendChild(child) { this.children.push(child); },
      addEventListener(name, handler) { this.handlers[name] = handler; }, setAttribute() {}, querySelectorAll() { return []; }, querySelector() { return null; } };
  }
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }, createElement: element, addEventListener() {}, body: element() };
  const requests = [];
  const drafts = new Map();
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({ document, console, AbortController,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    localStorage: { setItem(key, value) { drafts.set(key, value); }, getItem(key) { return drafts.get(key) || null; } },
    fetch(url, options) {
      const request = deferred();
      requests.push({ url, options, ...request });
      if (respectAbort) {
        if (options.signal.aborted) request.reject(new Error('aborted'));
        else options.signal.addEventListener('abort', () => request.reject(new Error('aborted')), {once: true});
      }
      return request.promise;
    },
  });
  let source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
  source = source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { state, runSimulate, scheduleSimulate, renderCalcButton, renderAnalysis, renderLeaderboard, refreshLeaderboard, renderAgentResult, onCalcClick, onAgentClick, applyDecisions, changeObjective, selectStrategy, restoreDraft }; })();`);
  vm.runInContext(source, context);
  const ui = context.ui;
  const config = JSON.parse(fs.readFileSync(path.join(root, 'config.json')));
  const districts = JSON.parse(fs.readFileSync(path.join(root, 'data/districts.json')));
  const measures = JSON.parse(fs.readFileSync(path.join(root, 'data/measures.json')));
  const base = { score_before: 52.56, score_after: 56.54, total_cost: 95, n_crit: 0, critical: [],
    districts: districts.map((d) => ({...d, d_before: 50, d_after: 55, indicators_before: d.indicators, indicators_after: d.indicators})),
    contributions: {}, score_components: {}, synergies_applied: [], d_min: 55 };
  Object.assign(ui.state, { config, districts, measures, baseResult: base,
    districtsById: Object.fromEntries(districts.map((d) => [d.id, d])),
    measuresById: Object.fromEntries(measures.map((m) => [m.id, m])),
    slots: [['M7','nura'], ['M8','nura'], ['M10','nura'], ['M12',''], ['M5','saryarka']].map(([measureId,districtId]) => ({measureId,districtId})),
    lastSimulate: { result: base, errors: [] },
  });
  const reply = (request, body) => request.resolve({ ok: true, json: async () => body });
  function expireTimers(delay) {
    for (const [id, timer] of [...timers]) {
      if (timer.delay === delay) { timers.delete(id); timer.callback(); }
    }
  }
  return { ui, elements, el: document.getElementById, requests, base, reply, drafts, timers, expireTimers };
}

test('out-of-order previews cannot overwrite a newer scenario', async () => {
  const { ui, requests, base, reply } = harness();
  const first = ui.runSimulate();
  ui.scheduleSimulate();
  const second = ui.runSimulate();
  reply(requests[1], {result: {...base, score_after: 57.21}, errors: []});
  await second;
  reply(requests[0], {result: {...base, score_after: 52}, errors: []});
  await first;
  assert.equal(ui.state.lastSimulate.result.score_after, 57.21);
});

test('failed preview and missing district never enable final actions', async () => {
  const { ui, requests, el, base } = harness();
  const run = ui.runSimulate();
  requests[0].reject(new Error('offline'));
  await run;
  assert.equal(el('btn-calc').disabled, true);
  assert.equal(el('btn-agent').disabled, true);
  assert.equal(el('btn-retry').hidden, false);
  ui.state.previewError = false;
  ui.state.lastSimulate = {result: base, errors: []};
  ui.state.slots[0].districtId = '';
  ui.renderCalcButton();
  assert.equal(el('btn-calc').disabled, true);
});

test('editing invalidates an in-flight analysis', async () => {
  const { ui, requests, el, base, reply } = harness();
  ui.renderCalcButton();
  const run = ui.onCalcClick();
  ui.scheduleSimulate();
  const placeholder = el('analysis-result').innerHTML;
  reply(requests[0], {result: base, analysis: {summary: 'OLD'}, ai_mode: 'llm'});
  await run;
  assert.equal(el('analysis-result').innerHTML, placeholder);
  assert.equal(ui.state.lastSimulate, null);
});

test('editing invalidates an in-flight optimization', async () => {
  const { ui, requests, el, reply } = harness();
  ui.renderCalcButton();
  const run = ui.onAgentClick();
  ui.scheduleSimulate();
  reply(requests[0], {explanation: 'OLD'});
  await run;
  assert.equal(el('agent-result').textContent, '');
  assert.equal(el('agent-result').innerHTML, '');
});

test('preview completion cannot enable duplicate analysis requests', async () => {
  const { ui, requests, el, base, reply } = harness();
  ui.state.busy.calc = ui.state.revision;
  const run = ui.runSimulate();
  reply(requests[0], {result: base, errors: []});
  await run;
  assert.equal(el('btn-calc').disabled, true);
  assert.equal(el('btn-agent').disabled, true);
});

test('team names and AI text are rendered as escaped text', () => {
  const { ui, el, base } = harness();
  const payload = '<img src=x onerror=alert(1)>';
  ui.renderAnalysis({ summary: payload, strengths: [payload], risks: [payload], consequences: [payload], main_tradeoff: payload }, 'llm');
  assert.ok(el('analysis-result').innerHTML.includes('&lt;img'));
  assert.ok(!el('analysis-result').innerHTML.includes('<img'));
  ui.state.leaderboard = [{rank: 1, team: payload, score: 1, total_cost: 1, created_at: '2026-09-23', decisions: []}];
  ui.renderLeaderboard();
  assert.ok(el('leaderboard-body').children[0].innerHTML.includes('&lt;img'));
  const decisions = ui.state.slots.map((s) => ({measure_id: s.measureId, district_id: s.districtId || null}));
  ui.renderAgentResult({ original_result: base, best_result: base, original_score: 56.54, best_score: 56.54, delta: 0,
    best_decisions: decisions, hypotheses: [{idea: payload, valid: false, error: payload}], explanation: payload }, decisions, 0);
  assert.ok(!el('agent-result').innerHTML.includes('<img'));
});

test('applying improvement retains locks and an undo snapshot', () => {
  const { ui } = harness();
  ui.state.slots[0].locked = true;
  const original = JSON.stringify(ui.state.slots);
  const decisions = ui.state.slots.map((s) => ({measure_id: s.measureId, district_id: s.districtId || null}));
  decisions[4] = {measure_id: 'M3', district_id: 'nura'};
  ui.applyDecisions(decisions);
  assert.equal(ui.state.slots[0].locked, true);
  assert.equal(ui.state.slots[4].measureId, 'M3');
  assert.equal(JSON.stringify(ui.state.undo[0]), original);
});

function strategyResponse(ui, base) {
  const original = ui.state.slots.map((s) => ({measure_id: s.measureId, district_id: s.districtId || null}));
  const scorePlan = original.map((d, i) => i === 4 ? {measure_id: 'M3', district_id: 'nura'} : d);
  const weakPlan = scorePlan.map((d, i) => i === 0 ? {measure_id: 'M4', district_id: 'nura'} : d);
  return { objective: 'score', original_result: base, hypotheses: [], explanation: 'Test',
    strategies: [
      {objective: 'score', decisions: scorePlan, result: {...base, score_after: 57.21, d_min: 55.1}, source: 'agent'},
      {objective: 'weakest', decisions: weakPlan, result: {...base, score_after: 55.43, d_min: 55.5, n_crit: 2}, source: 'baseline'},
      {objective: 'critical', decisions: scorePlan, result: {...base, score_after: 57.21, d_min: 55.1}, source: 'baseline'},
    ],
  };
}

test('selected objective is sent to server and survives draft restoration', async () => {
  const { ui, requests, drafts, reply } = harness();
  ui.changeObjective('critical');
  assert.equal(JSON.parse(drafts.get('akim-draft-v1')).objective, 'critical');
  ui.state.objective = 'score';
  ui.restoreDraft();
  assert.equal(ui.state.objective, 'critical');
  const run = ui.onAgentClick();
  assert.equal(JSON.parse(requests[0].options.body).objective, 'critical');
  ui.changeObjective('weakest');
  reply(requests[0], {objective:'critical'});
  await run;
  assert.equal(ui.state.optimization, null);
});

test('goal changes invalidate optimization without discarding valid analysis or preview', async () => {
  const { ui, requests, reply, el } = harness();
  const preview = ui.state.lastSimulate;
  el('analysis-result').innerHTML = 'Current valid analysis';
  const run = ui.onAgentClick();
  ui.changeObjective('weakest');
  reply(requests[0], {objective: 'score'});
  await run;
  assert.equal(ui.state.optimization, null);
  assert.equal(ui.state.lastSimulate, preview);
  assert.equal(el('analysis-result').innerHTML, 'Current valid analysis');
  assert.equal(el('strategies-panel').hidden, true);
  assert.equal(el('btn-agent').disabled, false);
});

test('previewing an alternative keeps the original plan, shows costs, and applies that alternative', () => {
  const { ui, base, el, requests } = harness();
  const response = strategyResponse(ui, base);
  const original = JSON.stringify(ui.state.slots);
  const originalDecisions = ui.state.slots.map((s) => ({measure_id:s.measureId,district_id:s.districtId || null}));
  ui.renderAgentResult(response, originalDecisions, 0);
  assert.ok(el('strategy-coincidence').textContent.includes('Совпали планы'));
  ui.selectStrategy('weakest');
  assert.equal(JSON.stringify(ui.state.slots), original);
  assert.equal(requests.length, 0);
  assert.ok(el('agent-result').innerHTML.includes('Score ниже вашего плана'));
  assert.ok(el('agent-result').innerHTML.includes('Критических значений станет больше'));
  assert.ok(el('agent-result').innerHTML.includes('Детерминированный поиск'));
  el('btn-apply').handlers.click();
  assert.equal(ui.state.slots[0].measureId, 'M4');
  assert.equal(ui.state.objective, 'weakest');
  assert.equal(JSON.stringify(ui.state.undo[0]), original);
  assert.equal(el('strategies-panel').hidden, true);
});

test('a stale apply callback cannot apply a strategy from a previous goal', () => {
  const { ui, base, el } = harness();
  const response = strategyResponse(ui, base);
  const original = JSON.stringify(ui.state.slots);
  ui.renderAgentResult(response, ui.state.slots.map((s) => ({measure_id:s.measureId,district_id:s.districtId || null})), 0);
  const staleApply = el('btn-apply').handlers.click;
  ui.changeObjective('critical');
  staleApply();
  assert.equal(JSON.stringify(ui.state.slots), original);
  assert.equal(ui.state.objective, 'critical');
});

test('an old goal request cannot clear the busy state of a new goal request', async () => {
  const { ui, requests, reply, el } = harness();
  const first = ui.onAgentClick();
  ui.changeObjective('weakest');
  const second = ui.onAgentClick();
  reply(requests[0], {objective:'score'});
  await first;
  assert.equal(el('btn-agent').disabled, true);
  assert.equal(ui.state.busy.agent, ui.state.revision);
  requests[1].reject(new Error('offline'));
  await second;
  assert.equal(el('btn-agent').disabled, false);
});

test('a stalled preview times out, clears its timer, and offers recovery', async () => {
  const { ui, requests, el, timers, expireTimers } = harness({respectAbort: true});
  ui.scheduleSimulate();
  const run = ui.runSimulate();
  expireTimers(10000);
  await run;
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(ui.state.pending, false);
  assert.equal(ui.state.previewError, true);
  assert.equal(el('btn-calc').disabled, true);
  assert.equal(el('btn-retry').hidden, false);
  assert.equal([...timers.values()].some((timer) => timer.delay === 10000), false);
});

test('a stalled optimization releases actions without changing the plan or retrying automatically', async () => {
  const { ui, requests, el, timers, expireTimers } = harness({respectAbort: true});
  const original = JSON.stringify(ui.state.slots);
  const run = ui.onAgentClick();
  expireTimers(120000);
  await run;
  assert.equal(JSON.stringify(ui.state.slots), original);
  assert.equal(requests.length, 1);
  assert.equal(el('btn-agent').disabled, false);
  assert.equal(el('btn-calc').disabled, false);
  assert.ok(el('agent-result').innerHTML.includes('не ответил вовремя'));
  assert.equal(timers.size, 0);
});

test('a scenario timeout checks the leaderboard and warns about uncertain saving without resubmitting', async () => {
  const { ui, requests, el, expireTimers } = harness({respectAbort: true});
  const run = ui.onCalcClick();
  expireTimers(120000);
  await run;
  assert.deepEqual(requests.map((request) => request.url), ['/api/scenario', '/api/leaderboard']);
  assert.ok(el('analysis-result').innerHTML.includes('Сохранение могло завершиться'));
  assert.equal(el('btn-calc').disabled, false);
  expireTimers(10000);
});

test('a completed analysis is usable while the secondary leaderboard request is stalled', async () => {
  const { ui, requests, el, base, reply, expireTimers } = harness({respectAbort: true});
  const run = ui.onCalcClick();
  reply(requests[0], {result: base, ai_mode: 'fallback', analysis: {summary: 'Saved analysis'}});
  await run;
  assert.equal(requests[1].url, '/api/leaderboard');
  assert.ok(el('analysis-result').innerHTML.includes('Saved analysis'));
  assert.equal(el('btn-calc').disabled, false);
  assert.equal(el('btn-agent').disabled, false);
  expireTimers(10000);
});

test('an older leaderboard response cannot overwrite a newer refresh', async () => {
  const { ui, requests, reply } = harness();
  const first = ui.refreshLeaderboard();
  const second = ui.refreshLeaderboard();
  const row = {rank: 1, team: 'New result', score: 1, total_cost: 1, created_at: '2026-09-23', decisions: []};
  reply(requests[1], {leaderboard: [row]});
  await second;
  reply(requests[0], {leaderboard: []});
  await first;
  assert.equal(ui.state.leaderboard[0].team, 'New result');
});

test('superseded requests abort and clean up their timeout without changing current status', async () => {
  const { ui, el, timers } = harness({respectAbort: true});
  const run = ui.runSimulate();
  ui.scheduleSimulate();
  await run;
  assert.equal(ui.state.pending, true);
  assert.equal(ui.state.previewError, false);
  assert.ok(el('app-status').textContent.includes('Проверяем'));
  assert.equal([...timers.values()].some((timer) => timer.delay === 10000), false);
});

test('fallback reasons are shown as safe text in analysis and strategy comparison', () => {
  const { ui, el, base } = harness();
  const payload = '<img src=x onerror=alert(1)>';
  ui.renderAnalysis({summary: 'Fallback'}, 'fallback', {code: 'timeout', message: payload});
  assert.ok(el('analysis-result').innerHTML.includes('&lt;img'));
  assert.ok(!el('analysis-result').innerHTML.includes('<img'));
  const response = {...strategyResponse(ui, base), ai_mode: 'fallback', ai_status: {code: 'timeout', message: payload}};
  ui.renderAgentResult(response, ui.state.slots.map((s) => ({measure_id:s.measureId, district_id:s.districtId || null})), 0);
  assert.ok(el('strategy-search-note').textContent.includes(payload));
});
