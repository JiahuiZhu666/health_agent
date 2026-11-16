#!/usr/bin/env node
'use strict';

// Dependency-free UI checks. All figures below are test fixtures, never saved runs.
// Does not start the app, load .env, contact a model, or make network requests.
// Run: node tests/test_web_english.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
const streamlit = fs.readFileSync(path.join(root, 'streamlit_app.py'), 'utf8');
const han = /\p{Script=Han}/u;
let checked = 0;
const test = (name, check) => { check(); checked++; console.log(`PASS ${name}`); };

test('English document, accessibility labels, and unique DOM targets', () => {
  assert.match(html, /<html lang="en">/);
  assert(!han.test(html), 'Static HTML contains untranslated text.');
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size, 'Duplicate HTML IDs.');
  const used = [...source.matchAll(/\$\('([^']+)'\)/g)].map((match) => match[1]);
  for (const id of used) assert(ids.includes(id), `Missing DOM target: ${id}`);
  for (const dialog of ['evidence-dialog', 'process-dialog', 'data-dialog', 'methods-dialog', 'history-dialog']) {
    assert.match(html, new RegExp(`<dialog[^>]*id="${dialog}"`));
  }
});

test('Welcome and composer support general chat, writing, and NHIS research', () => {
  assert(html.includes('What can I help you with?'));
  assert(html.includes('Chat, write, explain'));
  assert.match(html, /placeholder="Message NHIS Lab"/);
  const starters = [...html.matchAll(/data-question="([^"]+)"/g)].map((match) => match[1]);
  assert(starters.some((question) => /write.*email/i.test(question)));
  assert(starters.some((question) => /health by education/i.test(question)));
  assert(starters.some((question) => /health by income/i.test(question)));
  assert(streamlit.includes("PAGES = ['Chat', 'Analysis history', 'Data and methods']"));
  assert(streamlit.includes("st.chat_input('Message NHIS Lab'"));
});

test('No credential/configuration controls or remote UI dependencies', () => {
  assert(!/type="password"|id="(?:api-key|provider|model-name|base-url|settings-form)"/i.test(html));
  assert(!/\/api\/config\b|localStorage\./.test(source));
  assert(!/configure_model\(|test_connection\(/.test(streamlit));
  assert(!/<(?:script|link)[^>]+(?:src|href)="https?:\/\//i.test(html));
  assert(source.includes("sessionStorage.setItem(storageKey, id)"));
});

test('Remaining CJK in application code is confined to legacy input matchers', () => {
  for (const line of source.split('\n').filter((line) => han.test(line))) {
    assert(/\.replace\(\/|\.test\(measurement\.message/.test(line), `Untranslated JavaScript UI: ${line.trim()}`);
  }
  for (const line of streamlit.split('\n').filter((line) => han.test(line))) {
    assert(/re\.(?:sub|search)\(r/.test(line), `Untranslated Streamlit UI: ${line.trim()}`);
  }
});

class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.style = {}; this.attributes = {}; this._text = ''; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
}
const nodes = new Map();
const context = {
  console, URL,
  finite: (value) => typeof value === 'number' && Number.isFinite(value),
  number: (value) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US') : '—',
  pctValue: (value) => value * 100,
  percent: (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1)}%` : '—',
  runPattern: /^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$/,
  text: (node, value) => { node.textContent = value; },
  $: (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
  document: {createElement: (tag) => new Element(tag)},
  fetch: () => { throw new Error('Offline UI checks must not use the network.'); },
};
vm.createContext(context);
function loadBetween(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Production helper not found: ${start}`);
  vm.runInContext(source.slice(from, to), context, {timeout: 1000});
}
loadBetween('  const categoryLabels = ', '  function appendMessage(');
loadBetween('  function humanProcessFacts(', '  function renderProcess(');
loadBetween('  function renderChart(', '  async function refreshStatus(');

test('Known category, cross-group, and contrast labels are English without changing values', () => {
  const row = {group_id: 'less_than_high_school', reference_id: 'bachelors_plus', label: 'saved label', estimate: 0.123};
  const before = JSON.stringify(row);
  assert.equal(context.rowLabel(row), 'Less than high school − Bachelor’s degree or higher');
  assert.equal(context.categoryName('high_school|nh_black'), 'High school or GED × Non-Hispanic Black only');
  assert.equal(context.rowLabel({label: 'Custom English source label'}), 'Custom English source label');
  assert.equal(JSON.stringify(row), before);
  assert.equal(context.processLabel({stage: 'statistics', label: '执行调查统计'}), 'Calculate survey estimates');
  assert.equal(context.processLabel({stage: 'statistics', label: 'Survey computation'}), 'Survey computation');
});

test('Outcome headings use registered event labels and check the source descriptor', () => {
  context.state = {registry: {outcomes: {insured: {id: 'insured', label: 'Insured at interview', variable: 'NOTCOV_A'}}}};
  const result = {spec: {outcome: 'insured'}, survey: {outcome: {id: 'insured', variable: 'NOTCOV_A', label: 'Insured at interview'}}};
  assert.equal(context.resultOutcome(result).label, 'Insured at interview'); assert.equal(context.resultOutcome(result).matches, true);
  result.survey.outcome.variable = 'PHSTAT_A'; assert.equal(context.resultOutcome(result).matches, false);
  result.survey.outcome = {id: 'uninsured', variable: 'NOTCOV_A'}; assert.equal(context.resultOutcome(result).matches, false);
  assert.equal(context.resultOutcome({spec: {}, survey: {}}).label, 'Fair or poor self-rated health');
  assert.match(html, /id="outcome-chart-title"/); assert.match(html, /cost during the past 12 months/);
});

test('Observed process facts retain real denominators and use English units', () => {
  const facts = {full_sample_n: 1234, age_domain_n: 900, filtered_domain_n: 321, valid_outcome_n: 300,
    released_groups: 2, suppressed_groups: 1, planned_calls: 15, valid_calls: 14, cache_hit: false, elapsed_ms: 1250};
  const output = context.humanProcessFacts(facts).join('\n');
  assert(!han.test(output));
  assert(output.includes('Full adult sample: 1,234 people'));
  assert(output.includes('Valid-outcome denominator: 300 people'));
  assert(output.includes('Planned benchmark calls: 15 calls'));
  assert(output.includes('Valid benchmark calls: 14 calls'));
  assert(output.includes('Statistics cache: Miss'));
  assert(output.includes('Observed duration: 1.25 s'));
});

test('CI chart uses English group labels and suppresses unreleased estimates', () => {
  const released = {group_id: 'high_school', label: 'saved label', status: 'ok', estimate: 0.123, ci_low: 0.10, ci_high: 0.15};
  const blocked = {group_id: 'nh_other', status: 'suppressed', estimate: 0.9, ci_low: 0.8, ci_high: 1};
  context.renderChart([released, blocked], [{group_id: 'high_school', model_mean: 0.2}]);
  const chart = nodes.get('ci-chart');
  assert(chart.textContent.includes('High school or GED'));
  assert(chart.textContent.includes('12.3%'));
  assert(!chart.textContent.includes('90.0%'));
  assert(chart.children[0].title.includes('Model mean 20.0%'));
  assert.equal(nodes.get('model-chart-legend').hidden, false);
  context.renderChart([blocked], []);
  assert.equal(chart.children.length, 0, 'Unreleased result produced chart content.');
  assert.equal(nodes.get('model-chart-legend').hidden, true);
});

console.log(`\n${checked} offline English UI checks passed. No environment file or service was accessed.`);
