#!/usr/bin/env node
'use strict';
// Cursor/window fixtures exercise production UI code; no real records or API.
const assert = require('node:assert/strict');
const {fixture, status, A, B} = require('./web_dom_harness');
const run = (n) => `20261116T120000Z-${n.toString(16).padStart(10, '0')}`;
const message = (n, content = `Fixture message ${n}`) => ({id: n.toString(16).padStart(32, '0'), role: n % 2 ? 'user' : 'assistant', content});
const range = (start, end) => Array.from({length: end - start + 1}, (_, index) => message(start + index));
const defaults = (url) => url === '/api/status' ? status() : url === '/api/runs' ? {runs: []} : {conversations: []};
const tail = () => ({id: A, title: 'Fixture long chat', messages: range(51, 100), pagination: {has_more: true, before: message(51).id, total: 100}});
let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log(`PASS ${name}`); }

(async () => {
  await test('Initial URLs stay unchanged; only genuine next cursors expose load-more controls', async () => {
    const f = fixture(); f.respond(async (url) => url === '/api/runs' ? {runs: [{run_id: run(30), question: 'Fixture', status: 'ok'}], next_cursor: run(30)} : url === '/api/conversations' ? {conversations: [{id: A, title: 'Newest fixture'}], next_cursor: 'opaque:next/+?='} : defaults(url));
    await f.ui.loadHistory(); assert.deepEqual(f.calls.map((call) => call.url).sort(), ['/api/conversations', '/api/runs']);
    assert.equal(f.$('load-more-conversations').hidden, false); assert.equal(f.$('load-more-runs').hidden, false);
    const legacy = fixture(); legacy.respond(async (url) => defaults(url)); await legacy.ui.loadHistory();
    assert.equal(legacy.$('load-more-conversations').hidden, true); assert.equal(legacy.$('load-more-runs').hidden, true);
  });
  await test('Chat pagination encodes opaque cursors and merges summaries without duplicates or rollback', async () => {
    const f = fixture(), cursor = 'opaque:next/+?=';
    f.respond(async (url) => url === '/api/conversations' ? {conversations: [{id: A, title: 'Newest title', updated_at: '2026-11-16T12:00:00Z'}], next_cursor: cursor}
      : url === `/api/conversations?cursor=${encodeURIComponent(cursor)}` ? {conversations: [{id: A, title: 'Stale title', updated_at: '2026-11-15T12:00:00Z'}, {id: B, title: 'Older fixture'}], next_cursor: null} : defaults(url));
    await f.ui.loadHistory(); f.ui.input('Keep this draft'); await f.ui.loadMoreConversations();
    assert.equal(f.ui.snapshot().chatIds.length, 2); assert(f.$('conversation-history').textContent.includes('Newest title'));
    assert(!f.$('conversation-history').textContent.includes('Stale title')); assert.equal(f.$('question').value, 'Keep this draft');
    assert.equal(f.$('load-more-conversations').hidden, true); assert(!f.calls.some((call) => call.method === 'POST'));
  });
  await test('Analysis pages use before=runid, retain the active chat, and do not fetch source records', async () => {
    const f = fixture();
    f.respond(async (url) => url === '/api/runs' ? {runs: [{run_id: run(30), question: 'New fixture', status: 'ok'}], next_cursor: run(30)}
      : url === `/api/runs?before=${run(30)}` ? {runs: [{run_id: run(29), question: 'Older fixture', status: 'ok'}], next_cursor: null} : defaults(url));
    await f.ui.loadHistory(); f.ui.input('Keep analysis-panel draft'); await f.ui.loadMoreRuns();
    assert.equal(f.ui.snapshot().active, A); assert.equal(f.ui.snapshot().runIds.length, 2); assert.equal(f.$('question').value, 'Keep analysis-panel draft');
    assert.equal(f.$('load-more-runs').hidden, true); assert(!f.calls.some((call) => /^\/api\/runs\//.test(call.url)));
  });
  await test('Earlier messages prepend in order; drafts, pending IDs, and retry controls survive', async () => {
    const f = fixture(); f.ui.seed(tail());
    f.respond(async (url) => { if (url.endsWith('/messages')) throw new TypeError('Fixture unacknowledged submission');
      if (url === `/api/conversations/${A}?before=${message(51).id}&limit=50`) return {id: A, messages: range(1, 50), pagination: {has_more: false, before: message(1).id, total: 100}};
      return defaults(url); });
    f.ui.input('Pending fixture turn'); await f.ui.ask({preventDefault() {}}); const id = f.ui.snapshot().pending.client_message_id;
    f.ui.input('Draft while loading earlier messages'); await f.ui.loadEarlierMessages();
    const snapshot = f.ui.snapshot(); assert.equal(snapshot.messages.length, 100); assert.equal(snapshot.messages[0].id, message(1).id); assert.equal(snapshot.messages[99].id, message(100).id);
    assert.equal(snapshot.pending.client_message_id, id); assert.equal(f.$('question').value, 'Draft while loading earlier messages');
    assert(f.$('conversation').textContent.includes('Retry same submission')); assert.equal(f.$('load-earlier-messages').hidden, true);
  });
  await test('Stable IDs deduplicate overlaps; a job last-50 snapshot retains loaded earlier messages', async () => {
    const f = fixture(); f.ui.seed({id: A, title: 'Full loaded history', messages: range(1, 100), pagination: {has_more: false, before: message(1).id, total: 100}});
    const merged = f.ui.mergeMessagePages([message(1), message(2, 'Older text')], [message(2, 'Updated text'), message(3)]);
    assert.equal(merged.length, 3); assert.equal(merged[1].content, 'Updated text');
    f.respond(async (url) => defaults(url));
    await f.ui.acceptJob({response: {conversation: {id: A, title: 'Full loaded history', messages: range(52, 101), pagination: {has_more: true, before: message(52).id, total: 101}}, result: null}}, f.ui.snapshot().generation, A);
    assert.equal(f.ui.snapshot().messages.length, 101); assert.equal(f.ui.snapshot().messages[0].id, message(1).id);
    assert.equal(f.ui.snapshot().pagination.has_more, false); assert.equal(f.ui.snapshot().pagination.before, message(1).id);
    assert.equal(new Set(f.ui.snapshot().messages.map((item) => item.id)).size, 101);
  });
  await test('Switching chats rejects a late earlier-page response', async () => {
    const f = fixture(); f.ui.seed(tail()); let finish;
    f.respond(async (url) => url.includes('?before=') ? await new Promise((resolve) => { finish = resolve; }) : url === `/api/conversations/${B}` ? {id: B, title: 'Other chat', messages: [message(200)]} : defaults(url));
    const earlier = f.ui.loadEarlierMessages(); await f.ui.openConversation(B); finish({id: A, messages: range(1, 50), pagination: {has_more: false}}); await earlier;
    assert.equal(f.ui.snapshot().active, B); assert.equal(f.ui.snapshot().messages.length, 1); assert.equal(f.ui.snapshot().messages[0].id, message(200).id);
  });
  await test('Workspace changes reject late lists and messages and clear private drafts', async () => {
    const f = fixture(); f.ui.seed(tail()); let finish;
    f.respond(async (url) => url.includes('?before=') ? await new Promise((resolve) => { finish = resolve; }) : defaults(url));
    const earlier = f.ui.loadEarlierMessages(); f.ui.input('Private draft fixture'); f.ui.status(status('public-next-scope'));
    finish({id: A, messages: range(1, 50), pagination: {has_more: false}}); await earlier;
    assert.equal(f.ui.snapshot().active, undefined); assert.equal(f.$('question').value, ''); assert.equal(f.$('conversation').textContent, '');
    assert.equal(f.$('load-more-conversations').hidden, true); assert.equal(f.$('load-earlier-messages').hidden, true);
  });
  await test('A page from a different conversation is rejected without changing the draft', async () => {
    const f = fixture(); f.ui.seed(tail()); f.ui.input('Preserve correct-thread draft');
    f.respond(async () => ({id: B, messages: range(1, 50), pagination: {has_more: false}})); await f.ui.loadEarlierMessages();
    assert.equal(f.ui.snapshot().messages.length, 50); assert.equal(f.ui.snapshot().active, A); assert.equal(f.$('question').value, 'Preserve correct-thread draft');
    assert(f.$('message-page-error').textContent.includes('did not match this conversation'));
  });
  console.log(`\n${passed} isolated pagination checks passed. No real history or workspace was accessed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
