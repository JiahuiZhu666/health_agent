#!/usr/bin/env node
'use strict';

// Offline DOM smoke test for the actual private Markdown helpers in web/app.js.
// Fixtures exercise formatting/security only; nothing is served or saved as a run.
// Run from any directory: node /path/to/nhis-health-agent/tests/test_web_markdown.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const project = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(project, 'web', 'app.js'), 'utf8');
const start = source.indexOf('  function safeMarkdownUrl(');
const end = source.indexOf('  async function copyReply(', start);
assert(start >= 0 && end > start, 'Cannot find the production Markdown helpers.');
const helpers = source.slice(start, end);
const allowedTags = new Set([
  'DIV', 'P', 'H3', 'H4', 'H5', 'STRONG', 'CODE', 'PRE', 'A',
  'UL', 'OL', 'LI', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'HR',
]);

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.parentNode = null;
    this._text = '';
  }
  append(...nodes) {
    for (const node of nodes) { node.parentNode = this; this.children.push(node); }
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(''); }
  setAttribute(name, value) {
    assert(!/^on/i.test(name), 'Renderer attempted to attach an event handler.');
    assert(!/^(?:src|srcset|srcdoc)$/i.test(name), 'Renderer attempted to load an external resource.');
    this.attributes[name] = String(value);
  }
}
const document = {
  createElement(tag) {
    assert(allowedTags.has(tag.toUpperCase()), `Renderer created a forbidden tag: ${tag}`);
    return new Element(tag);
  },
  createTextNode(value) { const node = new Element('#text'); node.textContent = value; return node; },
};
const context = {
  document,
  URL,
  runPattern: /^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$/,
  text: (node, value) => { node.textContent = value; },
  fetch: () => { throw new Error('Network access must never occur while rendering Markdown.'); },
};
vm.createContext(context);
vm.runInContext(helpers, context, {timeout: 1000});
const render = (markdown) => { const root = new Element('div'); context.richText(root, markdown); return root; };
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const tags = (node, name) => descendants(node).filter((child) => child.tagName === name.toUpperCase());
const run = '/api/runs/20261115T234500Z-0123456789';
let checked = 0;
const test = (name, check) => { check(); checked++; console.log(`PASS ${name}`); };

test('headings, bold, inline code, ordered and nested unordered lists', () => {
  const result = render('### 结论\n**调查依据** 和 `estimate`。\n\n1. 明确人群\n2. 计算估计\n   - 使用权重\n   - 报告区间');
  assert.equal(tags(result, 'h4').length, 1);
  assert.equal(tags(result, 'strong')[0].textContent, '调查依据');
  assert.equal(tags(result, 'code')[0].textContent, 'estimate');
  assert.equal(tags(result, 'ol').length, 1);
  assert.equal(tags(result, 'ul').length, 1);
  assert.equal(tags(result, 'ul')[0].parentNode.tagName, 'LI');
  assert.equal(tags(result, 'li').length, 4);
  const continuation = render('3. 第三步\n4. 第四步');
  assert.equal(tags(continuation, 'ol')[0].start, 3);
});

test('compact tables preserve alignment, escaped pipes and code pipes', () => {
  const result = render('| 群体 | 结果 | 说明 |\n| :--- | ---: | :---: |\n| A | 12.3% | `a|b` |\n| B | 8.2% | x\\|y |');
  assert.equal(tags(result, 'table').length, 1);
  assert.equal(tags(result, 'tr').length, 3);
  assert.equal(tags(result, 'td').length, 6);
  assert.deepEqual(tags(result, 'th').map((cell) => cell.style.textAlign), ['left', 'right', 'center']);
  assert(tags(result, 'td').some((cell) => cell.textContent === 'x|y'));
  assert(tags(result, 'code').some((cell) => cell.textContent === 'a|b'));
  const region = tags(result, 'table')[0].parentNode;
  assert.equal(region.className, 'message-table-wrap');
  assert.equal(region.attributes.role, 'region');
  assert.equal(region.attributes.tabindex, '0');
  assert.equal(tags(render('列一 | 列二\n--- | ---\nA | B'), 'table').length, 1);
});

test('HTTPS and exact run-source links have safe target/rel', () => {
  const result = render(`[CDC](https://www.cdc.gov/nchs/nhis/) [记录](${run}) [报告](${run}/report) [CSV](${run}/csv)`);
  const anchors = tags(result, 'a');
  assert.equal(anchors.length, 4);
  for (const anchor of anchors) {
    assert.equal(anchor.target, '_blank');
    assert(anchor.rel.includes('noopener'));
    assert(anchor.rel.includes('noreferrer'));
    assert(anchor.href.startsWith('https://') || anchor.href.startsWith(run));
  }
  assert.equal(tags(render('[来源](https://example.org/report_(v1))'), 'a')[0].href, 'https://example.org/report_(v1)');
});

test('unsafe schemes, credentials and run-path traversal remain literal', () => {
  const destinations = [
    'javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,evil',
    'http://bad.example', '//bad.example', 'https://name:secret@example.com/',
    'https://example.com@bad.example/', 'https://example.com/" onclick="evil',
    '/api/runs/../../secret', '/api/runs/%2e%2e/secret', `${run}?unsafe=1`,
    'jav&#x61;script:alert(1)', 'https:\\bad.example',
  ];
  for (const destination of destinations) {
    const markdown = `[来源](${destination})`, result = render(markdown);
    assert.equal(tags(result, 'a').length, 0, `Unsafe destination became a link: ${destination}`);
    assert.equal(result.textContent, markdown);
    assert.equal(context.safeMarkdownUrl(destination), null);
  }
});

test('raw HTML, scripts, images and event attributes cannot create nodes', () => {
  const attack = '<script>globalThis.hacked=true</script> <img src=https://bad.example/pixel onerror=evil()> <svg onload=evil()></svg>\n\n![远程图像](https://bad.example/pixel.png)';
  const result = render(attack);
  assert.equal(tags(result, 'a').length, 0);
  assert.equal(tags(result, 'img').length, 0);
  assert.equal(tags(result, 'script').length, 0);
  assert.equal(tags(result, 'svg').length, 0);
  assert(result.textContent.includes('<script>globalThis.hacked=true</script>'));
  assert(result.textContent.includes('![远程图像](https://bad.example/pixel.png)'));
  assert.equal(context.hacked, undefined);
  assert(!helpers.includes('innerHTML'));
});

test('fenced code keeps malicious markup as code text', () => {
  const result = render('```html\n<img src=x onerror=evil()>\n<script>evil()</script>\n```');
  assert.equal(tags(result, 'pre').length, 1);
  assert.equal(tags(result, 'code').length, 1);
  assert.equal(tags(result, 'img').length, 0);
  assert.equal(tags(result, 'a').length, 0);
  assert(tags(result, 'code')[0].textContent.includes('<script>evil()</script>'));
  assert.equal(tags(render('~~~\n<iframe src=https://bad.example>\n~~~'), 'pre').length, 1);
});

test('malformed Markdown and non-table pipes retain their text', () => {
  const result = render('文字 **未闭合 和 `未闭合\n\n| 表头 |\n| 非分隔符 |');
  assert(result.textContent.includes('**未闭合'));
  assert(result.textContent.includes('| 非分隔符 |'));
  assert.equal(tags(result, 'table').length, 0);
  assert.equal(tags(render('前文\n\n```\n未闭合代码块'), 'pre')[0].textContent, '未闭合代码块');
});

test('copy action retains the original message source contract', () => {
  assert(source.includes('copyReply(value, copy)'), 'Copy must receive original content rather than rendered HTML.');
  assert(!source.includes('copyReply(body.innerHTML'));
});

console.log(`\n${checked} offline Markdown checks passed. No server, browser, dependency install or model call required.`);
