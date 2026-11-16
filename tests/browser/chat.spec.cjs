'use strict';
const {test, expect} = require('@playwright/test');
const fs = require('node:fs/promises');
const composer = (page) => page.getByRole('textbox', {name: 'Send a message to NHIS Lab'});
const fixtureState = async (page) => (await page.request.get('/__fixture__/state')).json();
const release = async (page, executionState = 'succeeded') => page.request.post('/__fixture__/release', {data: {execution_state: executionState}});
async function send(page, message) { await composer(page).fill(message); await composer(page).press('Enter'); }

test('delayed POST acknowledgment preserves the next typed draft', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:delayed-ack draft preservation');
  await expect.poll(async () => (await fixtureState(page)).created_jobs).toBe(1);
  await composer(page).fill('This next draft must survive the acknowledgment.');
  await release(page);
  await expect(page.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
  await expect(composer(page)).toHaveValue('This next draft must survive the acknowledgment.');
});

test('a dropped acknowledgment retries one saved submission without duplicating a job', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:dropped-ack retry identity');
  await expect(page.getByRole('button', {name: 'Retry same submission', exact: true})).toBeVisible();
  await composer(page).fill('A separate draft stays here.');
  await page.getByRole('button', {name: 'Retry same submission', exact: true}).click();
  await expect(page.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
  await expect(composer(page)).toHaveValue('A separate draft stays here.');
  const state = await fixtureState(page); expect(state.created_jobs).toBe(1); expect(state.message_posts).toBeGreaterThanOrEqual(2);
});

test('reload polls the saved queued/running job without another submission', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:running restore the saved job');
  await expect(page.getByRole('button', {name: 'Cancel turn', exact: true})).toBeVisible();
  const before = await fixtureState(page); await page.reload();
  await expect(page.getByRole('button', {name: 'Cancel turn', exact: true})).toBeVisible();
  await release(page);
  await expect(page.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
  const after = await fixtureState(page); expect(after.message_posts).toBe(before.message_posts); expect(after.created_jobs).toBe(1);
});

test('unknown execution stays explanatory after reload and is never blindly resumed', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:unknown interrupted external attempt');
  await expect(page.getByText(/The outcome of this saved turn is uncertain/)).toBeVisible();
  await expect(page.getByRole('button', {name: 'Resume saved work', exact: true})).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(/The outcome of this saved turn is uncertain/)).toBeVisible();
  expect((await fixtureState(page)).message_posts).toBe(1);
});

test('cancellation is requested first and only later reported as cancelled', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:running cooperative cancellation');
  await page.getByRole('button', {name: 'Cancel turn', exact: true}).click();
  await expect(page.getByRole('button', {name: 'Cancellation requested', exact: true})).toBeDisabled();
  await expect(page.getByText('Waiting for a safe stopping point. Work already sent or completed is not undone.')).toBeVisible();
  await expect(page.getByText(/This turn was cancelled\./)).toBeVisible();
  await expect(composer(page)).toBeEditable(); expect((await fixtureState(page)).created_jobs).toBe(1);
});

test('a polling network interruption restores existing work without resubmitting', async ({page}) => {
  let interrupted = false;
  await page.route('**/api/jobs/*', async (route) => {
    if (!interrupted && route.request().method() === 'GET') { interrupted = true; await route.abort('connectionreset'); }
    else await route.continue();
  });
  await page.goto('/'); await send(page, 'fixture:running network recovery');
  await expect(page.getByRole('button', {name: 'Restore chat without resending', exact: true})).toBeVisible();
  await composer(page).fill('My draft during reconnection.');
  await page.getByRole('button', {name: 'Restore chat without resending', exact: true}).click();
  await expect(page.getByRole('button', {name: 'Cancel turn', exact: true})).toBeVisible(); await release(page);
  await expect(page.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
  await expect(composer(page)).toHaveValue('My draft during reconnection.'); expect((await fixtureState(page)).message_posts).toBe(1);
});

test('real browser cookies isolate visitors and deletion only removes the selected visitor chat', async ({browser}) => {
  const first = await browser.newContext({baseURL: 'http://127.0.0.1:18765'}), second = await browser.newContext({baseURL: 'http://127.0.0.1:18765'});
  try {
    const a = await first.newPage(), b = await second.newPage();
    await a.goto('/'); await send(a, 'Visitor A fixture chat');
    await expect(a.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
    const conversation = (await fixtureState(a)).conversations[0];
    await b.goto('/'); expect((await b.request.get(`/api/conversations/${conversation.id}`)).status()).toBe(404);
    await expect(b.getByRole('button', {name: 'Visitor A fixture chat', exact: true})).toHaveCount(0);
    await send(b, 'Visitor B fixture chat'); await expect(b.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
    await a.getByRole('button', {name: 'Delete chat: Visitor A fixture chat', exact: true}).click();
    await expect(a.getByRole('dialog', {name: 'Delete this chat?'})).toBeVisible();
    await a.getByRole('button', {name: 'Delete chat', exact: true}).click();
    await expect(a.getByRole('button', {name: 'Visitor A fixture chat', exact: true})).toHaveCount(0);
    expect((await fixtureState(b)).conversations.some((item) => item.title === 'Visitor B fixture chat')).toBe(true);
  } finally { await first.close(); await second.close(); }
});

test('inline fixture charts download valid standalone SVG and real PNG files', async ({page}) => {
  await page.goto('/'); await send(page, 'fixture:chart download checks');
  await expect(page.getByRole('img', {name: 'Fixture survey chart. Estimates and 95% confidence intervals.'})).toBeVisible();
  const [svg] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', {name: 'Download Fixture survey chart as SVG', exact: true}).click()]);
  expect(svg.suggestedFilename()).toBe('nhis-20261116T120000Z-0123456789-proportion-point.svg');
  const svgText = await fs.readFile(await svg.path(), 'utf8'); expect(svgText).toContain('data-value="12.3"'); expect(svgText).not.toMatch(/<(?:script|image|foreignObject)\b/);
  const [png] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', {name: 'Download Fixture survey chart as PNG', exact: true}).click()]);
  expect(png.suggestedFilename()).toMatch(/\.png$/); const pngBytes = await fs.readFile(await png.path());
  expect([...pngBytes.subarray(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
});

test('composer respects IME input and the accepted layout fits narrow screens', async ({page}) => {
  for (const width of [320, 768, 1440]) {
    await page.setViewportSize({width, height: 900}); await page.goto('/');
    await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(composer(page)).toBeVisible();
  }
  await composer(page).fill('IME draft fixture'); await composer(page).dispatchEvent('compositionstart');
  await composer(page).dispatchEvent('keydown', {key: 'Enter', code: 'Enter', isComposing: true, keyCode: 229});
  expect((await fixtureState(page)).message_posts).toBe(0);
  await composer(page).dispatchEvent('compositionend'); await composer(page).press('Shift+Enter');
  expect((await fixtureState(page)).message_posts).toBe(0);
  await composer(page).press('Enter'); await expect(page.getByText('Fixture reply saved without a live model call.', {exact: true})).toBeVisible();
});

test('chat and analysis lists load additional cursor pages only when requested', async ({page}) => {
  await page.goto('/'); await page.request.post('/__fixture__/seed-pagination', {data: {chats: 35, runs: 35}}); await page.reload();
  await expect(page.getByRole('button', {name: 'Load more chats', exact: true})).toBeVisible();
  await expect(page.locator('.sidebar-conversation')).toHaveCount(30);
  await page.getByRole('button', {name: 'Load more chats', exact: true}).click();
  await expect(page.locator('.sidebar-conversation')).toHaveCount(35);
  await expect(page.getByRole('button', {name: 'Load more chats', exact: true})).toBeHidden();
  await page.getByRole('button', {name: 'Analysis history', exact: true}).click();
  await expect(page.locator('#history-list .history-item')).toHaveCount(30);
  await page.getByRole('button', {name: 'Load more analyses', exact: true}).click();
  await expect(page.locator('#history-list .history-item')).toHaveCount(35);
  await expect(page.getByRole('button', {name: 'Load more analyses', exact: true})).toBeHidden();
  expect((await fixtureState(page)).message_posts).toBe(0);
});

test('earlier message pages preserve the new draft and render each saved message once', async ({page}) => {
  await page.goto('/'); await page.request.post('/__fixture__/seed-pagination', {data: {chats: 1, messages: 120}}); await page.reload();
  await page.getByRole('button', {name: 'Fixture long chat', exact: true}).click();
  await expect(page.locator('#conversation > .message')).toHaveCount(50);
  await composer(page).fill('New draft while reading older messages.');
  await page.getByRole('button', {name: 'Load earlier messages', exact: true}).click();
  await expect(page.locator('#conversation > .message')).toHaveCount(100);
  await page.getByRole('button', {name: 'Load earlier messages', exact: true}).click();
  await expect(page.locator('#conversation > .message')).toHaveCount(120);
  await expect(page.getByRole('button', {name: 'Load earlier messages', exact: true})).toBeHidden();
  await expect(composer(page)).toHaveValue('New draft while reading older messages.');
  expect(await page.locator('#conversation > .message').evaluateAll((nodes) => new Set(nodes.map((node) => node.dataset.messageId)).size)).toBe(120);
  expect((await fixtureState(page)).message_posts).toBe(0);
});
