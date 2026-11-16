#!/usr/bin/env node
'use strict';

// Production frontend functions run against isolated DOM and same-origin API fixtures.
// Never starts a service, loads .env, or contacts a model.
// Run: node tests/test_web_public.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '..', 'web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'web/index.html'), 'utf8');
const start = source.indexOf('(() => {') + '(() => {'.length;
const end = source.indexOf("  $('sidebar-toggle').addEventListener('click'");
const initializeStart = source.indexOf('  async function initializeWorkspace()');
const initializeEnd = source.indexOf('  closeSidebar(); autoGrow();', initializeStart);
const A = 'a'.repeat(32), B = 'b'.repeat(32), run = '20261115T234500Z-0123456789';
class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {setProperty() {}};
    this.attributes = {}; this.value = ''; this.checked = false; this.disabled = false; this.hidden = false;
    this.open = false; this.scrollHeight = 100; this.scrollTop = 0; this._text = '';
    const classes = new Set(); this.classList = {add: (v) => classes.add(v), remove: (v) => classes.delete(v), contains: (v) => classes.has(v)};
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(''); }
  get lastElementChild() { return this.children.at(-1); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  focus() {} addEventListener() {} close() { this.open = false; } showModal() { this.open = true; }
  getBoundingClientRect() { return {height: 100}; } scrollTo({top}) { this.scrollTop = top; }
}
function fixture() {
  const nodes = new Map(), storage = new Map(), calls = [], navigator = {cookieEnabled: true};
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  let respond = async (url) => url === '/api/status' ? publicStatus('public-one') : url === '/api/runs' ? {runs: []} : {conversations: []};
  const document = {getElementById: $, body: new Node(), documentElement: new Node(), activeElement: new Node('button'),
    createElement: (tag) => new Node(tag), createElementNS: (_, tag) => new Node(tag),
    createTextNode: (value) => { const node = new Node('#text'); node.textContent = value; return node; },
    querySelectorAll: (selector) => selector === 'dialog[open]' ? [...nodes.values()].filter((node) => node.open) : []};
  const context = {document, navigator, crypto: webcrypto, window: {matchMedia: () => ({matches: false})}, console, URL, Blob,
    AbortController, setTimeout, clearTimeout, requestAnimationFrame: (fn) => fn(),
    sessionStorage: {setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key), getItem: (key) => storage.get(key)},
    fetch: async (url, options) => { calls.push({url, ...options}); const result = await respond(url, options);
      return {ok: !result.httpStatus || result.httpStatus < 400, status: result.httpStatus || 200,
        headers: {get: () => result.retryHeader ?? null}, json: async () => result.body || result}; },
  };
  vm.createContext(context);
  vm.runInContext(`(() => {${source.slice(start, end)}${source.slice(initializeStart, initializeEnd)}
    globalThis.ui = {api, setupData, refreshStatus, initializeWorkspace, ask, openRun, loadHistory, retryAfterSeconds, rateLimitMessage, publicGuidance,
      status(value) { state = value; applyAccess(value); renderStatus(value); },
      seed() { renderConversation({id:'${A}',title:'Private fixture title',messages:[]}); runs.set('${run}',{run_id:'${run}'}); },
      snapshot() { return {scope:accessScope,key:storageKey,active:activeConversation?.id,chatCount:conversations.size,runCount:runs.size}; }
    }; })();`, context, {timeout: 1000});
  return {ui: context.ui, $, calls, storage, navigator, document, respond: (fn) => { respond = fn; }};
}
const publicStatus = (scope) => ({access: {public: true, scope}, data: {ready: true, income_ready: true}, statistics: {ready: true}, dialogue: {configured: true, connection: {status: 'ok'}}});
const privateStatus = () => ({...publicStatus('private'), access: {public: false, scope: 'private'}});
const drain = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
let checked = 0;
async function test(name, fn) { await fn(); checked++; console.log(`PASS ${name}`); }

(async () => {
  await test('Owner controls fail closed and public mode disables benchmark/data setup', async () => {
    assert.match(html, /id="measure-model" disabled/); assert.match(html, /id="setup-data" disabled/);
    const f = fixture(); f.$('measure-model').checked = true; f.ui.status(publicStatus('public-one'));
    assert.equal(f.$('measure-model').checked, false); assert.equal(f.$('measure-model').disabled, true);
    assert.equal(f.$('setup-data').hidden, true); assert.equal(f.$('public-access-note').hidden, false);
    await f.ui.setupData(); assert.equal(f.calls.length, 0);
    f.ui.status(privateStatus()); assert.equal(f.$('measure-model').disabled, false); assert.equal(f.$('setup-data').hidden, false);
  });

  await test('Status establishes scope before history and excludes the owner storage key', async () => {
    const f = fixture(); f.storage.set('nhis_active_conversation_en_v2', A);
    await f.ui.initializeWorkspace();
    assert.equal(f.calls[0].url, '/api/status');
    assert(f.ui.snapshot().key.endsWith('_public-one'));
    assert(!f.calls.some((call) => call.url === `/api/conversations/${A}`));
    assert(f.calls.every((call) => call.credentials === 'same-origin'));
  });

  await test('Public data guidance delegates preparation to the site operator', async () => {
    const f = fixture(); f.ui.status(publicStatus('public-one'));
    const guidance = f.ui.publicGuidance('The official data have not been verified. Select Download official data or run scripts/setup.command in a terminal.');
    assert(guidance.includes('site operator')); assert(!guidance.includes('setup.command')); assert(!guidance.includes('Select Download'));
    f.ui.status(privateStatus());
    assert(f.ui.publicGuidance('Select Download official data or run scripts/setup.command in a terminal.').includes('setup.command'));
  });

  await test('A changed visitor scope clears cached content, inputs, links, and history', async () => {
    const f = fixture(); f.ui.status(privateStatus()); f.ui.seed();
    f.$('result-answer').textContent = 'Private evidence fixture'; f.$('history-list').textContent = 'Private record fixture';
    f.$('question').value = 'Private draft fixture'; f.$('load-run-id').value = run; f.$('evidence-dialog').open = true;
    f.ui.status(publicStatus('public-two'));
    assert.equal(f.ui.snapshot().active, undefined); assert.equal(f.ui.snapshot().chatCount, 0); assert.equal(f.ui.snapshot().runCount, 0);
    for (const id of ['result-answer', 'history-list', 'conversation']) assert(!f.$(id).textContent.includes('Private'));
    assert.equal(f.$('question').value, ''); assert.equal(f.$('load-run-id').value, ''); assert.equal(f.$('evidence-dialog').open, false);
    assert(!f.document.title.includes('Private'));
  });

  await test('Disabled cookies and server cookie-required errors give actionable feedback', async () => {
    const f = fixture(); f.ui.status(publicStatus('public-one')); f.navigator.cookieEnabled = false;
    await assert.rejects(f.ui.api('/api/conversations', {}), /Enable cookies.*reload/); assert.equal(f.calls.length, 0);
    f.navigator.cookieEnabled = true;
    f.respond(async () => ({httpStatus: 401, body: {error_code: 'cookies_required', error: 'Cookie required'}}));
    await f.ui.initializeWorkspace();
    assert(f.$('system-error').textContent.includes('Cookies are required'));
  });

  await test('Rate limits use actual Retry-After header or response seconds', async () => {
    const f = fixture(); f.respond(async () => ({httpStatus: 429, retryHeader: '90', body: {error: 'Limit'}}));
    await assert.rejects(f.ui.api('/api/conversations', {}), (error) => error.status === 429 && error.retryAfterSeconds === 90 && error.message.includes('2 minutes'));
    f.respond(async () => ({httpStatus: 429, body: {retry_after_seconds: 3600}}));
    await assert.rejects(f.ui.api('/api/conversations', {}), /1 hour/);
    assert.equal(f.ui.retryAfterSeconds('invalid'), null);
    assert(f.ui.retryAfterSeconds(new Date(Date.now() + 60000).toUTCString()) >= 59);
    assert(f.ui.rateLimitMessage(null).includes('Please wait'));
  });

  await test('Public chat request always excludes independent measurement, even if DOM is changed', async () => {
    const f = fixture(); f.ui.status(publicStatus('public-one')); f.ui.seed(); f.$('measure-model').checked = true;
    f.respond(async (url, options) => url === '/api/status' ? publicStatus('public-one') : url.endsWith('/messages')
      ? {conversation: {id: A, title: 'Chat', messages: []}, result: null} : url === '/api/runs' ? {runs: []} : {conversations: []});
    f.$('question').value = 'Hello'; await f.ui.ask({preventDefault() {}}); await drain();
    const sent = f.calls.find((call) => call.url.endsWith('/messages'));
    assert(sent); assert.equal(JSON.parse(sent.body).measure_model, false);
  });

  await test('An older run response cannot reopen evidence after the visitor scope changes', async () => {
    const f = fixture(); f.ui.status(publicStatus('public-one')); let finish;
    f.respond(async () => new Promise((resolve) => { finish = resolve; }));
    const opened = f.ui.openRun(run); f.ui.status(publicStatus('public-two'));
    finish({run_id: run, status: 'blocked', answer: 'Other-session fixture'}); await opened;
    assert.equal(f.$('evidence-dialog').open, false); assert.equal(f.ui.snapshot().runCount, 0);
    assert(!f.$('result-answer').textContent.includes('Other-session'));
  });

  console.log(`\n${checked} public-mode frontend checks passed. No service or environment files accessed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
