#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const {fixture, waitFor, status, A, J} = require('./web_dom_harness');
const children = (node) => [node, ...node.children.flatMap(children)];
const answer = (content = 'Fixture saved answer') => ({conversation: {id: A, title: 'Fixture chat', messages: [{id: 'assistant-fixture', role: 'assistant', content}]}, result: null});
const defaults = (url) => url === '/api/status' ? status() : url === '/api/runs' ? {runs: []} : {conversations: []};
let checked = 0, failed = 0;
async function test(name, fn) { try { await fn(); checked++; console.log(`PASS ${name}`); } catch (error) { failed++; console.error(`FAIL ${name}: ${error.message}`); } }

(async () => {
  for (const outcome of ['acknowledged', 'network failure']) {
    await test(`UI-01: typing a new draft during delayed POST preserves it after ${outcome}`, async () => {
      const f = fixture(); let finish, reject;
      f.respond(async (url) => {
        if (url.endsWith('/messages')) return await new Promise((resolve, no) => { finish = resolve; reject = no; });
        return url === '/api/status' ? status() : url === '/api/runs' ? {runs: []} : {conversations: []};
      });
      f.ui.input('Submitted message'); const sending = f.ui.ask({preventDefault() {}});
      await waitFor(() => typeof finish === 'function'); f.ui.input('My next draft stays here');
      if (outcome === 'acknowledged') finish({conversation: {id: A, title: 'Fixture chat', messages: []}, result: null});
      else reject(new TypeError('Fixture connection interrupted'));
      await sending;
      assert.equal(f.$('question').value, 'My next draft stays here');
    });
  }
  await test('A user who edits then empties the next draft does not get the old text restored', async () => {
    const f = fixture(); let reject;
    f.respond(async (url) => url.endsWith('/messages') ? await new Promise((_, no) => { reject = no; }) : defaults(url));
    f.ui.input('First turn'); const sending = f.ui.ask({preventDefault() {}}); await waitFor(() => reject);
    f.ui.input('Changed draft'); f.ui.input(''); reject(new TypeError('Fixture lost connection')); await sending;
    assert.equal(f.$('question').value, '');
  });
  await test('An explicit network retry reuses the saved UUID and never consumes the next draft', async () => {
    const f = fixture(); let attempts = 0;
    f.respond(async (url) => { if (url.endsWith('/messages')) { if (++attempts === 1) throw new TypeError('Fixture lost acknowledgment'); return answer(); } return defaults(url); });
    f.ui.input('Same saved turn'); await f.ui.ask({preventDefault() {}});
    const first = JSON.parse(f.calls.find((call) => call.url.endsWith('/messages')).body);
    assert.match(first.client_message_id, /^[a-f0-9-]{36}$/i); assert(f.ui.snapshot().pending);
    f.ui.input('Different next draft'); await f.ui.retry(first.client_message_id);
    const requests = f.calls.filter((call) => call.url.endsWith('/messages')).map((call) => JSON.parse(call.body));
    assert.equal(requests.length, 2); assert.deepEqual(requests[1], requests[0]); assert.equal(f.$('question').value, 'Different next draft');
    assert.equal(f.ui.snapshot().pending, null); assert(![...f.storage.keys()].some((key) => key.includes('_pending_')));
  });
  await test('A genuinely new message gets a new UUID even when its text is identical', async () => {
    const f = fixture(); f.respond(async (url) => url.endsWith('/messages') ? answer() : defaults(url));
    for (let i = 0; i < 2; i++) { f.ui.input('Deliberately repeated text'); await f.ui.ask({preventDefault() {}}); }
    const requests = f.calls.filter((call) => call.url.endsWith('/messages')).map((call) => JSON.parse(call.body));
    assert.equal(requests.length, 2); assert.notEqual(requests[0].client_message_id, requests[1].client_message_id);
  });
  await test('Reload can recover an unacknowledged UUID without automatically sending it', async () => {
    const first = fixture(); first.respond(async (url) => { if (url.endsWith('/messages')) throw new TypeError('Fixture lost acknowledgment'); return defaults(url); });
    first.ui.input('Saved pending fixture'); await first.ui.ask({preventDefault() {}});
    const id = first.ui.snapshot().pending.client_message_id, second = fixture({storage: first.storage});
    assert.equal(second.ui.restorePending(A).client_message_id, id); assert.equal(second.calls.length, 0);
    second.respond(async (url) => url.endsWith('/messages') ? answer() : defaults(url));
    second.ui.input('New draft after reload'); await second.ui.retry(id);
    assert.equal(JSON.parse(second.calls.find((call) => call.url.endsWith('/messages')).body).client_message_id, id);
    assert.equal(second.$('question').value, 'New draft after reload');
  });
  await test('Restore polls queued/running work and loads the completed answer without POST', async () => {
    const f = fixture(); let polls = 0;
    f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], active_job_id: J}
      : url === `/api/jobs/${J}` ? (++polls < 3 ? {id: J, status: 'running', execution_state: polls === 1 ? 'queued' : 'running', events: []} : {id: J, status: 'complete', execution_state: 'succeeded', response: answer()}) : defaults(url));
    f.ui.input('Keep restored draft'); await f.ui.openConversation(A);
    assert.equal(polls, 3); assert.equal(f.$('question').value, 'Keep restored draft'); assert(f.$('conversation').textContent.includes('Fixture saved answer'));
    assert(!f.calls.some((call) => call.method === 'POST')); assert.equal(f.ui.snapshot().busy, false);
  });
  for (const phase of ['unknown', 'blocked', 'failed', 'cancelled']) {
    await test(`Saved ${phase} state is explained without automatic resubmission`, async () => {
      const f = fixture(); f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], latest_job: {id: J, execution_state: phase, can_resume: false, events: []}} : defaults(url));
      f.ui.input('Preserved terminal draft'); await f.ui.openConversation(A);
      assert.equal(f.$('question').value, 'Preserved terminal draft'); assert(!f.calls.some((call) => call.method === 'POST'));
      assert(f.$('conversation').textContent.includes('Execution status')); assert(!f.$('conversation').textContent.includes('Resume saved work'));
      assert(f.$('conversation').textContent.includes(phase === 'unknown' ? 'uncertain' : phase === 'failed' ? 'could not complete' : phase));
    });
  }
  await test('An unavailable/restarted job is uncertain, not silently resubmitted', async () => {
    const f = fixture(); f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], active_job_id: J}
      : url === `/api/jobs/${J}` ? {httpStatus: 404, body: {error: 'Fixture job unavailable'}} : defaults(url));
    await f.ui.openConversation(A); assert(f.$('conversation').textContent.includes('execution outcome cannot be confirmed'));
    assert(!f.calls.some((call) => call.method === 'POST'));
  });
  await test('Cancellation requested stays distinct from actual cancelled state', async () => {
    const f = fixture(); let cancelled = false;
    f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], active_job_id: J}
      : url === `/api/jobs/${J}/cancel` ? {id: J, status: 'running', execution_state: 'running', cancel_requested: true}
      : url === `/api/jobs/${J}` ? {id: J, status: cancelled ? 'failed' : 'running', execution_state: cancelled ? 'cancelled' : 'running'} : defaults(url));
    const restored = f.ui.openConversation(A); await waitFor(() => f.ui.snapshot().currentJobId === J && !f.$('cancel-job').hidden);
    await f.ui.cancel(); assert.equal(f.$('cancel-job').textContent, 'Cancellation requested'); assert(f.$('cancel-job').disabled);
    assert(f.$('cancel-job-detail').textContent.includes('not undone')); cancelled = true; await restored;
    assert(f.$('conversation').textContent.includes('This turn was cancelled')); assert.equal(f.ui.snapshot().currentJobId, null);
    assert.equal(f.calls.filter((call) => call.url.endsWith('/cancel')).length, 1);
  });
  await test('Safe recovery runs only after an explicit Resume saved work action', async () => {
    const f = fixture(); f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], latest_job: {id: J, execution_state: 'unknown', can_resume: true}}
      : url === `/api/jobs/${J}/resume` ? {id: J, status: 'running', execution_state: 'queued'}
      : url === `/api/jobs/${J}` ? {id: J, execution_state: 'succeeded', status: 'complete', response: answer('Recovered saved fixture')} : defaults(url));
    await f.ui.openConversation(A); assert(!f.calls.some((call) => call.method === 'POST'));
    const resume = children(f.$('conversation')).find((node) => node.tagName === 'BUTTON' && node.textContent === 'Resume saved work'); assert(resume);
    f.ui.input('Draft during recovery'); await resume.trigger('click'); assert.equal(f.$('question').value, 'Draft during recovery');
    assert.equal(f.calls.filter((call) => call.url.endsWith('/resume')).length, 1); assert(!f.calls.some((call) => call.url.endsWith('/messages')));
  });
  await test('A recovery-required queued job pauses polling and offers explicit recovery', async () => {
    const f = fixture(); let resumed = false;
    f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], active_job_id: J}
      : url === `/api/jobs/${J}/resume` ? (resumed = true, {id: J, status: 'running', execution_state: 'queued', recovery_required: false})
      : url === `/api/jobs/${J}` ? resumed ? {id: J, execution_state: 'succeeded', response: answer('Safely recovered queued fixture')} : {id: J, status: 'running', execution_state: 'queued', recovery_required: true, can_resume: true} : defaults(url));
    const restoring = f.ui.openConversation(A);
    try {
      await waitFor(() => f.calls.some((call) => call.url === `/api/jobs/${J}`));
      assert(f.$('conversation').textContent.includes('Resume saved work'), 'Interrupted queued work must not wait forever in the active polling loop.');
      await restoring;
      assert.equal(f.ui.snapshot().busy, false); assert.equal(f.calls.filter((call) => call.url === `/api/jobs/${J}`).length, 1);
      assert(!f.calls.some((call) => call.method === 'POST'));
      f.ui.input('Do not overwrite this new draft'); await f.ui.ask({preventDefault() {}});
      assert.equal(f.$('question').value, 'Do not overwrite this new draft'); assert(!f.calls.some((call) => call.url.endsWith('/messages')));
      const resume = children(f.$('conversation')).find((node) => node.tagName === 'BUTTON' && node.textContent === 'Resume saved work');
      await resume.trigger('click'); assert(f.$('conversation').textContent.includes('Safely recovered queued fixture'));
    } finally { f.ui.status(status('public-cleanup-scope')); await restoring; }
  });
  await test('Interrupted queued work can be cancelled explicitly without a new message', async () => {
    const f = fixture(); f.respond(async (url) => url === `/api/conversations/${A}` ? {id: A, title: 'Saved', messages: [], active_job_id: J}
      : url === `/api/jobs/${J}/cancel` ? {id: J, execution_state: 'cancelled', cancel_requested: true, can_resume: false}
      : url === `/api/jobs/${J}` ? {id: J, execution_state: 'queued', recovery_required: true, can_resume: true} : defaults(url));
    await f.ui.openConversation(A); f.ui.input('Draft after cancellation');
    const cancel = children(f.$('conversation')).find((node) => node.tagName === 'BUTTON' && node.textContent === 'Cancel saved turn'); assert(cancel);
    await cancel.trigger('click'); assert(f.$('conversation').textContent.includes('This turn was cancelled'));
    assert.equal(f.$('question').value, 'Draft after cancellation'); assert.equal(f.calls.filter((call) => call.url.endsWith('/cancel')).length, 1);
    assert(!f.calls.some((call) => call.url.endsWith('/messages') || call.url.endsWith('/resume')));
  });
  await test('A public scope change clears pending content and rejects a late POST acknowledgment', async () => {
    const f = fixture(); let finish;
    f.respond(async (url) => url.endsWith('/messages') ? await new Promise((resolve) => { finish = resolve; }) : defaults(url));
    f.ui.input('Prior-workspace private fixture'); const sending = f.ui.ask({preventDefault() {}}); await waitFor(() => finish);
    f.ui.input('Prior-workspace draft'); f.ui.status(status('public-other')); finish(answer('Prior-workspace answer')); await sending;
    assert.equal(f.$('question').value, ''); assert.equal(f.ui.snapshot().pending, null); assert.equal(f.ui.snapshot().active, undefined);
    assert(!f.$('conversation').textContent.includes('Prior-workspace')); assert(![...f.storage.keys()].some((key) => key.includes('_pending_')));
  });
  console.log(`\n${checked} turn checks passed; ${failed} failed. Isolated fixtures only.`);
  if (failed) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exitCode = 1; });
