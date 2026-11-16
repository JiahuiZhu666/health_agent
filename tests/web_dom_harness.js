'use strict';

// A small isolated DOM/API adapter for the actual private frontend functions.
// No production server, environment files, survey data, or model calls are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '..', 'web/app.js'), 'utf8');
const start = source.indexOf('(() => {') + '(() => {'.length;
const end = source.indexOf("  $('sidebar-toggle').addEventListener('click'");
assert(start > 0 && end > start);
const A = 'a'.repeat(32), B = 'b'.repeat(32), J = 'c'.repeat(32);
const status = (scope = 'private') => ({access: {public: scope !== 'private', scope}, data: {ready: true, income_ready: true}, statistics: {ready: true}, dialogue: {configured: true, connection: {status: 'unverified'}}});

function fixture(options = {}) {
  const nodes = new Map(), storage = options.storage || new Map(), calls = [];
  let reply = async (url) => url === '/api/status' ? status(options.scope || 'private') : url === '/api/runs' ? {runs: []} : {conversations: []};
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.style = {setProperty() {}}; this.attributes = {}; this.listeners = {}; this.dataset = {};
      this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this.open = false; this._text = '';
      this.scrollTop = 0; this.scrollHeight = 100; this.clientHeight = 100; this.isConnected = true;
      const classes = new Set(); this.classList = {add: (...values) => values.forEach((v) => classes.add(v)), remove: (...values) => values.forEach((v) => classes.delete(v)), contains: (v) => classes.has(v), toggle: (v) => classes.has(v) ? classes.delete(v) : classes.add(v)};
    }
    append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } }
    replaceChildren(...items) { this.children = []; this._text = ''; this.append(...items); }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    get lastElementChild() { return this.children.at(-1); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    async trigger(type) { return await this.listeners[type]?.({preventDefault() {}, target: this}); }
    focus() {} getBoundingClientRect() { return {height: 120}; } getClientRects() { return [1]; }
    scrollTo({top}) { this.scrollTop = top; } closest() { return null; }
    showModal() { this.open = true; } close() { this.open = false; }
    remove() { this.isConnected = false; if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); }
  }
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = {getElementById: $, body: new Element('body'), documentElement: new Element('html'), activeElement: new Element('button'),
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    createTextNode: (value) => { const node = new Element('#text'); node.textContent = value; return node; },
    querySelectorAll: (selector) => selector === 'dialog[open]' ? [...nodes.values()].filter((node) => node.open) : [],
    querySelector: (selector) => selector === 'dialog[open]' ? [...nodes.values()].find((node) => node.open) : null};
  const context = {document, navigator: {cookieEnabled: true}, window: {matchMedia: () => ({matches: false})}, crypto: webcrypto,
    console, AbortController, URL, Blob, setTimeout, clearTimeout, requestAnimationFrame: (fn) => fn(),
    sessionStorage: {setItem: (key, value) => storage.set(key, value), getItem: (key) => storage.get(key), removeItem: (key) => storage.delete(key)},
    fetch: async (url, request) => { calls.push({url, ...request}); const value = await reply(url, request);
      return {ok: !value.httpStatus || value.httpStatus < 400, status: value.httpStatus || 200, headers: {get: () => value.retryHeader ?? null}, json: async () => value.body || value}; }};
  vm.createContext(context);
  vm.runInContext(`(() => {${source.slice(start, end)}
    globalThis.ui = {ask, openConversation, resetWorkspace, pollJob, acceptJob, applyAccess, loadHistory, loadMoreConversations, loadMoreRuns, loadEarlierMessages, mergeMessagePages,
      cancel: (...args) => typeof cancelCurrentJob === 'function' ? cancelCurrentJob(...args) : undefined,
      resume: (...args) => typeof resumeSavedJob === 'function' ? resumeSavedJob(...args) : undefined,
      retry: (...args) => typeof retryPendingSubmission === 'function' ? retryPendingSubmission(...args) : undefined,
      restorePending: (...args) => typeof restorePendingSubmission === 'function' ? restorePendingSubmission(...args) : undefined,
      input(value) { $('question').value = value; if (typeof composerInput === 'function') composerInput(); else autoGrow(); },
      status(value) { state = value; applyAccess(value); },
      seed(conversation) { renderConversation(conversation); },
      snapshot() { return {busy, currentJobId, generation, active:activeConversation?.id, pending:typeof pendingSubmission === 'undefined' ? null : pendingSubmission,
        conversationCursor, runCursor, messages: activeConversation?.messages, pagination: activeConversation?.pagination, chatIds:[...conversations.keys()], runIds:[...runs.keys()]}; }
    }; })();`, context, {timeout: 1000});
  context.ui.status(status(options.scope || 'private'));
  context.ui.seed({id: A, title: 'Fixture chat', messages: []});
  return {$, ui: context.ui, calls, storage, context, respond: (fn) => { reply = fn; }};
}
const waitFor = async (predicate) => { for (let i = 0; i < 100; i++) { if (predicate()) return; await new Promise((resolve) => setImmediate(resolve)); } throw new Error('Fixture did not reach expected state.'); };
module.exports = {fixture, waitFor, status, A, B, J};
