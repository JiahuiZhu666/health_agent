#!/usr/bin/env node
'use strict';

// Isolated fixtures test the actual inline chart renderer and explicit downloads.
// Fixtures are never published as research results; no server or model is called.
// Run: node tests/test_web_charts.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web/app.js'), 'utf8');
const run = '20261115T234500Z-0123456789';
const A = 'a'.repeat(32);
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const find = (node, tag) => descendants(node).filter((item) => item.tagName === tag);
const marked = (node, mark) => descendants(node).filter((item) => item.attributes['data-mark'] === mark);
const escape = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const serialize = (node) => `<${node.tagName}${Object.entries(node.attributes).map(([key, value]) => ` ${key}="${escape(value)}"`).join('')}>${escape(node._text)}${node.children.map(serialize).join('')}</${node.tagName}>`;
function fixture() {
  const nodes = new Map(), downloads = [], blobs = new Map(), timers = [], revoked = [], sources = [], draws = [];
  let urlSequence = 0;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {}; this.style = {}; this._text = ''; this.hidden = false; this.disabled = false; this.value = ''; this.scrollTop = 0; }
    append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } }
    replaceChildren(...items) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ''; this.append(...items); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get textContent() { return this._text + this.children.map((item) => item.textContent).join(''); }
    get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
    setAttribute(key, value) { assert(!/^on/i.test(key)); this.attributes[key] = String(value); }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    async trigger(type) { return await this.listeners[type]?.({preventDefault() {}}); }
    click() { if (this.tagName === 'a') downloads.push({name: this.download, url: this.href, blob: blobs.get(this.href)}); }
    remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; } }
    focus() {}
    getContext() { return {fillRect() {}, drawImage: (...values) => draws.push(values)}; }
    toBlob(fn, type) { fn(new Blob(['isolated PNG fixture'], {type})); }
  }
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); };
  const allowedSvg = new Set(['svg', 'title', 'desc', 'rect', 'line', 'text', 'g', 'circle']);
  const document = {body: new Element('body'), createElement: (tag) => new Element(tag),
    createElementNS: (namespace, tag) => { assert.equal(namespace, 'http://www.w3.org/2000/svg'); assert(allowedSvg.has(tag), `Unsafe SVG tag ${tag}`); return new Element(tag); }};
  document.body.append($('conversation'));
  const context = {document, $, Blob, console, asArray: (value) => Array.isArray(value) ? value : [], finite: (value) => typeof value === 'number' && Number.isFinite(value),
    runPattern: /^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$/, conversationPattern: /^[a-f0-9]{32}$/, accessScope: 'public-fixture',
    chartDownloadUrls: new Set(), text: (node, value) => { node.textContent = value; },
    setTimeout: (fn, duration) => { timers.push({fn, duration}); return timers.length; }, clearTimeout() {},
    URL: {createObjectURL: (blob) => { const url = `blob:isolated-${++urlSequence}`; blobs.set(url, blob); return url; }, revokeObjectURL: (url) => { revoked.push(url); blobs.delete(url); }},
    XMLSerializer: class {serializeToString(node) { return serialize(node); }},
    createImageBitmap: async (blob) => { assert.equal(blob.type, 'image/svg+xml;charset=utf-8'); return {close() { draws.push('closed'); }}; },
    openRun: (id) => { sources.push(id); }, fetch: () => { throw new Error('Charts must never fetch external resources.'); },
    displayMessage: String, richText: (node, value) => { node.textContent = value; }, processEvents: () => [], copyReply() {},
    scrollBottom() {}, renderConversationHistory() {}, requestAnimationFrame: (fn) => fn(),
    conversations: new Map(), deletedConversationIds: new Set(), activeConversation: null, autoFollow: true, saveActive() {},
  };
  vm.createContext(context);
  for (const [from, until] of [['  function generationLabel(', '  function renderServiceStatus('], ['  function releasedChart(', '  function setBusy(']]) {
    const start = source.indexOf(from), end = source.indexOf(until, start); assert(start >= 0 && end > start);
    vm.runInContext(source.slice(start, end), context, {timeout: 1000});
  }
  const article = document.createElement('article'); document.body.append(article);
  return {context, document, article, $, downloads, blobs, timers, revoked, sources, draws, drainTimers: () => { for (const timer of timers.splice(0)) timer.fn(); }};
}
const panel = (extra = {}) => ({id: 'education', title: 'Education and self-rated health', subtitle: 'Adults aged 25–64 · Official survey fixture', kind: 'proportion', style: 'point',
  unit: 'percent', reference: 0, source_run_id: run, groups: [{group_id: 'high_school', label: 'High school or GED', value: 12.3, low: 10.1, high: 14.6, n: 321}], ...extra});
let checked = 0;
async function test(name, fn) { await fn(); checked++; console.log(`PASS ${name}`); }

(async () => {
  await test('Several panels retain their own populations, source IDs, and already-scaled values', async () => {
    const f = fixture(); const income = panel({id: 'income', title: 'Income groups', groups: [{label: 'Below 100% of the poverty threshold', value: 20.4, low: 18.2, high: 22.9, n: 456}]});
    const women = panel({id: 'women-income', title: 'Income groups among women', source_run_id: '20261115T234501Z-1123456789', groups: [{label: 'Women: below 100%', value: 23.7, low: 20.2, high: 27.5, n: 234}]});
    f.context.appendCharts(f.article, [panel(), income, women]);
    const figures = find(f.article, 'figure'); assert.equal(figures.length, 3);
    assert.equal(find(figures[0], 'g')[0].attributes['data-value'], '12.3'); assert.equal(find(figures[1], 'g')[0].attributes['data-value'], '20.4'); assert.equal(find(figures[2], 'g')[0].attributes['data-value'], '23.7');
    assert(figures[2].textContent.includes('among women')); assert(figures[0].textContent.includes('95% confidence intervals'));
    await find(figures[2], 'button').find((button) => button.textContent === 'Source ↗').trigger('click'); assert.equal(f.sources[0], women.source_run_id);
    const many = fixture(); many.context.appendCharts(many.article, Array.from({length: 13}, (_, index) => panel({id: `panel-${index}`})));
    assert.equal(find(many.article, 'figure').length, 13, 'An all-charts response must not be silently truncated.');
  });

  await test('Horizontal bars preserve confidence intervals and zero baselines', async () => {
    const f = fixture(); f.context.appendCharts(f.article, [panel({style: 'bar'})]);
    assert.equal(marked(f.article, 'bar').length, 1); assert.equal(marked(f.article, 'ci').length, 1); assert.equal(marked(f.article, 'estimate').length, 1);
    assert.equal(marked(f.article, 'bar')[0].attributes.x, '238'); assert(find(f.article, 'line').some((line) => line.attributes['data-reference'] === '0'));
  });

  await test('Care-access chart labels and axes do not revert to self-rated health', async () => {
    const f = fixture(); f.context.appendCharts(f.article, [panel({title: 'Insured at interview', subtitle: 'Adults aged 25–64; coverage at interview does not establish timely care.', x_label: 'Insured at interview (%)', outcome_id: 'insured', outcome: {id: 'insured', label: 'Insured at interview'}})]);
    assert(f.article.textContent.includes('Insured at interview (%)'));
    assert(!f.article.textContent.includes('Fair or poor'));
    assert.equal(find(f.article, 'g')[0].attributes['data-value'], '12.3');
  });

  await test('Gradient pp and odds-ratio forest keep native units and scientific references', async () => {
    const f = fixture(); f.context.appendCharts(f.article, [panel({kind: 'gradient', unit: 'percentage_points', groups: [{label: 'A minus reference', value: -3.4, low: -5.6, high: -1.2}]}),
      panel({kind: 'regression', unit: 'odds_ratio', style: 'bar', reference: 1, groups: [{label: 'Education term', value: 2, low: 1.1, high: 3.2}]})]);
    const figures = find(f.article, 'figure'); assert.equal(find(figures[0], 'g')[0].attributes['data-value'], '-3.4'); assert(figures[0].textContent.includes('-3.40 (-5.60–-1.20)'));
    assert(find(figures[0], 'line').some((line) => line.attributes['data-reference'] === '0'));
    assert(find(figures[1], 'line').some((line) => line.attributes['data-reference'] === '1')); assert(figures[1].textContent.includes('log scale')); assert.equal(marked(figures[1], 'bar').length, 0);
    assert(figures[1].textContent.includes('2.000 (1.100–3.200)'));
    const wide = f.context.releasedChart(panel({kind: 'gradient', unit: 'percentage_points', groups: [{label: 'Wide recorded interval', value: 60, low: -20, high: 125}]}));
    assert.equal(wide.groups[0].high, 125, 'A recorded linearized difference CI must not be clipped at 100pp.');
  });

  await test('Invalid, unreleased, sparse, or untrusted values never become marks', async () => {
    const f = fixture(); const invalid = [
      {label: 'Sparse fixture', value: 88, low: 80, high: 90, n: 99}, {label: 'Suppressed fixture', value: 77, low: 70, high: 80, status: 'suppressed'},
      {label: 'NaN fixture', value: NaN, low: 2, high: 4}, {label: 'CI ordering fixture', value: 9, low: 10, high: 12}, {label: 'Out of range', value: 101, low: 99, high: 102},
    ];
    f.context.appendCharts(f.article, [panel({groups: [...invalid, panel().groups[0]]}), panel({source_run_id: '../../other'}), panel({unit: 'proportion'}), panel({status: 'blocked'})]);
    assert.equal(find(f.article, 'svg').length, 1); assert.equal(find(f.article, 'g').length, 1);
    for (const row of invalid) assert(!f.article.textContent.includes(row.label));
    assert.equal(f.context.releasedChart(panel({groups: invalid})), null);
    const malicious = panel({title: '<script>alert(1)</script>', groups: [{label: '<img onerror=evil()>', value: 5, low: 4, high: 6}]});
    f.context.appendCharts(f.article, [malicious]); assert.equal(find(f.article, 'script').length, 0); assert.equal(find(f.article, 'img').length, 0);
  });

  await test('SVG download uses a stable safe filename and revokes its only object URL', async () => {
    const f = fixture(); f.context.appendCharts(f.article, [panel({id: '../../malicious-id'})]);
    await find(f.article, 'button').find((button) => button.textContent === 'Download SVG').trigger('click');
    assert.equal(f.downloads.length, 1); assert.equal(f.downloads[0].name, `nhis-${run}-proportion-point.svg`);
    const svg = await f.downloads[0].blob.text(); assert(svg.includes('xmlns="http://www.w3.org/2000/svg"')); assert(svg.includes('fill="#ffffff"')); assert(svg.includes('data-value="12.3"'));
    assert(!/<(?:image|foreignObject|script)\b|href=|src=/.test(svg));
    assert.equal(f.blobs.size, 1); f.drainTimers(); assert.equal(f.blobs.size, 0); assert.equal(f.context.chartDownloadUrls.size, 0);
  });

  await test('PNG decodes only generated SVG, draws a local canvas, and cleans up', async () => {
    const f = fixture(); f.context.appendCharts(f.article, [panel()]);
    await find(f.article, 'button').find((button) => button.textContent === 'Download PNG').trigger('click');
    assert.equal(f.downloads[0].blob.type, 'image/png'); assert.equal(f.downloads[0].name, `nhis-${run}-proportion-point.png`);
    assert.equal(f.draws.filter((value) => Array.isArray(value)).length, 1); assert(f.draws.includes('closed'));
    f.drainTimers(); assert.equal(f.blobs.size, 0);
  });

  await test('PNG data-image fallback stays local and conversion failure leaves SVG available', async () => {
    const f = fixture(); f.context.createImageBitmap = undefined;
    f.context.Image = class {set src(value) { assert(value.startsWith('data:image/svg+xml;charset=utf-8,')); this.onload(); }};
    f.context.appendCharts(f.article, [panel()]);
    await find(f.article, 'button').find((button) => button.textContent === 'Download PNG').trigger('click');
    assert.equal(f.downloads[0].blob.type, 'image/png'); f.drainTimers(); assert.equal(f.blobs.size, 0);
    const failed = fixture(), createElement = failed.document.createElement;
    failed.document.createElement = (tag) => { const node = createElement(tag); if (tag === 'canvas') node.getContext = () => null; return node; };
    failed.context.appendCharts(failed.article, [panel()]);
    const pngButton = find(failed.article, 'button').find((button) => button.textContent === 'Download PNG');
    await pngButton.trigger('click'); assert.equal(failed.downloads.length, 0); assert.equal(failed.blobs.size, 0); assert.equal(pngButton.disabled, false);
    assert(failed.article.textContent.includes('Download SVG instead'));
  });

  await test('Restored assistant messages render charts inline without duplicate IDs', async () => {
    const f = fixture(); f.context.renderConversation({id: A, title: 'Saved charts', messages: [{id: 'm1', role: 'assistant', kind: 'visualization', generation: {status: 'local', type: 'visualization'}, content: 'Here are your saved charts.', charts: [panel(), panel({title: 'Second saved panel'})]}]}, true);
    assert.equal(find(f.$('conversation'), 'figure').length, 2); assert.equal(find(f.$('conversation'), 'svg').length, 2); assert(f.$('conversation').textContent.includes('Data visualization'));
    assert(!descendants(f.$('conversation')).some((node) => Object.hasOwn(node.attributes, 'id')));
  });

  await test('A visitor scope change cancels an in-flight PNG before a download URL is made', async () => {
    const f = fixture(); let finish;
    f.context.createImageBitmap = () => new Promise((resolve) => { finish = resolve; });
    f.context.appendCharts(f.article, [panel()]); const download = find(f.article, 'button').find((button) => button.textContent === 'Download PNG').trigger('click');
    f.context.accessScope = 'public-other-fixture'; finish({close() {}}); await download;
    assert.equal(f.downloads.length, 0); assert.equal(f.blobs.size, 0);
  });

  console.log(`\n${checked} offline inline-chart checks passed. No real records or services were accessed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
