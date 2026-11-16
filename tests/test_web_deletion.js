#!/usr/bin/env node
'use strict';

// Isolated DOM/API fixtures exercise production UI logic. Never deletes real files/chats.
// Run: node tests/test_web_deletion.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const start = source.indexOf('(() => {') + '(() => {'.length;
const end = source.indexOf("  $('sidebar-toggle').addEventListener('click'");
assert(start > 0 && end > start, 'Production UI function boundary not found.');
const A = 'a'.repeat(32), B = 'b'.repeat(32), C = 'c'.repeat(32);
const runId = '20261115T234500Z-0123456789';
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.dataset = {};
    this.style = {setProperty() {}}; this.value = ''; this._text = ''; this.hidden = false;
    this.disabled = false; this.open = false; this.scrollHeight = 100; this.scrollTop = 0;
    const classes = new Set();
    this.classList = {add: (...values) => values.forEach((value) => classes.add(value)),
      remove: (...values) => values.forEach((value) => classes.delete(value)), contains: (value) => classes.has(value),
      toggle: (value) => { if (classes.has(value)) { classes.delete(value); return false; } classes.add(value); return true; }};
  }
  append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this._text = ''; this.append(...items); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener() {}
  getBoundingClientRect() { return {height: 140}; }
  scrollTo({top}) { this.scrollTop = top; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}
function fixture() {
  const nodes = new Map(), storage = new Map(), storageLog = [], requests = [];
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  let respond = async (url) => url === '/api/runs' ? {runs: []} : {conversations: []};
  const document = {getElementById: node, body: new Element('body'), documentElement: new Element('html'),
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    activeElement: new Element('button'),
    querySelectorAll: (selector) => selector === 'dialog[open]' ? [...nodes.values()].filter((item) => item.open) : [],
  };
  const context = {document, window: {matchMedia: () => ({matches: false})}, console,
    AbortController, setTimeout, clearTimeout, URL, Blob, requestAnimationFrame: (fn) => fn(),
    sessionStorage: {setItem: (key, value) => { storage.set(key, value); storageLog.push(['set', value]); },
      removeItem: (key) => { storage.delete(key); storageLog.push(['remove']); }},
    fetch: async (url, options) => {
      requests.push({url, ...options});
      const result = await respond(url, options);
      return {ok: !result.httpStatus || result.httpStatus < 400, status: result.httpStatus || 200,
        json: async () => result.body || result};
    },
  };
  vm.createContext(context);
  vm.runInContext(`(() => {${source.slice(start, end)}
    globalThis.ui = {requestDeletion, confirmDeletion, loadHistory, renderConversationHistory, renderConversation, openConversation,
      seed(active, other) { conversations.clear(); deletedConversationIds.clear(); busy = false;
        if (other) conversations.set(other.id, other); renderConversation(active); runs.set('${runId}', {run_id: '${runId}', question: 'Stored analysis', status: 'ok'}); },
      busy(value) { setBusy(value, false); },
      snapshot() { return {active: activeConversation?.id, ids: [...conversations.keys()], removed: [...deletedConversationIds],
        busy, deleting, generation, historyRevision, pending: pendingDeletion, runCount: runs.size}; }
    }; })();`, context, {timeout: 1000});
  context.ui.seed({id: A, title: 'Active chat', messages: []}, {id: B, title: 'Other chat', messages: []});
  node('question').value = 'Keep this draft';
  return {ui: context.ui, node, storage, storageLog, requests, setResponse: (value) => { respond = value; }};
}
let checked = 0;
async function test(name, check) { await check(); checked++; console.log(`PASS ${name}`); }
const drain = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

(async () => {
  await test('Delete controls are siblings, accessible, and confirmation is irreversible', async () => {
    const f = fixture(); f.ui.renderConversationHistory();
    const rows = f.node('conversation-history').children;
    assert.equal(rows.length, 2);
    for (const row of rows) {
      assert.equal(row.tagName, 'DIV');
      assert.equal(row.children.length, 2);
      assert(row.children.every((child) => child.tagName === 'BUTTON'));
      assert(row.children[1].attributes['aria-label'].startsWith('Delete chat:'));
    }
    f.ui.requestDeletion(B);
    assert.equal(f.requests.length, 0, 'Opening confirmation made a destructive request.');
    assert(f.node('delete-description').textContent.includes('cannot be undone'));
    assert(f.node('delete-description').textContent.includes('Analysis records remain'));
    assert(f.node('delete-dialog').open);
    assert.match(html, /id="delete-dialog"[^>]*aria-labelledby="delete-title"/);
  });

  await test('Inactive deletion preserves active chat, composer, selection, and runs', async () => {
    const f = fixture();
    f.setResponse(async (url, options) => options.method === 'DELETE' ? {deleted: [B], deleted_count: 1}
      : url === '/api/runs' ? {runs: []} : {conversations: [{id: A, title: 'Active chat'}]});
    f.ui.requestDeletion(B); await f.ui.confirmDeletion(); await drain();
    assert.equal(f.ui.snapshot().active, A);
    assert.deepEqual(Array.from(f.ui.snapshot().ids), [A]);
    assert.equal(f.node('question').value, 'Keep this draft');
    assert.equal(f.storage.values().next().value, A);
    assert.equal(f.ui.snapshot().runCount, 1);
    assert.equal(f.requests.filter((request) => request.method === 'POST').length, 0);
    const deletion = f.requests.find((request) => request.method === 'DELETE');
    assert.equal(deletion.url, `/api/conversations/${B}`); assert.equal(deletion.credentials, 'same-origin');
  });

  await test('Active deletion clears the old opaque ID and creates one fresh chat', async () => {
    const f = fixture();
    f.setResponse(async (url, options) => options.method === 'DELETE' ? {deleted: [A], deleted_count: 1}
      : options.method === 'POST' ? {id: C, title: 'New chat', messages: []}
      : url === '/api/runs' ? {runs: []} : {conversations: [{id: B}, {id: C}]});
    f.ui.requestDeletion(A); await f.ui.confirmDeletion(); await drain();
    assert.equal(f.ui.snapshot().active, C); assert(!f.ui.snapshot().ids.includes(A));
    assert(f.storageLog.some(([operation]) => operation === 'remove'));
    assert.equal(f.storage.values().next().value, C); assert.equal(f.node('question').value, '');
    assert.equal(f.requests.filter((request) => request.method === 'POST').length, 1);
    assert(!f.requests.some((request) => /\/messages$/.test(request.url)));
  });

  await test('Clear all resets chats and composer while retaining analysis runs', async () => {
    const f = fixture();
    f.setResponse(async (url, options) => options.method === 'DELETE' ? {deleted: [A, B], deleted_count: 2}
      : options.method === 'POST' ? {id: C, title: 'New chat', messages: []}
      : url === '/api/runs' ? {runs: []} : {conversations: [{id: C}]});
    f.ui.requestDeletion(); await f.ui.confirmDeletion(); await drain();
    assert.equal(f.requests.find((request) => request.method === 'DELETE').url, '/api/conversations');
    assert.deepEqual(Array.from(f.ui.snapshot().ids), [C]); assert.equal(f.ui.snapshot().runCount, 1);
    assert.equal(f.node('question').value, '');
  });

  await test('Local busy and service409 do not delete or create any chat', async () => {
    const f = fixture(); f.ui.busy(true); f.ui.requestDeletion(B); await f.ui.confirmDeletion();
    assert.equal(f.requests.length, 0); assert(f.node('clear-conversations').disabled);
    f.ui.busy(false); f.ui.requestDeletion(B);
    f.setResponse(async () => ({httpStatus: 409, body: {error: 'Chat busy'}}));
    await f.ui.confirmDeletion();
    assert.equal(f.ui.snapshot().active, A); assert.equal(f.ui.snapshot().ids.length, 2);
    assert(f.node('delete-dialog').open); assert(f.node('delete-error').textContent.includes('No chats were deleted'));
    assert.equal(f.ui.snapshot().busy, false); assert.equal(f.node('delete-confirm').disabled, false);
    assert.equal(f.requests.length, 1);
  });

  await test('Already-deleted404 reconciles the list with explicit feedback', async () => {
    const f = fixture();
    f.setResponse(async (url, options) => options.method === 'DELETE' ? {httpStatus: 404, body: {error: 'Not found'}}
      : url === '/api/runs' ? {runs: []} : {conversations: [{id: A}]});
    f.ui.requestDeletion(B); await f.ui.confirmDeletion(); await drain();
    assert(!f.ui.snapshot().ids.includes(B)); assert.equal(f.ui.snapshot().active, A);
    assert(f.node('system-error').textContent.includes('already deleted'));
  });

  await test('Late history and restore responses cannot resurrect a deleted conversation', async () => {
    const f = fixture(); let release;
    const old = new Promise((resolve) => { release = resolve; }); let listCalls = 0;
    f.setResponse(async (url, options) => {
      if (options.method === 'DELETE') return {deleted: [B], deleted_count: 1};
      if (url === '/api/runs') return {runs: []};
      if (url === '/api/conversations' && ++listCalls === 1) return old;
      return {conversations: [{id: A}]};
    });
    const earlierHistory = f.ui.loadHistory();
    f.ui.requestDeletion(B); await f.ui.confirmDeletion();
    release({conversations: [{id: A}, {id: B, title: 'Stale deleted chat'}]});
    await earlierHistory; await drain();
    assert(!f.ui.snapshot().ids.includes(B));
    const calls = f.requests.length;
    await f.ui.openConversation(B); f.ui.renderConversation({id: B, messages: []});
    assert.equal(f.requests.length, calls); assert.equal(f.ui.snapshot().active, A);
  });

  console.log(`\n${checked} isolated deletion UI checks passed. No real chats, environment files, or services were accessed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
