'use strict';

// Browser-test transport only. Never imports Python, reads .env, accesses real
// conversations/data, or makes outbound requests. All state lives in this process.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID, createHash} = require('node:crypto');
const root = path.resolve(__dirname, '../..'), visitors = new Map();
const opaque = () => randomUUID().replaceAll('-', '');
const clone = (value) => JSON.parse(JSON.stringify(value));
const runId = '20261116T120000Z-0123456789';
const knownStates = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked', 'cancelled', 'unknown']);
const chart = {id: `${runId}-proportion`, title: 'Fixture survey chart', subtitle: 'Artificial browser-test data; not an NHIS estimate.',
  kind: 'proportion', style: 'point', unit: 'percent', reference: 0, x_label: 'Fixture outcome (%)', source_run_id: runId,
  groups: [{label: 'Fixture group A', group_id: 'fixture_a', value: 12.3, low: 10.1, high: 14.6, n: 321}]};

function visitor(req, res) {
  let id = /(?:^|;\s*)nhis_fixture=([a-f0-9]{32})(?:;|$)/.exec(req.headers.cookie || '')?.[1];
  if (!id || !visitors.has(id)) { id = opaque(); visitors.set(id, {id, conversations: new Map(), runs: new Map(), jobs: new Map(), submissions: new Map(), messagePosts: 0, createdJobs: 0, pendingAcks: new Map()}); res.setHeader('Set-Cookie', `nhis_fixture=${id}; Path=/; HttpOnly; SameSite=Lax`); }
  return visitors.get(id);
}
function json(res, body, code = 200) {
  if (res.destroyed) return;
  res.writeHead(code, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(body));
}
function summary(job) {
  return {id: job.id, status: ['queued', 'running'].includes(job.phase) ? 'running' : job.phase === 'succeeded' ? 'complete' : 'failed',
    execution_state: job.phase, client_message_id: job.clientId, cancel_requested: !!job.cancelRequested,
    recovery_required: job.phase === 'unknown', can_resume: job.mode === 'safe-resume' && job.phase === 'unknown'};
}
function publicConversation(v, conversation, before = null, limit = 50) {
  const result = clone(conversation), jobs = [...v.jobs.values()].filter((job) => job.cid === conversation.id), latest = jobs.at(-1);
  if (latest) { result.latest_job = summary(latest); if (['queued', 'running'].includes(latest.phase)) result.active_job_id = latest.id; }
  const stop = before ? conversation.messages.findIndex((message) => message.id === before) : conversation.messages.length;
  const start = Math.max(0, stop - limit); result.messages = clone(conversation.messages.slice(start, stop));
  result.pagination = {has_more: start > 0, before: result.messages[0]?.id || null, total: conversation.messages.length};
  return result;
}
function complete(v, job) {
  if (job.phase === 'succeeded') return;
  job.phase = 'succeeded';
  const conversation = v.conversations.get(job.cid);
  if (!conversation) return;
  const message = {id: opaque(), role: 'assistant', content: 'Fixture reply saved without a live model call.', generation: {status: 'local'}, status: 'ok', created_at: new Date().toISOString()};
  if (job.mode === 'chart') { message.content = 'Fixture chart for a browser rendering and download check.'; message.kind = 'visualization'; message.generation = {status: 'local', type: 'visualization'}; message.charts = [chart]; }
  conversation.messages.push(message); conversation.updated_at = new Date().toISOString();
  job.response = {conversation_id: conversation.id, conversation: publicConversation(v, conversation), message, result: null};
}
function publicJob(v, job) {
  return {...summary(job), events: [{stage: job.phase === 'queued' ? 'queue' : 'reply', label: job.phase === 'queued' ? 'Waiting for an available worker' : 'Fixture execution state', status: ['queued', 'running'].includes(job.phase) ? 'running' : 'complete', detail: 'Observed fixture transport state; no external execution.', timestamp: new Date().toISOString(), facts: {}}], ...(job.response ? {response: job.response} : {})};
}
async function body(req) {
  let raw = ''; for await (const chunk of req) { raw += chunk; if (raw.length > 100000) throw new Error('Fixture request too large'); }
  return raw ? JSON.parse(raw) : {};
}
const server = http.createServer(async (req, res) => {
  try {
    const parsed = new URL(req.url, 'http://127.0.0.1:18765'), pathname = parsed.pathname;
    if (pathname === '/__fixture__/health') return json(res, {fixture: true});
    const v = visitor(req, res);
    if (pathname === '/__fixture__/state') return json(res, {message_posts: v.messagePosts, created_jobs: v.createdJobs,
      conversations: [...v.conversations.values()].map((item) => publicConversation(v, item)), jobs: [...v.jobs.values()].map(summary)});
    if (pathname === '/__fixture__/seed-pagination' && req.method === 'POST') {
      const value = await body(req), count = Math.max(1, Math.min(65, Number(value.chats) || 1)), totalMessages = Math.max(0, Math.min(160, Number(value.messages) || 0));
      for (let i = 0; i < count; i++) {
        const timestamp = new Date(Date.UTC(2026, 10, 16, 12, i)).toISOString();
        const conversation = {id: opaque(), title: totalMessages ? 'Fixture long chat' : `Fixture paginated chat ${i + 1}`, messages: [], context: {}, created_at: timestamp, updated_at: timestamp};
        for (let j = 1; j <= totalMessages; j++) conversation.messages.push({id: j.toString(16).padStart(32, '0'), role: j % 2 ? 'user' : 'assistant', content: `Fixture historical message ${j}`, status: 'ok', generation: {status: 'local'}});
        v.conversations.set(conversation.id, conversation);
      }
      for (let i = 1; i <= Math.min(65, Number(value.runs) || 0); i++) { const id = `20261116T120000Z-${i.toString(16).padStart(10, '0')}`; v.runs.set(id, {run_id: id, question: `Artificial pagination fixture ${i}`, status: 'ok', analysis: 'proportion'}); }
      return json(res, {fixture: true, conversations: v.conversations.size, runs: v.runs.size});
    }
    if (pathname === '/__fixture__/release' && req.method === 'POST') {
      const value = await body(req), job = v.jobs.get(value.job_id) || [...v.jobs.values()].at(-1);
      if (!job) return json(res, {error: 'Fixture job not found'}, 404);
      const phase = value.execution_state || 'succeeded'; if (!knownStates.has(phase)) return json(res, {error: 'Invalid fixture state'}, 400);
      if (phase === 'succeeded') complete(v, job); else job.phase = phase;
      const acknowledgment = v.pendingAcks.get(job.id); if (acknowledgment) { v.pendingAcks.delete(job.id); json(acknowledgment, {job_id: job.id, conversation_id: job.cid, status: 'running', client_message_id: job.clientId}, 202); }
      return json(res, publicJob(v, job));
    }
    if (pathname === '/api/status') return json(res, {access: {public: true, scope: 'public-' + createHash('sha256').update(v.id).digest('hex').slice(0, 16)}, data: {ready: true, income_ready: true, source: 'Browser-test transport only'}, statistics: {ready: true}, dialogue: {configured: false, connection: {status: 'not_configured'}}, registry: {}, capabilities: []});
    if (pathname === '/api/runs') {
      const before = parsed.searchParams.get('before'), all = [...v.runs.values()].sort((a, b) => b.run_id.localeCompare(a.run_id)).filter((item) => !before || item.run_id < before), page = all.slice(0, 30);
      return json(res, {runs: page, next_cursor: all.length > 30 ? page.at(-1).run_id : null});
    }
    if (pathname === '/api/conversations' && req.method === 'GET') {
      const all = [...v.conversations.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at)), raw = parsed.searchParams.get('cursor'), offset = raw ? Number(Buffer.from(raw, 'base64url').toString()) : 0;
      const page = all.slice(offset, offset + 30);
      return json(res, {conversations: page.map((item) => ({id: item.id, title: item.title, updated_at: item.updated_at, message_count: item.messages.length})), next_cursor: offset + 30 < all.length ? Buffer.from(String(offset + 30)).toString('base64url') : null});
    }
    if (pathname === '/api/conversations' && req.method === 'POST') {
      const conversation = {id: opaque(), title: 'New chat', messages: [], context: {}, created_at: new Date().toISOString(), updated_at: new Date().toISOString()}; v.conversations.set(conversation.id, conversation); return json(res, conversation);
    }
    if (pathname === '/api/conversations' && req.method === 'DELETE') { const ids = [...v.conversations.keys()]; v.conversations.clear(); return json(res, {deleted: ids, deleted_count: ids.length}); }
    const conversationRoute = /^\/api\/conversations\/([a-f0-9]{32})(\/messages)?$/.exec(pathname);
    if (conversationRoute) {
      const conversation = v.conversations.get(conversationRoute[1]); if (!conversation) return json(res, {error: 'Conversation not found'}, 404);
      if (!conversationRoute[2] && req.method === 'GET') return json(res, publicConversation(v, conversation, parsed.searchParams.get('before'), Math.min(50, Number(parsed.searchParams.get('limit')) || 50)));
      if (!conversationRoute[2] && req.method === 'DELETE') { v.conversations.delete(conversation.id); return json(res, {deleted: [conversation.id], deleted_count: 1}); }
      if (conversationRoute[2] && req.method === 'POST') {
        v.messagePosts++; const value = await body(req);
        if (!/^[a-f0-9-]{36}$/i.test(value.client_message_id || '')) return json(res, {error: 'A fixture client message ID is required'}, 400);
        const key = `${conversation.id}:${value.client_message_id}`, request = JSON.stringify(value), existing = v.submissions.get(key);
        if (existing) return existing.request === request ? json(res, {job_id: existing.job.id, conversation_id: conversation.id, idempotent: true}, 202) : json(res, {error: 'Submission ID conflict'}, 409);
        const text = String(value.message || ''), mode = text.includes('fixture:delayed-ack') ? 'delay' : text.includes('fixture:dropped-ack') ? 'drop' : text.includes('fixture:running') ? 'hold' : text.includes('fixture:unknown') ? 'unknown' : text.includes('fixture:safe-resume') ? 'safe-resume' : text.includes('fixture:chart') ? 'chart' : 'complete';
        const job = {id: opaque(), cid: conversation.id, clientId: value.client_message_id, phase: ['unknown', 'safe-resume'].includes(mode) ? 'unknown' : 'queued', mode, polls: 0}; v.jobs.set(job.id, job); v.submissions.set(key, {request, job}); v.createdJobs++;
        conversation.title = text.slice(0, 60); conversation.updated_at = new Date().toISOString(); conversation.messages.push({id: opaque(), client_message_id: value.client_message_id, role: 'user', content: text, created_at: conversation.updated_at});
        if (mode === 'delay') { v.pendingAcks.set(job.id, res); return; }
        if (mode === 'drop') { res.writeHead(202, {'Content-Type': 'application/json', 'Content-Length': '9999', 'Connection': 'close'}); res.end('{'); return; }
        return json(res, {job_id: job.id, conversation_id: conversation.id, status: 'running'}, 202);
      }
    }
    const jobRoute = /^\/api\/jobs\/([a-f0-9]{32})(?:\/(cancel|resume))?$/.exec(pathname);
    if (jobRoute) {
      const job = v.jobs.get(jobRoute[1]); if (!job) return json(res, {error: 'Job not found'}, 404);
      if (jobRoute[2] === 'cancel' && req.method === 'POST') { job.cancelRequested = true; job.phase = 'running'; job.cancelPolls = 0; return json(res, publicJob(v, job)); }
      if (jobRoute[2] === 'resume' && req.method === 'POST') { if (job.mode !== 'safe-resume') return json(res, {error: 'Unknown external work cannot be resumed'}, 409); job.mode = 'complete'; job.phase = 'queued'; return json(res, publicJob(v, job)); }
      if (!jobRoute[2] && req.method === 'GET') {
        if (job.cancelRequested) { if (++job.cancelPolls >= 2) job.phase = 'cancelled'; }
        else if (['complete', 'chart', 'drop'].includes(job.mode) && job.phase === 'queued') complete(v, job);
        else if (job.mode === 'hold' && job.phase === 'queued' && ++job.polls > 1) job.phase = 'running';
        return json(res, publicJob(v, job));
      }
    }
    const staticFiles = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'};
    if (req.method === 'GET' && staticFiles[pathname]) { const file = staticFiles[pathname], type = file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'; res.writeHead(200, {'Content-Type': `${type}; charset=utf-8`, 'Cache-Control': 'no-store'}); return res.end(fs.readFileSync(path.join(root, 'web', file))); }
    json(res, {error: 'Fixture endpoint not found'}, 404);
  } catch (_) { json(res, {error: 'Isolated fixture request failed'}, 500); }
});
if (require.main === module) {
  server.listen(18765, '127.0.0.1', () => process.stdout.write('Isolated browser fixture listening on 127.0.0.1:18765\n'));
  process.on('SIGTERM', () => { server.closeAllConnections(); server.close(() => process.exit(0)); });
}
module.exports = {server};
