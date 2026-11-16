'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const asArray = (value) => Array.isArray(value) ? value : [];
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const pctValue = (value) => value * 100;
  const percent = (value) => finite(value) && value >= 0 && value <= 1 ? `${pctValue(value).toFixed(1)}%` : '—';
  const number = (value) => finite(value) ? value.toLocaleString('en-US') : '—';
  const runPattern = /^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$/;
  const conversationPattern = /^[a-f0-9]{32}$/;
  let storageKey = 'nhis_active_conversation_en_v2', accessScope = null;
  const mobile = window.matchMedia('(max-width: 768px)');
  const runs = new Map(), conversations = new Map(), dialogOpeners = new WeakMap(), deletedConversationIds = new Set();
  let state = null, activeConversation = null, lastResult = null;
  let busy = false, composing = false, currentJobId = null, generation = 0;
  let pollTimer = null, autoFollow = true, jobEvents = [], processSelection = 'live', publicRecordUrl = null;
  let historyRevision = 0, pendingDeletion = null, deleting = false;
  const isPublic = () => state?.access?.public === true;
  const ownerAllowed = () => state?.access?.public === false;
  const cookiesUnavailable = () => isPublic() && typeof navigator !== 'undefined' && navigator.cookieEnabled === false;
  const cookieMessage = 'Cookies are required to keep your chats separate from other visitors. Enable cookies for this site, then reload the page.';
  const errorText = (error) => error?.name === 'AbortError' ? 'The request timed out. Check your connection and try again.' : error?.message || 'The request could not be completed. Please try again.';

  function publicGuidance(value) {
    const message = String(value ?? '');
    if (!isPublic()) return message;
    return message
      .replace('Select Download official data or run scripts/setup.command in a terminal.', 'The site operator prepares and verifies official data. Please try this analysis again later.')
      .replace('Run scripts/setup.command in a local terminal and check the network connection.', 'The site operator is responsible for preparing the official data. Please try this analysis again later.')
      .replace('The service is not ready. Please check the server configuration.', 'Chat is temporarily unavailable. Please try again later.');
  }

  function retryAfterSeconds(value) {
    if (value == null || value === '') return null;
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric >= 0) return Math.ceil(numeric);
    const date = Date.parse(String(value));
    return Number.isNaN(date) ? null : Math.max(0, Math.ceil((date - Date.now()) / 1000));
  }
  function rateLimitMessage(seconds) {
    if (seconds == null) return 'Too many requests. Please wait before trying again.';
    if (seconds === 0) return 'Too many requests. Please try again shortly.';
    const unit = seconds >= 3600 ? 'hour' : seconds >= 60 ? 'minute' : 'second';
    const amount = Math.ceil(seconds / (unit === 'hour' ? 3600 : unit === 'minute' ? 60 : 1));
    return `Too many requests. Try again in ${amount} ${unit}${amount === 1 ? '' : 's'}.`;
  }

  async function api(path, data, timeout = 30000, method = data === undefined ? 'GET' : 'POST') {
    if (path !== '/api/status' && cookiesUnavailable()) throw new Error(cookieMessage);
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {method, headers: data === undefined ? undefined : {'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data), credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      let result;
      try { result = await response.json(); } catch (_) { throw new Error(`The service did not return readable JSON (HTTP ${response.status}). `); }
      if (!response.ok) {
        const code = result.error_code || result.code || result.error?.code;
        const retry = retryAfterSeconds(response.headers?.get?.('Retry-After') ?? result.retry_after ?? result.retry_after_seconds);
        const message = code === 'cookies_required' ? cookieMessage : response.status === 429 ? rateLimitMessage(retry) : response.status >= 500 ? 'The service could not complete this request. Please try again later.' : typeof result.error === 'string' ? result.error : result.message || `Request failed (HTTP ${response.status}).`;
        const error = new Error(message); error.status = response.status; error.code = code; error.retryAfterSeconds = retry; throw error;
      }
      return result;
    } finally { clearTimeout(timer); }
  }
  function showSystemError(message) { text($('system-error'), message); $('system-error').hidden = !message; }
  function scrollBottom(force = false) {
    if (!force && !autoFollow) return;
    if (force) autoFollow = true;
    requestAnimationFrame(() => { $('chat-scroll').scrollTo({top: $('chat-scroll').scrollHeight, behavior: 'auto'}); $('jump-to-bottom').hidden = true; });
  }
  function autoGrow() {
    $('question').style.height = 'auto';
    $('question').style.height = `${Math.min(200, Math.max(35, $('question').scrollHeight))}px`;
    document.documentElement.style.setProperty('--composer-height', `${Math.ceil($('composer-dock').getBoundingClientRect().height)}px`);
    $('ask-button').disabled = busy || !$('question').value.trim();
  }
  function fillQuestion(value) { $('question').value = value; autoGrow(); $('question').focus(); }
  function closeSidebar() {
    document.body.classList.remove('sidebar-open'); $('sidebar-backdrop').hidden = true;
    $('app-main').inert = false;
    $('sidebar').inert = mobile.matches || document.body.classList.contains('sidebar-collapsed');
    $('sidebar-toggle').setAttribute('aria-expanded', String(!mobile.matches && !document.body.classList.contains('sidebar-collapsed')));
  }
  function toggleSidebar() {
    if (mobile.matches) {
      const opening = !document.body.classList.contains('sidebar-open');
      if (!opening) { closeSidebar(); $('sidebar-toggle').focus(); return; }
      document.body.classList.add('sidebar-open'); $('sidebar').inert = false; $('app-main').inert = true;
      $('sidebar-backdrop').hidden = false; $('sidebar-toggle').setAttribute('aria-expanded', 'true'); $('sidebar-close').focus();
    } else {
      document.body.classList.toggle('sidebar-collapsed'); closeSidebar();
    }
  }
  function closeDialog(dialog) { if (dialog?.open && !(dialog.id === 'delete-dialog' && deleting)) dialog.close(); }
  function openDialog(id) {
    const opener = document.activeElement;
    closeSidebar();
    document.querySelectorAll('dialog[open]').forEach((dialog) => { if (dialog.id !== id) closeDialog(dialog); });
    if (!$(id).open) { dialogOpeners.set($(id), opener); $(id).showModal(); }
  }
  function openView(name) { if (['methods', 'data', 'history'].includes(name)) { openDialog(`${name}-dialog`); if (name === 'history') loadHistory(); if (name === 'data') refreshStatus(); } }
  function saveActive(id) { try { if (conversationPattern.test(id || '')) sessionStorage.setItem(storageKey, id); else sessionStorage.removeItem(storageKey); } catch (_) { /* Storage is optional. */ } }
  async function requireSession() {
    if (!state) await refreshStatus();
    if (!state) throw new Error('Unable to confirm the service status. Refresh the page and try again.');
    if (cookiesUnavailable()) throw new Error(cookieMessage);
  }
  function applyAccess(status) {
    const scope = String(status.access?.scope || (status.access?.public === true ? 'public' : 'private'));
    const changed = accessScope !== null && accessScope !== scope;
    if (changed) {
      ++generation; ++historyRevision; saveActive(null); conversations.clear(); runs.clear(); deletedConversationIds.clear();
      clearConversationView(); $('history-list').replaceChildren(); $('results').hidden = true; $('process-timeline').replaceChildren();
      for (const id of ['result-context', 'result-answer', 'result-messages', 'results-body', 'ci-chart', 'derived-results', 'comparison-body', 'gradient-comparison', 'artifact-links', 'survey-note', 'measurement-note', 'run-id']) $(id).replaceChildren();
      $('conversation-search').value = ''; $('load-run-id').value = ''; $('load-run-button').disabled = false; text($('history-error'), ''); text($('delete-description'), '');
      document.title = 'NHIS Lab · Chat and research'; showSystemError('');
      if (publicRecordUrl) { URL.revokeObjectURL(publicRecordUrl); publicRecordUrl = null; }
      pendingDeletion = null; deleting = false; $('delete-confirm').disabled = false; $('delete-cancel').disabled = false;
      document.querySelectorAll('dialog[open]').forEach((dialog) => { if (dialog.id !== 'data-dialog') closeDialog(dialog); });
      setBusy(false);
    }
    accessScope = scope;
    storageKey = isPublic() ? `nhis_active_conversation_en_v2_${scope}` : 'nhis_active_conversation_en_v2';
    const owner = ownerAllowed();
    $('measure-model').disabled = busy || !owner;
    if (!owner) $('measure-model').checked = false;
    $('measure-option').title = owner ? 'Optional extra calls using group definitions only, without survey estimates or chat history' : 'Independent model benchmarks are managed by the site operator.';
    $('public-access-note').hidden = !isPublic();
    text($('history-intro'), isPublic() ? 'Only analyses from this browser’s session appear here. Opening a record does not change the active chat or its research scope.' : 'Open saved analyses. Viewing a record does not change the active chat or its research scope.');
    if (changed) { renderConversationHistory(); renderHistory(); }
  }

  function safeMarkdownUrl(value) {
    const candidate = String(value).trim();
    if (!candidate || /[\s\u0000-\u001f\u007f\\]/.test(candidate)) return null;
    const local = candidate.match(/^\/api\/runs\/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{10})(?:\/(?:report|csv))?$/);
    if (local && runPattern.test(local[1])) return candidate;
    if (!/^https:\/\//i.test(candidate)) return null;
    try {
      const url = new URL(candidate);
      return url.protocol === 'https:' && url.hostname && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }
  function markdownLinkAt(source, start) {
    if (source[start] !== '[') return null;
    let cursor = start + 1, brackets = 1;
    for (; cursor < source.length; cursor++) {
      if (source[cursor] === '\\') { cursor++; continue; }
      if (source[cursor] === '[') brackets++;
      if (source[cursor] === ']' && --brackets === 0) break;
    }
    if (cursor >= source.length || source[cursor + 1] !== '(') return null;
    const labelEnd = cursor, destinationStart = cursor + 2;
    let parentheses = 1; cursor = destinationStart;
    for (; cursor < source.length; cursor++) {
      if (source[cursor] === '\\') { cursor++; continue; }
      if (source[cursor] === '(') parentheses++;
      else if (source[cursor] === ')' && --parentheses === 0) break;
    }
    if (cursor >= source.length) return null;
    return {label: source.slice(start + 1, labelEnd), destination: source.slice(destinationStart, cursor), end: cursor + 1};
  }
  function inlineText(node, value, depth = 0, allowLinks = true) {
    const source = String(value);
    if (depth > 6) { node.append(document.createTextNode(source)); return; }
    let cursor = 0, plain = '';
    const flush = () => { if (plain) { node.append(document.createTextNode(plain)); plain = ''; } };
    const closing = (token, from) => {
      let at = source.indexOf(token, from);
      while (at >= 0) { let slashes = 0; for (let p = at - 1; p >= 0 && source[p] === '\\'; p--) slashes++; if (slashes % 2 === 0) return at; at = source.indexOf(token, at + token.length); }
      return -1;
    };
    while (cursor < source.length) {
      if (source[cursor] === '\\' && /[\\`*\[\]()|#]/.test(source[cursor + 1] || '')) { plain += source[cursor + 1]; cursor += 2; continue; }
      if (source[cursor] === '`') {
        let count = 1; while (source[cursor + count] === '`') count++;
        const token = '`'.repeat(count), end = source.indexOf(token, cursor + count);
        if (end > cursor + count) { flush(); const code = document.createElement('code'); text(code, source.slice(cursor + count, end)); node.append(code); cursor = end + count; continue; }
      }
      if (source.startsWith('**', cursor)) {
        const end = closing('**', cursor + 2);
        if (end > cursor + 2) { flush(); const strong = document.createElement('strong'); inlineText(strong, source.slice(cursor + 2, end), depth + 1, allowLinks); node.append(strong); cursor = end + 2; continue; }
      }
      if (source[cursor] === '!' && source[cursor + 1] === '[') {
        const image = markdownLinkAt(source, cursor + 1);
        if (image) { plain += source.slice(cursor, image.end); cursor = image.end; continue; }
      }
      if (allowLinks && source[cursor] === '[') {
        const link = markdownLinkAt(source, cursor);
        if (link) {
          const destination = safeMarkdownUrl(link.destination);
          if (destination) { flush(); const anchor = document.createElement('a'); anchor.href = destination; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; inlineText(anchor, link.label, depth + 1, false); node.append(anchor); }
          else plain += source.slice(cursor, link.end);
          cursor = link.end; continue;
        }
      }
      plain += source[cursor++];
    }
    flush();
  }
  function markdownTableRow(value) {
    const source = value.trim(), cells = []; let cell = '', fence = 0, separators = 0;
    for (let cursor = 0; cursor < source.length; cursor++) {
      const char = source[cursor];
      if (char === '\\' && cursor + 1 < source.length) { cell += char + source[++cursor]; continue; }
      if (char === '`') { let count = 1; while (source[cursor + count] === '`') count++; if (!fence) fence = count; else if (fence === count) fence = 0; cell += '`'.repeat(count); cursor += count - 1; continue; }
      if (char === '|' && !fence) { cells.push(cell.trim()); cell = ''; separators++; } else cell += char;
    }
    cells.push(cell.trim());
    if (source.startsWith('|')) cells.shift();
    if (source.endsWith('|') && cells[cells.length - 1] === '') cells.pop();
    return {cells, separators};
  }
  function richText(node, value) {
    const lines = String(value).replace(/\r\n?/g, '\n').split('\n'); let paragraph = [], lists = [];
    const flush = () => { if (paragraph.length) { const p = document.createElement('p'); inlineText(p, paragraph.join('\n')); node.append(p); paragraph = []; } };
    const reset = () => { flush(); lists = []; };
    for (let cursor = 0; cursor < lines.length; cursor++) {
      const line = lines[cursor], fence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
      if (fence) {
        reset(); const content = [], marker = fence[1][0], minimum = fence[1].length; cursor++;
        for (; cursor < lines.length; cursor++) { const close = lines[cursor].trim(); if (close.length >= minimum && [...close].every((char) => char === marker)) break; content.push(lines[cursor]); }
        const pre = document.createElement('pre'), code = document.createElement('code'); text(code, content.join('\n')); pre.append(code); node.append(pre); continue;
      }
      const header = markdownTableRow(line), separator = cursor + 1 < lines.length ? markdownTableRow(lines[cursor + 1]) : null;
      if (header.separators && header.cells.length && separator && separator.cells.length === header.cells.length && separator.cells.every((cell) => /^:?-{3,}:?$/.test(cell))) {
        reset();
        const wrapper = document.createElement('div'); wrapper.className = 'message-table-wrap'; wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Data table in this reply'); wrapper.setAttribute('tabindex', '0');
        const table = document.createElement('table'), head = document.createElement('thead'), headRow = document.createElement('tr'), body = document.createElement('tbody');
        const alignment = separator.cells.map((cell) => cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : 'left');
        header.cells.forEach((cell, index) => { const th = document.createElement('th'); th.scope = 'col'; th.style.textAlign = alignment[index]; inlineText(th, cell); headRow.append(th); }); head.append(headRow); table.append(head);
        cursor += 2;
        for (; cursor < lines.length; cursor++) {
          const row = markdownTableRow(lines[cursor]); if (!row.separators || row.cells.length !== header.cells.length || !lines[cursor].trim()) break;
          const tr = document.createElement('tr'); row.cells.forEach((cell, index) => { const td = document.createElement('td'); td.style.textAlign = alignment[index]; inlineText(td, cell); tr.append(td); }); body.append(tr);
        }
        cursor--; table.append(body); wrapper.append(table); node.append(wrapper); continue;
      }
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/), bullet = line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
      if (heading) { reset(); const h = document.createElement(heading[1].length <= 2 ? 'h3' : heading[1].length <= 4 ? 'h4' : 'h5'); inlineText(h, heading[2].replace(/\s+#+\s*$/, '')); node.append(h); }
      else if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { reset(); node.append(document.createElement('hr')); }
      else if (bullet) {
        flush(); const indentation = bullet[1].replace(/\t/g, '    ').length, kind = /^\d/.test(bullet[2]) ? 'ol' : 'ul';
        while (lists.length && (lists[lists.length - 1].indent > indentation || (lists[lists.length - 1].indent === indentation && lists[lists.length - 1].kind !== kind))) lists.pop();
        let level = lists[lists.length - 1];
        if (!level || level.indent < indentation) {
          const list = document.createElement(kind); if (kind === 'ol') { const start = Number.parseInt(bullet[2], 10); if (start > 0 && start <= 1000000) list.start = start; }
          if (level?.lastItem) level.lastItem.append(list); else node.append(list);
          level = {indent: indentation, kind, list, lastItem: null}; lists.push(level);
        }
        const li = document.createElement('li'); inlineText(li, bullet[3]); level.list.append(li); level.lastItem = li;
      }
      else if (!line.trim()) reset();
      else if (lists.length && /^\s{2,}\S/.test(line)) { const li = lists[lists.length - 1].lastItem; li.append(document.createTextNode('\n')); inlineText(li, line.trim()); }
      else { lists = []; paragraph.push(line); }
    }
    flush();
  }

  async function copyReply(value, button) {
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) { try { await navigator.clipboard.writeText(value); copied = true; } catch (_) { /* Try the local browser fallback. */ } }
      if (!copied) {
        const area = document.createElement('textarea'); area.value = value; area.style.position = 'fixed'; area.style.opacity = '0'; document.body.append(area); area.select();
        copied = document.execCommand('copy'); area.remove(); button.focus({preventScroll: true});
        if (!copied) throw new Error('This browser does not support copying.');
      }
      text(button, 'Copied'); text($('copy-status'), 'Reply copied'); setTimeout(() => { text(button, 'Copy'); }, 1600);
    } catch (_) { text(button, 'Copy failed'); text($('copy-status'), 'Copy failed. Select the reply text to copy it manually.'); }
  }

  function generationLabel(record) {
    return record?.status === 'ok' ? 'Model response' : 'Local explanation';
  }
  function renderServiceStatus(dialogue = {}) {
    const connected = dialogue.connection?.status === 'ok';
    text($('service-status'), connected ? 'Service is ready' : dialogue.configured ? 'Service status not yet confirmed' : 'Service is not ready');
    text($('service-detail'), connected ? isPublic() ? 'Chat responses are managed by the site operator.' : 'Chat responses and independent benchmarks are managed by the server.' : dialogue.configured ? isPublic() ? 'Chat availability is checked when you send a message.' : 'Connection status will update after an actual reply.' : isPublic() ? 'Chat is temporarily unavailable. Please try again later.' : 'The service is not ready. Please check the server configuration.');
  }

  function displayMessage(value) {
    // Normalize obsolete setup guidance in saved replies without translating user text.
    const message = String(value).replace(/请先在模型设置中保存并测试连接[。.]/g, 'The service is not ready. Please check the server configuration.')
      .replace(/尚未连接真实 API，请在模型设置中填写模型名称和密钥[。.]/g, 'The service is not ready. Please check the server configuration.')
      .replace(/请在模型连接设置的密码输入框填写密钥，不要把密钥作为研究问题发送[。.]/g, 'Check the server configuration. Do not send credentials in chat.');
    return publicGuidance(message);
  }

  const categoryLabels = {
    overall: 'All adults', less_than_high_school: 'Less than high school', high_school: 'High school or GED',
    some_college: 'Some college or associate degree', bachelors_plus: 'Bachelor’s degree or higher',
    female: 'Female', male: 'Male', hispanic: 'Hispanic, any race', nh_white: 'Non-Hispanic White only',
    nh_black: 'Non-Hispanic Black only', nh_asian: 'Non-Hispanic Asian only', nh_other: 'Non-Hispanic other or multiple race',
    '25_34': '25–34 years', '35_44': '35–44 years', '45_54': '45–54 years', '55_64': '55–64 years',
    below_100: 'Below 100% of the poverty threshold', '100_199': '100% to <200% of the poverty threshold',
    '200_399': '200% to <400% of the poverty threshold', '400_plus': '400%+ of the poverty threshold',
  };
  function categoryName(id) {
    if (categoryLabels[id]) return categoryLabels[id];
    const parts = String(id || '').split(/\||__/);
    return parts.length === 2 && parts.every((part) => categoryLabels[part]) ? parts.map((part) => categoryLabels[part]).join(' × ') : null;
  }
  function rowLabel(row, fallback = 'Unnamed group') {
    const target = categoryName(row.group_id), reference = categoryName(row.reference_id);
    if (target && reference) return `${target} − ${reference}`;
    return target || row.label || row.group_id || fallback;
  }
  function processLabel(event) {
    const labels = {understand: 'Understand the request', scope: 'Confirm research scope', data: 'Check real data',
      statistics: 'Calculate survey estimates', validation: 'Check result reliability', model: 'Independent model benchmark',
      report: 'Save evidence and report', reply: 'Prepare reply', explain: 'Explain existing results',
      analysis: 'Run the analysis', reply_api: 'Generate reply', answer: 'Prepare the answer'};
    const label = String(event.label || '');
    return (!label || /\p{Script=Han}/u.test(label)) ? labels[event.stage] || 'Execution step' : label;
  }
  function appendMessage(role, value, status, metadata = {}) {
    if (role === 'assistant') value = displayMessage(value);
    const article = document.createElement('article'); article.className = `message ${role}`;
    if (metadata.id) article.dataset.messageId = metadata.id;
    if (role === 'assistant') { const label = document.createElement('div'); label.className = 'message-label'; text(label, 'NHIS Lab'); const source = document.createElement('span'); source.className = 'message-generation'; text(source, generationLabel(metadata.generation)); label.append(source); article.append(label); }
    const body = document.createElement('div'); body.className = 'message-body';
    if (role === 'user') text(body, value); else richText(body, value); article.append(body);
    if (role === 'assistant') {
      const actions = document.createElement('div'); actions.className = 'message-actions';
      const copy = document.createElement('button'); copy.type = 'button'; text(copy, 'Copy'); copy.setAttribute('aria-label', 'Copy this reply'); copy.addEventListener('click', () => copyReply(value, copy)); actions.append(copy);
      if (runPattern.test(metadata.run_id || '')) { const evidence = document.createElement('button'); evidence.type = 'button'; text(evidence, 'Research evidence ↗'); evidence.addEventListener('click', () => openRun(metadata.run_id)); actions.append(evidence); }
      const events = processEvents(metadata.process);
      if (events.length) { const process = document.createElement('button'); process.type = 'button'; text(process, 'Calculation steps'); process.addEventListener('click', () => showProcess(events, false)); actions.append(process); }
      article.append(actions);
      const suggestions = asArray(metadata.suggestions).slice(0, 3);
      if (suggestions.length) {
        const chips = document.createElement('div'); chips.className = 'chat-suggestions';
        for (const suggestion of suggestions) {
          const question = typeof suggestion === 'string' ? suggestion : suggestion?.message || suggestion?.question || suggestion?.text;
          if (!question) continue;
          const button = document.createElement('button'); button.type = 'button'; text(button, typeof suggestion === 'string' ? suggestion : suggestion.label || question); button.title = 'Fill the composer, review, then send';
          button.addEventListener('click', () => { if (!busy) fillQuestion(question); }); chips.append(button);
        }
        article.append(chips);
      }
    }
    $('conversation').append(article); $('conversation-details').hidden = false; $('welcome').hidden = true; scrollBottom();
  }
  function renderConversation(conversation, forceScroll = false) {
    if (deletedConversationIds.has(conversation.id)) return;
    const previousTop = $('chat-scroll').scrollTop;
    activeConversation = conversation;
    if (conversationPattern.test(conversation.id || '')) { conversations.set(conversation.id, {...conversations.get(conversation.id), ...conversation}); saveActive(conversation.id); }
    const seen = new Set(), messages = asArray(conversation.messages); $('conversation').replaceChildren();
    for (const message of messages) { if (message.id && seen.has(message.id)) continue; if (message.id) seen.add(message.id); if (['user', 'assistant'].includes(message.role)) appendMessage(message.role, message.content || '', message.status, message); }
    $('conversation-details').hidden = !messages.length; $('welcome').hidden = !!messages.length;
    text($('conversation-heading'), conversation.title || 'New chat'); document.title = `${conversation.title || 'Chat and research'} · NHIS Lab`;
    $('question').placeholder = messages.length ? 'Ask a follow-up…' : 'Message NHIS Lab';
    renderConversationHistory();
    if (forceScroll || autoFollow) scrollBottom(forceScroll); else requestAnimationFrame(() => { $('chat-scroll').scrollTop = previousTop; });
  }
  function setBusy(value, showActivity = true) {
    busy = value; $('new-analysis').disabled = value; $('ask-button').disabled = value || !$('question').value.trim();
    $('ask-button').setAttribute('aria-label', value ? 'Working on your message. Please wait for the reply.' : 'Send message');
    $('activity').hidden = !value || !showActivity; $('activity-spinner').hidden = !value;
    document.querySelectorAll('[data-question], .chat-suggestions button, [data-delete-conversation], .sidebar-conversation').forEach((button) => { button.disabled = value; });
    $('clear-conversations').disabled = value || !conversations.size;
    $('measure-model').disabled = value || !ownerAllowed();
    scrollBottom();
  }
  function clearConversationView() {
    activeConversation = null; lastResult = null; currentJobId = null; jobEvents = []; processSelection = 'live';
    $('conversation').replaceChildren(); $('conversation-details').hidden = true; $('welcome').hidden = false;
    text($('conversation-heading'), 'New chat'); $('last-evidence').hidden = true; $('activity-process').hidden = true;
    $('activity').hidden = true; $('question').value = ''; $('question').placeholder = 'Message NHIS Lab'; autoFollow = true; autoGrow();
  }
  async function resetWorkspace() {
    if (busy) return;
    const version = ++generation; setBusy(true); closeSidebar(); showSystemError('');
    try {
      await requireSession(); if (version !== generation) return;
      const response = await api('/api/conversations', {}, 20000); if (version !== generation) return;
      const conversation = response.conversation || response; if (!conversationPattern.test(conversation.id || '')) throw new Error('Unable to create a new chat.');
      clearConversationView(); renderConversation(conversation, true); void loadHistory(); $('question').focus();
    } catch (error) { if (version === generation) showSystemError(errorText(error)); }
    finally { if (version === generation) setBusy(false); }
  }
  async function acceptJob(job, version, conversationId) {
    if (version !== generation || activeConversation?.id !== conversationId) return;
    const response = job.response || {}, conversation = response.conversation;
    if (conversation?.id === conversationId) renderConversation(conversation);
    else if (response.message) renderConversation({...activeConversation, messages: [...asArray(activeConversation.messages), response.message]});
    if (response.result?.run_id) {
      lastResult = response.result; $('last-evidence').hidden = false;
      const question = [...asArray(activeConversation?.messages)].reverse().find((message) => message.role === 'user')?.content;
      recordRun(response.result, question);
    }
    if (job.events) renderProcess(job.events, true);
    currentJobId = null; void Promise.allSettled([refreshStatus(), loadHistory()]); scrollBottom();
  }
  async function pollJob(id, version, conversationId) {
    if (!conversationPattern.test(id || '')) throw new Error('The service did not return a valid job ID.');
    currentJobId = id;
    while (currentJobId === id && version === generation && activeConversation?.id === conversationId) {
      const job = await api(`/api/jobs/${id}`, undefined, 25000);
      if (currentJobId !== id || version !== generation || activeConversation?.id !== conversationId) return;
      if (job.events) renderProcess(job.events, true);
      if (job.status === 'complete') { await acceptJob(job, version, conversationId); return; }
      if (job.status === 'failed') throw new Error(typeof job.error === 'string' ? job.error : 'This turn did not finish. No automatic retry was made.');
      if (job.status !== 'running') throw new Error('The job status is unknown. Restore the chat before deciding whether to retry.');
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
  }
  async function ask(event) {
    event.preventDefault(); if (busy || composing) return;
    const question = $('question').value.trim(); if (!question) return;
    const version = generation; setBusy(true); showSystemError(''); jobEvents = []; processSelection = 'live'; $('activity-process').hidden = true;
    text($('activity-title'), 'Sending message'); text($('activity-detail'), '');
    try {
      await requireSession(); if (version !== generation) return;
      if (!activeConversation?.id) {
        const response = await api('/api/conversations', {}, 20000); if (version !== generation) return;
        const conversation = response.conversation || response; if (!conversationPattern.test(conversation.id || '')) throw new Error('Unable to start the chat.'); renderConversation(conversation);
      }
      const conversationId = activeConversation.id;
      appendMessage('user', question); scrollBottom(true);
      const response = await api(`/api/conversations/${conversationId}/messages`, {message: question, measure_model: ownerAllowed() && $('measure-model').checked, repeats: 5, prompt_versions: 3});
      if (version !== generation || activeConversation?.id !== conversationId) return;
      $('question').value = ''; autoGrow();
      if (response.job_id) await pollJob(response.job_id, version, conversationId);
      else await acceptJob({response, events: processEvents(response.message?.process)}, version, conversationId);
    } catch (error) {
      if (version !== generation) return; currentJobId = null;
      appendMessage('assistant', `${errorText(error)}\n\nNo message or model call was automatically repeated. Restore this chat to check its state before retrying.`, 'error');
      if (activeConversation?.id) { const actions = document.createElement('div'); actions.className = 'message-actions'; const recover = document.createElement('button'); recover.type = 'button'; text(recover, 'Restore chat without resending'); recover.addEventListener('click', () => openConversation(activeConversation.id)); actions.append(recover); $('conversation').lastElementChild.append(actions); }
      fillQuestion(question);
    } finally { if (version === generation) { setBusy(false); $('question').focus(); } }
  }
  async function openConversation(id, restoring = false) {
    if (!conversationPattern.test(id || '') || deletedConversationIds.has(id)) return;
    if (busy) { if (!restoring) showSystemError('A reply is still being processed. You can switch chats when it finishes.'); return; }
    const version = ++generation; setBusy(true); closeSidebar(); showSystemError('');
    text($('activity-title'), 'Restoring chat'); text($('activity-detail'), 'Loading saved messages without resending them.');
    try {
      const response = await api(`/api/conversations/${id}`, undefined, 20000); if (version !== generation || deletedConversationIds.has(id)) return;
      const conversation = response.conversation || response; if (conversation.id !== id) throw new Error('The saved chat format is invalid.');
      clearConversationView(); renderConversation(conversation, true);
      if (conversation.latest_result?.run_id) { lastResult = conversation.latest_result; $('last-evidence').hidden = false; }
      const activeJob = conversation.active_job_id || conversation.active_job?.id;
      if (activeJob) { setBusy(true); text($('activity-title'), 'Restoring the current job'); await pollJob(activeJob, version, id); }
    } catch (error) { if (version === generation) { currentJobId = null; showSystemError(`${errorText(error)} No messages were resent.`); if (!activeConversation) saveActive(null); } }
    finally { if (version === generation) setBusy(false); }
  }
  function processEvents(value) {
    return Array.isArray(value) ? value : asArray(value?.events);
  }

  function safeFacts(value) {
    if (Array.isArray(value)) return value.map(safeFacts);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([key]) => !/key|token|secret|password|authorization|credential/i.test(key) && !/^(?:provider|base_url|api_url|endpoint|model|model_name|model_id|returned_model|response_model|configured_model|response_id|request_id|usage|api_status|generation|dialogue|connection|headers|calls|raw_response)$/i.test(key)).map(([key, item]) => [key, safeFacts(item)]));
    return value;
  }

  function humanProcessFacts(facts) {
    const find = (value, key) => {
      if (!value || typeof value !== 'object') return undefined;
      if (Object.hasOwn(value, key)) return value[key];
      for (const nested of Object.values(value)) { if (nested && typeof nested === 'object') { const found = find(nested, key); if (found !== undefined) return found; } }
      return undefined;
    };
    const labels = {
      full_sample_n: ['Full adult sample', 'people'], age_domain_n: ['Adults aged 25–64', 'people'],
      filtered_domain_n: ['Filtered subpopulation', 'people'], valid_outcome_n: ['Valid-outcome denominator', 'people'],
      released_groups: ['Released groups', 'groups'], suppressed_groups: ['Suppressed groups', 'groups'],
      planned_calls: ['Planned benchmark calls', 'calls'], valid_calls: ['Valid benchmark calls', 'calls'],
    };
    const lines = [];
    for (const [key, [label, unit]] of Object.entries(labels)) {
      const value = find(facts, key);
      if (finite(value) && value >= 0) lines.push(`${label}: ${number(value)} ${unit}`);
    }
    const cacheHit = find(facts, 'cache_hit');
    if (typeof cacheHit === 'boolean') lines.push(`Statistics cache: ${cacheHit ? 'Hit' : 'Miss'}`);
    const sourceRunId = find(facts, 'source_run_id');
    if (typeof sourceRunId === 'string' && runPattern.test(sourceRunId)) lines.push(`Source run: ${sourceRunId}`);
    const elapsed = find(facts, 'elapsed_ms');
    if (finite(elapsed) && elapsed >= 0) lines.push(`Observed duration: ${elapsed < 1000 ? `${elapsed.toFixed(0)} ms` : `${(elapsed / 1000).toFixed(2)} s`}`);
    return lines;
  }

  function renderProcess(events, progress = false) {
    const actualEvents = asArray(events);
    if (progress) {
      jobEvents = actualEvents;
      $('activity-process').hidden = !jobEvents.length;
      const latest = jobEvents[jobEvents.length - 1];
      if (latest && busy) { text($('activity-title'), processLabel(latest)); text($('activity-detail'), typeof latest.detail === 'string' ? publicGuidance(latest.detail) : ''); scrollBottom(); }
      if (processSelection !== 'live') return;
    }
    const timeline = $('process-timeline'); timeline.replaceChildren(); $('process-details').hidden = !actualEvents.length;
    const stages = new Map();
    actualEvents.forEach((event, index) => { const key = event.stage || event.label || `event-${index}`, prior = stages.get(key); stages.set(key, {event, records: [...(prior?.records || []), event], facts: {...(prior?.facts || {}), ...safeFacts(event.facts || {})}}); });
    for (const {event, records, facts} of stages.values()) {
      const item = document.createElement('li'), heading = document.createElement('strong');
      const status = {running: 'In progress', complete: 'Complete', completed: 'Complete', passed: 'Passed', failed: 'Incomplete', blocked: 'Needs attention', skipped: 'Not run'}[event.status];
      text(heading, [processLabel(event), status].filter(Boolean).join(' · ')); item.append(heading);
      if (event.timestamp) { const date = new Date(event.timestamp), time = document.createElement('time'); text(time, Number.isNaN(date.getTime()) ? event.timestamp : date.toLocaleTimeString('en-US', {hour12: false})); item.append(time); }
      if (typeof event.detail === 'string' && event.detail) { const p = document.createElement('p'); text(p, publicGuidance(event.detail)); item.append(p); }
      const humanFacts = humanProcessFacts(facts); if (humanFacts.length) { const p = document.createElement('p'); text(p, humanFacts.join('; ')); item.append(p); }
      if (records.some((record) => record.facts && Object.keys(record.facts).length)) { const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre'); text(summary, 'Technical record'); text(pre, JSON.stringify(records.map(safeFacts), null, 2)); details.append(summary, pre); item.append(details); }
      timeline.append(item);
    }
  }
  function showProcess(events, live = false) { processSelection = live ? 'live' : 'message'; renderProcess(events); openDialog('process-dialog'); }

  function addTag(container, value) {
    if (value == null || value === '') return;
    const tag = document.createElement('span'); tag.className = 'context-tag'; text(tag, value); container.append(tag);
  }

  function renderResults(result) {
    $('results').hidden = false;
    text($('result-answer'), publicGuidance(result.answer || 'See the status and available output for this analysis.'));
    const sourceSurvey = result.survey || {};
    const canPublish = result.status === 'ok' && sourceSurvey.status === 'ok';
    const survey = canPublish ? sourceSurvey : {rows: [], message: sourceSurvey.message || sourceSurvey.reason || '', regression: sourceSurvey.regression ? {status: 'blocked', reason: sourceSurvey.regression.reason || 'This analysis did not pass the release checks'} : undefined};
    const rows = asArray(survey.rows);
    const hasResults = rows.length > 0;
    const regression = survey.regression || {};
    const hasDerived = (regression.status === 'ok' && asArray(regression.coefficients).length > 0) || asArray(survey.gradient?.contrasts).length > 0;
    text($('result-badge'), result.status === 'ok' ? 'Analysis complete' : result.status === 'clarify' ? 'Clarification needed' : 'Calculation blocked');
    $('result-badge').className = `pill ${result.status === 'ok' ? 'ready' : 'error'}`;
    $('results-empty').hidden = true;
    $('results-content').hidden = false;
    const context = $('result-context'); context.replaceChildren();
    const spec = result.spec || {};
    addTag(context, spec.population || spec.age_range || 'Adults aged 25–64');
    addTag(context, spec.outcome === 'fair_poor' ? 'Fair or poor self-rated health' : spec.outcome || spec.indicator || 'Fair or poor self-rated health');
    const groupLabels = {overall: 'All adults', education: 'Education', race: 'Race and Hispanic ethnicity', sex: 'Sex', age: 'Age group', education_race: 'Education × race and Hispanic ethnicity', income: 'Income-to-poverty category'};
    const stratification = spec.stratify_by || spec.stratification || spec.grouping || groupLabels[spec.group_by];
    if (spec.analysis) addTag(context, {proportion: 'Group proportions', gradient: 'Group gradients · risk differences', regression: 'Adjusted regression · OR'}[spec.analysis] || spec.analysis);
    addTag(context, Array.isArray(stratification) ? stratification.join(' / ') : stratification);
    for (const [key, values] of Object.entries(spec.filters || {})) {
      const labels = (Array.isArray(values) ? values : [values]).map((value) => categoryName(value) || asArray(state?.registry?.groups?.[key]?.categories).find((category) => category.id === value)?.label || value);
      addTag(context, `${groupLabels[key] || key}: ${labels.join(' / ')}`);
    }
    if (result.survey?.year) addTag(context, `NHIS ${result.survey.year}`);
    $('result-messages').replaceChildren();
    const messages = [...asArray(result.messages), ...asArray(result.survey?.warnings)];
    if (!hasResults && !hasDerived) messages.unshift('No survey estimates were generated. Follow the reply to prepare data, clarify the question, or revise the research scope.');
    for (const message of messages) {
      const p = document.createElement('p'); text(p, publicGuidance(typeof message === 'string' ? message : message.message || JSON.stringify(message))); $('result-messages').append(p);
    }
    const body = $('results-body'); body.replaceChildren();
    for (const row of rows) {
      const tr = document.createElement('tr');
      const published = row.status === 'ok';
      const values = [rowLabel(row), number(row.n), published ? percent(row.estimate) : '—', published ? percent(row.se) : '—', published && finite(row.ci_low) && finite(row.ci_high) ? `${percent(row.ci_low)} – ${percent(row.ci_high)}` : '—'];
      for (const value of values) { const td = document.createElement('td'); text(td, value); tr.append(td); }
      const td = document.createElement('td'); const badge = document.createElement('span');
      const ok = ['ok', 'estimated', 'success', 'ready'].includes(row.status) || (!row.status && finite(row.estimate));
      badge.className = `table-status${ok ? '' : ' warning'}`; text(badge, ok ? 'Estimated' : row.status || 'Not estimable'); td.append(badge); tr.append(td); body.append(tr);
    }
    $('numbers-details').hidden = !hasResults;
    $('results-body').closest('.table-wrap').hidden = !hasResults;
    $('ci-chart').previousElementSibling.hidden = !hasResults;
    renderChart(rows, canPublish ? asArray(result.comparison) : []);
    const note = survey.method || survey.methodology || survey.methods?.proportion_ci || survey.message || '';
    text($('survey-note'), (typeof note === 'string' ? note : JSON.stringify(note)) + (hasResults ? '  n is the analysis sample size. Proportions and standard errors are survey-weighted. Group differences are descriptive associations, not causal effects.' : ''));
    renderDerived(survey);
    renderMeasurement(result);
    text($('run-id'), result.run_id ? `RUN / ${result.run_id}` : '');
    $('artifact-links').replaceChildren();
    for (const [key, label] of [['report', 'Analysis report'], ['csv', 'Results CSV'], ['json', 'Statistical record']]) {
      const url = result.artifacts?.[key];
      if (typeof url !== 'string' || !/^\/api\/runs\/[A-Za-z0-9_-]+(?:\/(?:report|csv))?$/.test(url)) continue;
      const a = document.createElement('a');
      if (key === 'json') { if (publicRecordUrl) URL.revokeObjectURL(publicRecordUrl); publicRecordUrl = URL.createObjectURL(new Blob([JSON.stringify(safeFacts(result), null, 2)], {type: 'application/json'})); a.href = publicRecordUrl; a.download = `${result.run_id || 'analysis'}.json`; }
      else { a.href = url; a.target = '_blank'; a.rel = 'noopener'; }
      text(a, label); $('artifact-links').append(a);
    }
  }

  function renderDerived(survey) {
    const host = $('derived-results'); host.replaceChildren();
    const scalar = (value) => finite(value) ? value.toFixed(3) : '—';
    const pp = (value) => finite(value) ? `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}` : '—';
    const sePP = (value) => finite(value) ? (value * 100).toFixed(2) : '—';
    const addSection = (title, note, headers, rows) => {
      const section = document.createElement('section'); section.className = 'measurement-section';
      const heading = document.createElement('div'); heading.className = 'chart-header';
      const div = document.createElement('div'); const h = document.createElement('h3'); text(h, title); div.append(h); heading.append(div); section.append(heading);
      const p = document.createElement('p'); p.className = 'footnote'; text(p, note); section.append(p);
      if (rows.length) {
        const wrap = document.createElement('div'); wrap.className = 'table-wrap';
        const table = document.createElement('table'), thead = document.createElement('thead'), tr = document.createElement('tr'), tbody = document.createElement('tbody');
        for (const label of headers) { const th = document.createElement('th'); text(th, label); tr.append(th); }
        thead.append(tr); table.append(thead);
        for (const values of rows) { const row = document.createElement('tr'); for (const value of values) { const td = document.createElement('td'); text(td, value); row.append(td); } tbody.append(row); }
        table.append(tbody); wrap.append(table); section.append(wrap);
      }
      host.append(section);
      return section;
    };
    const gradient = survey.gradient;
    if (gradient) {
      const rows = asArray(gradient.contrasts).map((row) => {
        const ok = row.status === 'ok';
        return [rowLabel(row, 'Unnamed contrast'), ok ? pp(row.estimate) : '—', ok ? sePP(row.se) : '—', ok ? `${pp(row.ci_low)} – ${pp(row.ci_high)}` : '—', ok ? 'Passed' : row.suppression_reason || row.status || 'Not estimated'];
      });
      addSection(gradient.reference === '400_plus' ? 'Income gradient: risk differences relative to 400%+ of the poverty threshold' : 'Education gradient: risk differences relative to a bachelor’s degree or higher', 'Values are in percentage points. Confidence intervals use joint survey covariance; income analyses also include imputation variance. Unadjusted differences are not causal effects.', ['Group contrast', 'Risk difference (pp)', 'SE (pp)', '95% confidence interval', 'Status'], rows);
    }
    const regression = survey.regression;
    if (regression) {
      const ok = regression.status === 'ok';
      const rows = ok ? asArray(regression.coefficients).map((row) => [row.term, scalar(row.odds_ratio), `${scalar(row.ci_low)} – ${scalar(row.ci_high)}`, scalar(row.log_odds), scalar(row.se), scalar(row.df)]) : [];
      let note = ok ? `Adjusted odds ratios (OR), not risk ratios or causal effects. Complete-case n=${number(regression.n)}; ${regression.imputation_count || 1} analysis datasets.` : `Regression checks did not pass: ${regression.reason || regression.status || 'Not ready'}. Coefficients are not shown.`;
      if (regression.formula) note += ` Model: ${regression.formula}. `;
      if (ok && regression.references) note += ` Reference groups: ${Object.entries(regression.references).map(([key, value]) => `${key}=${value}`).join('; ')}. `;
      if (ok && regression.adjustment) note += ` Adjustment: ${regression.adjustment}. `;
      addSection('Survey-design-adjusted regression', note, ['Term', 'Adjusted OR', '95% CI for OR', 'Log odds', 'SE(Log odds)', 'Degrees of freedom'], rows);
      if (ok && regression.unadjusted) {
        const unadjusted = regression.unadjusted;
        const unadjustedRows = asArray(unadjusted.coefficients).map((row) => [row.term, scalar(row.odds_ratio), `${scalar(row.ci_low)} – ${scalar(row.ci_high)}`, scalar(row.log_odds), scalar(row.se), scalar(row.df)]);
        addSection('Unadjusted education model on the same sample', `Uses the same analysis sample as the adjusted model, n=${number(unadjusted.n ?? regression.n)}. Unadjusted ORs are not risk ratios or causal effects. Model: ${unadjusted.formula || 'See run record'}.`, ['Term', 'Unadjusted OR', '95% CI for OR', 'Log odds', 'SE(Log odds)', 'Degrees of freedom'], unadjustedRows);
      }
    }
    const checks = survey.checks;
    if (asArray(checks).length || survey.imputation || survey.missingness) {
      const details = document.createElement('details'); details.className = 'analysis-details';
      const summary = document.createElement('summary'); text(summary, 'Survey design, missing data, and calculation checks'); details.append(summary);
      const pre = document.createElement('pre'); text(pre, JSON.stringify({design: survey.design, methods: survey.methods, imputation: survey.imputation, missingness: survey.missingness, checks}, null, 2)); details.append(pre); host.append(details);
    }
  }

  function renderMeasurement(result) {
    const canPublish = result.status === 'ok' && result.survey?.status === 'ok';
    const comparison = canPublish ? asArray(result.comparison) : [];
    const gradients = canPublish ? asArray(result.gradient_comparison) : [];
    const measurement = canPublish ? result.measurement || {} : {status: 'blocked', message: 'This analysis did not pass release checks. Model comparison values are not shown.'};
    $('measurement-section').hidden = !comparison.length && !gradients.length && (!measurement.status || measurement.status === 'not_requested');
    text($('measurement-note'), (/模型设置|接口密钥|模型名称|API Key|保存.*连接/.test(measurement.message || '') ? 'The service is not ready. Please check the server configuration.' : measurement.message) || (measurement.status === 'ok' ? 'Each group uses the recorded repetitions and prompt versions. See the analysis report for details.' : measurement.status ? `Benchmark status: ${measurement.status}` : ''));
    $('comparison-body').replaceChildren();
    for (const row of comparison) {
      const tr = document.createElement('tr');
      const values = [rowLabel(row), percent(row.survey), percent(row.model_mean), finite(row.gap_pp) ? `${row.gap_pp > 0 ? '+' : ''}${row.gap_pp.toFixed(1)}` : '—', row.inside_ci === true ? 'Yes' : row.inside_ci === false ? 'No' : '—'];
      for (const value of values) { const td = document.createElement('td'); text(td, value); tr.append(td); }
      $('comparison-body').append(tr);
    }
    $('comparison-body').closest('.table-wrap').hidden = !comparison.length;
    const host = $('gradient-comparison'); host.replaceChildren();
    if (gradients.length) {
      const heading = document.createElement('div'); heading.className = 'chart-header';
      const h = document.createElement('h3'); text(h, 'Gradient comparison: survey and model group differences'); heading.append(h); host.append(heading);
      const note = document.createElement('p'); note.className = 'footnote'; text(note, 'All values below are percentage points. Gradient gap = model gradient − survey gradient.'); host.append(note);
      const wrap = document.createElement('div'); wrap.className = 'table-wrap';
      const table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr'), body = document.createElement('tbody');
      for (const label of ['Group contrast', 'Survey gradient (pp)', 'Model gradient (pp)', 'Gradient gap (pp)']) { const th = document.createElement('th'); text(th, label); header.append(th); }
      head.append(header); table.append(head);
      for (const row of gradients) {
        const tr = document.createElement('tr');
        const values = [rowLabel(row, 'Unnamed contrast'), ...['survey_gradient_pp', 'model_gradient_pp', 'gradient_gap_pp'].map((key) => finite(row[key]) ? `${row[key] > 0 ? '+' : ''}${row[key].toFixed(2)}` : '—')];
        for (const value of values) { const td = document.createElement('td'); text(td, value); tr.append(td); } body.append(tr);
      }
      table.append(body); wrap.append(table); host.append(wrap);
    }
  }

  function renderChart(rows, comparison = []) {
    // Dot = survey estimate, capsule = its 95% CI, diamond = model mean.
    const valid = rows.filter((row) => row.status === 'ok' && finite(row.estimate) && row.estimate >= 0 && row.estimate <= 1);
    const measured = new Map(comparison.filter((row) => finite(row.model_mean) && row.model_mean >= 0 && row.model_mean <= 1).map((row) => [row.group_id, row]));
    const hasModel = valid.some((row) => measured.has(row.group_id));
    $('model-chart-legend').hidden = !hasModel;
    const host = $('ci-chart'); host.replaceChildren();
    if (!valid.length) return;
    const block = (className, parent) => { const node = document.createElement('div'); node.className = className; parent.append(node); return node; };
    const mark = (className, track, left, width) => { const node = document.createElement('i'); node.className = className; node.style.left = `${left.toFixed(2)}%`; if (width !== undefined) node.style.width = `${width.toFixed(2)}%`; track.append(node); };
    const observations = valid.flatMap((row) => [pctValue(finite(row.ci_high) ? row.ci_high : row.estimate), measured.has(row.group_id) ? pctValue(measured.get(row.group_id).model_mean) : 0]);
    const maximum = Math.min(100, Math.max(10, Math.ceil(Math.max(...observations) / 10) * 10));
    const x = (v) => Math.max(0, Math.min(maximum, pctValue(v))) / maximum * 100;
    for (const row of valid) {
      const label = String(rowLabel(row));
      const model = measured.get(row.group_id);
      const line = block('plot-row', host);
      line.title = `${label}: ${percent(row.estimate)} (95% CI ${percent(row.ci_low)} – ${percent(row.ci_high)})` + (model ? ` · Model mean ${percent(model.model_mean)}` : '');
      text(block('plot-label', line), label);
      const track = block('plot-track', line);
      for (let i = 0; i <= 4; i++) mark('grid', track, i * 25);
      if (finite(row.ci_low) && finite(row.ci_high)) mark('range', track, (x(row.ci_low) + x(row.ci_high)) / 2, x(row.ci_high) - x(row.ci_low));
      mark('dot', track, x(row.estimate));
      if (model) mark('model', track, x(model.model_mean));
      text(block('plot-value', line), percent(row.estimate));
    }
    const axis = block('plot-row axis', host); axis.setAttribute('aria-hidden', 'true');
    block('plot-label', axis);
    const ticks = block('plot-track', axis);
    for (let i = 0; i <= 4; i++) { const tick = document.createElement('span'); tick.style.left = `${i * 25}%`; text(tick, `${(maximum * i / 4).toFixed(maximum % 4 ? 1 : 0)}%`); ticks.append(tick); }
    block('plot-value', axis);
  }

  async function refreshStatus() {
    try { state = await api('/api/status', undefined, 20000); applyAccess(state); renderStatus(state); return true; }
    catch (error) { text($('connection-badge'), 'Service is unavailable'); $('connection-badge').className = 'pill error'; text($('top-data-label'), 'Disconnected'); if ($('data-dialog').open || error.code === 'cookies_required') showSystemError(errorText(error)); return false; }
  }
  function renderStatus(status) {
    const data = status.data || {}, stats = status.statistics || {};
    text($('top-data-label'), data.ready ? 'Real data ready' : isPublic() ? 'Data not ready' : 'Prepare data'); $('top-data-dot').className = `live-dot${data.ready ? '' : ' muted-dot'}`;
    renderServiceStatus(status.dialogue || {});
    text($('connection-badge'), isPublic() ? 'Service connected' : 'Local service connected'); $('connection-badge').className = 'pill ready';
    text($('data-status'), data.ready ? 'Real survey data ready' : 'Real data not yet prepared');
    text($('data-detail'), data.ready ? `${finite(data.row_count) ? number(data.row_count) + ' records · ' : ''}${data.file ? String(data.file).split(/[\\/]/).pop() : 'Official NHIS adult public-use file'}` : data.message || 'Official NHIS 2024 adult public-use file');
    $('data-dot').className = `status-dot ${data.ready ? 'ready' : 'pending'}`;
    text($('stats-status'), stats.ready ? 'Statistical engine ready' : 'Statistical environment setup needed'); text($('stats-detail'), stats.message || stats.reason || 'R survey · complex design · weights and confidence intervals'); $('stats-dot').className = `status-dot ${stats.ready ? 'ready' : 'pending'}`;
    text($('income-method-note'), data.income_ready ? 'All 10 official income imputations are ready. Income analyses and fully adjusted regressions use Rubin pooling.' : 'Income analyses and fully adjusted regression require all 10 official imputations. They are not ready; a single imputation is never substituted.');
    $('data-notice').hidden = !!data.ready && !!data.income_ready;
    const download = data.download || status.download || {}, stage = download.status || '';
    const downloading = ['running', 'started', 'downloading', 'preparing', 'extracting', 'in_progress'].includes(stage);
    $('setup-data').disabled = downloading || !ownerAllowed(); $('setup-data').hidden = isPublic(); text($('setup-data'), downloading ? 'Preparing…' : 'Download official data');
    text($('download-title'), downloading ? 'Preparing official data' : data.ready ? 'Adult data ready; income imputations pending' : 'Prepare real survey data');
    text($('download-detail'), isPublic() ? 'The site operator prepares and verifies official data. You can continue chatting while data preparation is pending.' : download.message || data.income_message || data.message || 'Download and verify official files. No simulated estimates are generated.');
    $('runtime-notice').hidden = !!stats.ready; text($('runtime-detail'), isPublic() ? 'The statistical service is being prepared by the site operator. Please try research analyses again later.' : 'Run scripts/setup.command locally to prepare the statistical dependencies.' + (stats.reason ? ' ' + stats.reason : ''));
    text($('data-provenance'), JSON.stringify({data, statistics: stats, validation: status.validation || {}}, null, 2));
    text($('registry-content'), JSON.stringify({registry: status.registry || {}, capabilities: status.capabilities || {}}, null, 2));
    clearTimeout(pollTimer); if (downloading) pollTimer = setTimeout(refreshStatus, 3000);
  }
  function recordRun(result, question) {
    if (!runPattern.test(result.run_id || '')) return;
    runs.set(result.run_id, {...runs.get(result.run_id), run_id: result.run_id, question: question || runs.get(result.run_id)?.question, status: result.status, analysis: result.spec?.analysis, group_by: result.spec?.group_by, result}); renderHistory();
  }
  async function loadHistory() {
    const revision = ++historyRevision;
    const [runResponse, conversationResponse] = await Promise.allSettled([api('/api/runs', undefined, 15000), api('/api/conversations', undefined, 15000)]);
    if (revision !== historyRevision) return;
    if (runResponse.status === 'fulfilled') { for (const item of asArray(runResponse.value.runs)) if (runPattern.test(item.run_id || '')) runs.set(item.run_id, {...runs.get(item.run_id), ...item}); renderHistory(); }
    if (conversationResponse.status === 'fulfilled') {
      const items = Array.isArray(conversationResponse.value) ? conversationResponse.value : asArray(conversationResponse.value.conversations);
      for (const item of items) {
        if (!conversationPattern.test(item.id || '') || deletedConversationIds.has(item.id)) continue;
        const cached = conversations.get(item.id);
        if (cached?.updated_at && (!item.updated_at || String(cached.updated_at).localeCompare(String(item.updated_at)) > 0)) continue;
        conversations.set(item.id, {...cached, ...item});
      }
      renderConversationHistory();
    } else if (!conversations.size) { text($('conversation-history'), errorText(conversationResponse.reason)); $('conversation-history').classList.add('sidebar-empty'); }
    if (runResponse.status === 'rejected' && $('history-dialog').open) text($('history-error'), errorText(runResponse.reason));
  }
  function renderConversationHistory() {
    const host = $('conversation-history'); host.replaceChildren(); host.classList.remove('sidebar-empty');
    const search = $('conversation-search').value.trim().toLocaleLowerCase();
    const items = [...conversations.values()].filter((item) => !deletedConversationIds.has(item.id) && (!search || String(item.title || 'New chat').toLocaleLowerCase().includes(search))).sort((a, b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')));
    $('clear-conversations').disabled = busy || !conversations.size;
    if (!items.length) { const p = document.createElement('p'); p.className = 'sidebar-empty'; text(p, search ? 'No matching chats.' : isPublic() ? 'Your saved chats appear here. Cookies keep this browser’s history separate.' : 'Your saved chats appear here.'); host.append(p); return; }
    for (const item of items) {
      const row = document.createElement('div'); row.className = `sidebar-chat-row${item.id === activeConversation?.id ? ' active' : ''}`;
      const button = document.createElement('button'); button.type = 'button'; button.className = `sidebar-conversation${item.id === activeConversation?.id ? ' active' : ''}`;
      text(button, item.title || 'New chat'); button.title = item.title || 'New chat'; if (item.id === activeConversation?.id) button.setAttribute('aria-current', 'page');
      button.disabled = busy; button.addEventListener('click', () => openConversation(item.id)); row.append(button);
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'sidebar-delete'; remove.dataset.deleteConversation = item.id; remove.disabled = busy;
      remove.setAttribute('aria-label', `Delete chat: ${item.title || 'New chat'}`); remove.title = 'Delete chat';
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg'), use = document.createElementNS('http://www.w3.org/2000/svg', 'use'); icon.setAttribute('class', 'icon'); icon.setAttribute('aria-hidden', 'true'); use.setAttribute('href', '#icon-trash'); icon.append(use); remove.append(icon);
      remove.addEventListener('click', () => requestDeletion(item.id)); row.append(remove); host.append(row);
    }
  }
  function requestDeletion(id = null) {
    if (busy) { showSystemError('Wait for the current reply to finish before deleting chats.'); return; }
    if (id !== null && (!conversationPattern.test(id) || deletedConversationIds.has(id))) return;
    pendingDeletion = {id, title: id === null ? '' : conversations.get(id)?.title || 'New chat'};
    text($('delete-title'), id === null ? 'Clear all chats?' : 'Delete this chat?');
    text($('delete-description'), id === null ? isPublic() ? 'This permanently deletes all chats for this browser’s session. This cannot be undone. Your analysis records remain available.' : 'This permanently deletes every saved chat. This cannot be undone. Analysis records remain available.' : `“${pendingDeletion.title}” will be permanently deleted. This cannot be undone. Analysis records remain available.`);
    text($('delete-confirm'), id === null ? 'Clear all chats' : 'Delete chat'); text($('delete-error'), '');
    $('delete-confirm').disabled = false; $('delete-cancel').disabled = false; openDialog('delete-dialog'); $('delete-cancel').focus();
  }
  async function confirmDeletion() {
    if (!pendingDeletion || busy || deleting) return;
    const target = {...pendingDeletion}, activeId = activeConversation?.id, version = ++generation;
    ++historyRevision; deleting = true; setBusy(true, false); $('delete-confirm').disabled = true; $('delete-cancel').disabled = true; text($('delete-error'), '');
    let alreadyMissing = false;
    try {
      let response;
      try { response = await api(target.id === null ? '/api/conversations' : `/api/conversations/${target.id}`, undefined, 30000, 'DELETE'); }
      catch (error) { if (error.status !== 404 || target.id === null) throw error; alreadyMissing = true; response = {deleted: [target.id], deleted_count: 0}; }
      if (version !== generation) return;
      const removed = asArray(response.deleted).filter((id) => conversationPattern.test(id));
      if (target.id === null) { for (const id of conversations.keys()) deletedConversationIds.add(id); conversations.clear(); }
      for (const id of removed) { deletedConversationIds.add(id); conversations.delete(id); }
      if (target.id !== null) { deletedConversationIds.add(target.id); conversations.delete(target.id); }
      const removedActive = target.id === null || target.id === activeId || removed.includes(activeId);
      if (removedActive) { saveActive(null); clearConversationView(); $('conversation-search').value = ''; document.title = 'NHIS Lab · Chat and research'; }
      renderConversationHistory(); showSystemError(''); pendingDeletion = null; deleting = false; closeDialog($('delete-dialog'));
      text($('copy-status'), alreadyMissing ? 'This chat no longer exists. It has been removed from the list.' : target.id === null ? 'All chats deleted. Analysis records remain.' : 'Chat deleted. Analysis records remain.');
      if (alreadyMissing) showSystemError('This chat was already deleted. It has been removed from this list.');
      if (removedActive) {
        try {
          const created = await api('/api/conversations', {}, 20000); if (version !== generation) return;
          const conversation = created.conversation || created; if (!conversationPattern.test(conversation.id || '')) throw new Error('The service did not return a new chat.');
          renderConversation(conversation, true); $('question').focus();
        } catch (_) { if (version === generation) showSystemError('The chats were deleted, but a new chat could not be created. Send a message or select New chat to try again.'); }
      }
      if (version === generation) void loadHistory();
    } catch (error) {
      if (version === generation) text($('delete-error'), error.status === 409 ? 'A chat is still being processed. Wait for it to finish, then try again. No chats were deleted.' : errorText(error));
    } finally {
      if (version === generation) { deleting = false; setBusy(false); $('delete-confirm').disabled = false; $('delete-cancel').disabled = false; }
    }
  }
  function renderHistory() {
    const host = $('history-list'); host.replaceChildren();
    const items = [...runs.values()].sort((a, b) => b.run_id.localeCompare(a.run_id));
    if (!items.length) { const p = document.createElement('p'); p.className = 'plain-empty'; text(p, 'There are no saved analysis records yet.'); host.append(p); }
    for (const item of items) {
      const article = document.createElement('article'); article.className = 'history-item'; const top = document.createElement('div'); top.className = 'history-top';
      const id = document.createElement('span'); id.className = 'mono'; text(id, item.run_id); const badge = document.createElement('span'); badge.className = `pill${item.status === 'ok' ? ' ready' : ' error'}`; text(badge, item.status === 'ok' ? 'Analysis complete' : 'Estimates not released'); top.append(id, badge);
      const title = document.createElement('h2'); text(title, item.question || 'Saved research analysis');
      const footer = document.createElement('footer'), meta = document.createElement('small'); text(meta, {proportion: 'Group proportions', gradient: 'Group gradients', regression: 'Adjusted regression'}[item.analysis] || item.analysis || 'Real survey analysis');
      const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; text(button, 'View research evidence ↗'); button.addEventListener('click', () => openRun(item.run_id)); footer.append(meta, button); article.append(top, title, footer); host.append(article);
    }
  }
  async function openRun(id) {
    if (!runPattern.test(id || '')) { text($('history-error'), 'Invalid run ID format.'); return; }
    const scope = accessScope, version = generation;
    $('load-run-button').disabled = true; text($('history-error'), 'Loading saved record…');
    try {
      const cached = runs.get(id), result = cached?.result || await api(`/api/runs/${id}`);
      if (scope !== accessScope || version !== generation) return;
      recordRun(result, cached?.question); renderResults(result); text($('results-summary-label'), 'Survey estimates and record for this run'); text($('history-error'), ''); openDialog('evidence-dialog');
      // Viewing evidence never changes the active conversation, its filters, or the composer.
    } catch (error) { if (scope === accessScope && version === generation) { text($('history-error'), errorText(error)); if (!$('history-dialog').open) showSystemError(errorText(error)); } }
    finally { if (scope === accessScope && version === generation) $('load-run-button').disabled = false; }
  }
  async function setupData() {
    if (!ownerAllowed()) { text($('download-detail'), 'Official data preparation is managed by the site operator.'); return; }
    $('setup-data').disabled = true; text($('setup-data'), 'Starting…');
    try { const result = await api('/api/setup-data', {}); if (result.status === 'error' || result.error) throw new Error(result.message || result.error); await refreshStatus(); clearTimeout(pollTimer); pollTimer = setTimeout(refreshStatus, 3000); }
    catch (error) { text($('download-detail'), errorText(error)); $('setup-data').disabled = !ownerAllowed(); text($('setup-data'), 'Retry download'); }
  }
  $('sidebar-toggle').addEventListener('click', toggleSidebar);
  $('sidebar-close').addEventListener('click', () => { if (mobile.matches) closeSidebar(); else { document.body.classList.add('sidebar-collapsed'); closeSidebar(); } $('sidebar-toggle').focus(); });
  $('sidebar-backdrop').addEventListener('click', () => { closeSidebar(); $('sidebar-toggle').focus(); });
  mobile.addEventListener('change', closeSidebar);
  $('sidebar').addEventListener('keydown', (event) => {
    if (!mobile.matches || !document.body.classList.contains('sidebar-open') || event.key !== 'Tab') return;
    const items = [...$('sidebar').querySelectorAll('button:not(:disabled), a[href], input')], first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.querySelectorAll('[data-open-view]').forEach((button) => button.addEventListener('click', () => openView(button.dataset.openView)));
  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
  document.querySelectorAll('dialog').forEach((dialog) => {
    dialog.addEventListener('close', () => {
      requestAnimationFrame(() => {
        if (document.querySelector('dialog[open]')) return;
        const opener = dialogOpeners.get(dialog);
        const usable = opener?.isConnected && !opener.disabled && !opener.closest('[inert], [hidden]') && opener.getClientRects().length;
        (usable ? opener : $('sidebar-toggle')).focus({preventScroll: true});
      });
    });
    dialog.addEventListener('click', (event) => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(dialog); });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open') && !document.querySelector('dialog[open]')) { closeSidebar(); $('sidebar-toggle').focus(); }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) { event.preventDefault(); resetWorkspace(); }
  });
  $('conversation-search').addEventListener('input', renderConversationHistory);
  $('clear-conversations').addEventListener('click', () => requestDeletion());
  $('delete-confirm').addEventListener('click', confirmDeletion);
  $('delete-dialog').addEventListener('cancel', (event) => { if (deleting) event.preventDefault(); });
  $('delete-dialog').addEventListener('close', () => { if (!deleting) pendingDeletion = null; });
  $('new-analysis').addEventListener('click', resetWorkspace);
  $('ask-form').addEventListener('submit', ask);
  $('question').addEventListener('input', autoGrow);
  $('question').addEventListener('compositionstart', () => { composing = true; }); $('question').addEventListener('compositionend', () => { composing = false; autoGrow(); });
  $('question').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing && event.keyCode !== 229) { event.preventDefault(); $('ask-form').requestSubmit(); } });
  document.querySelectorAll('[data-question]').forEach((button) => button.addEventListener('click', () => fillQuestion(button.dataset.question)));
  $('chat-scroll').addEventListener('scroll', () => { const scroll = $('chat-scroll'); autoFollow = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120; $('jump-to-bottom').hidden = autoFollow || $('conversation-details').hidden; }, {passive: true});
  $('jump-to-bottom').addEventListener('click', () => scrollBottom(true));
  $('activity-process').addEventListener('click', () => showProcess(jobEvents, true));
  $('last-evidence').addEventListener('click', () => { if (lastResult) { renderResults(lastResult); openDialog('evidence-dialog'); } });
  $('load-run-form').addEventListener('submit', (event) => { event.preventDefault(); openRun($('load-run-id').value.trim()); });
  $('refresh-status').addEventListener('click', refreshStatus);
  $('setup-data').addEventListener('click', setupData);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => { document.documentElement.style.setProperty('--composer-height', `${Math.ceil($('composer-dock').getBoundingClientRect().height)}px`); }).observe($('composer-dock'));
  window.addEventListener('resize', autoGrow);
  async function initializeWorkspace() {
    if (!await refreshStatus()) { if (!$('system-error').textContent) showSystemError('Unable to confirm the service status. Refresh the page and try again.'); return; }
    if (cookiesUnavailable()) { showSystemError(cookieMessage); return; }
    await loadHistory();
    let restoredId = null; try { restoredId = sessionStorage.getItem(storageKey); } catch (_) { /* Optional opaque ID only. */ }
    if (conversationPattern.test(restoredId || '')) await openConversation(restoredId, true);
  }
  closeSidebar(); autoGrow(); void initializeWorkspace();
})();
