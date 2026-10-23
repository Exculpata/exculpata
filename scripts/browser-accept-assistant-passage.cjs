#!/usr/bin/env node
// Supplemental synthetic acceptance with Playwright and an explicitly selected browser.
// Usage: node scripts/browser-accept-assistant-passage.cjs /path/to/chromium /tmp/fresh-output
const { chromium } = require('playwright');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.argv[3]);
fs.mkdirSync(output); // Retain prior receipts; never overwrite them.
const server = spawn(path.join(root, '.venv/bin/python'), ['-u', '-c', `
import json, socket, tempfile, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from synthetic_browser_environment import isolate_environment
isolate_environment()
import uvicorn
from fastapi.testclient import TestClient
from case_intelligence.workbench import create_workbench_app
from case_intelligence.generation import UnavailableGenerator
from tests.test_assistant_passage_save import seed
with tempfile.TemporaryDirectory() as temporary:
    app = create_workbench_app(Path(temporary), generator=UnavailableGenerator(), auth_mode='test')
    with TestClient(app) as client:
        client.get('/')
        matter, conversation, message, document, save_url = seed(app.state.workbench)
        alternate = app.state.workbench.workspace.create_conversation(matter.matter_id,
            'Synthetic alternate saved chat', actor_id='development-taylor-morgan')
        app.state.workbench.workspace.append_message(matter.matter_id, alternate.conversation_id,
            'assistant', message.content, message.payload)
        token = app.state.workbench.source_store(matter).action_token(document)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        print(json.dumps(dict(base=f'http://127.0.0.1:{listener.getsockname()[1]}',
            reader=f'/matters/{matter.slug}/sources/{token}?unit=1&q=gauge&conversation={conversation.conversation_id}',
            matter=f'/matters/{matter.slug}', conversation=conversation.conversation_id, alternate=alternate.conversation_id, save_url=save_url)), flush=True)
        uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='warning')).run(sockets=[listener])
`], { cwd: root, stdio: ['ignore', 'pipe', 'inherit'] });
let browser, activePage;
const deadline = setTimeout(() => {
  console.error('Synthetic browser acceptance exceeded 90 seconds.');
  server.kill('SIGTERM');
  if (browser) browser.close();
  process.exitCode = 1;
}, 90000);
const checks = [];
const revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const dirty = Boolean(execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim());
(async () => {
  const [data] = await once(server.stdout, 'data');
  const fixture = JSON.parse(data.toString().trim());
  browser = await chromium.launch({ executablePath: process.argv[2], headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  activePage = page;
  await page.goto(fixture.base + fixture.reader);
  const form = page.locator('[data-assistant-save-passage]').first();
  if (!await form.isVisible()) await page.locator('[data-assistant-expand]').click();
  const button = form.locator('button');
  const feedback = form.locator('[role=status]');
  await page.locator('#assistant-question').fill('Keep this synthetic unsent draft.');
  await button.focus();
  await page.evaluate(() => window.scrollTo(0, 230));
  assert.ok(await page.evaluate(() => scrollY > 0), 'Exercise a scrolled reader');
  const snapshot = () => page.evaluate(() => ({ url: location.href,
    draft: document.querySelector('#assistant-question').value,
    chat: document.querySelector('[data-assistant-dock]').dataset.conversationId,
    scroll: [...document.querySelectorAll('body, .assistant-thread, .source-review-main, .source-browser-viewer')].map(e => [e.scrollTop, e.scrollLeft]),
    window: [scrollX, scrollY] }));
  const before = await snapshot();
  await button.press('Enter');
  await feedback.filter({ hasText: 'Passage saved' }).waitFor();
  assert.deepEqual(await snapshot(), before);
  assert.equal(await button.evaluate(e => e === document.activeElement), true);
  checks.push('Keyboard save preserves reader URL, filters, section, scroll, chat, draft and focus.');
  await button.press('Enter');
  await feedback.filter({ hasText: 'already saved' }).waitFor();
  assert.deepEqual(await snapshot(), before);
  checks.push('Repeated save reports deterministic deduplication inline.');
  await page.screenshot({ path: path.join(output, 'synthetic-dock-save.png') });
  // Hold only the response, allowing the real authenticated save to complete.
  let release, intercepted;
  const arrived = new Promise(resolve => intercepted = resolve);
  const hold = new Promise(resolve => release = resolve);
  await page.route(`**${fixture.save_url}`, async route => {
    const response = await route.fetch(); intercepted(); await hold;
    await route.fulfill({ response });
  });
  await button.click(); await arrived;
  await page.locator('[data-assistant-new-chat]').click();
  await page.locator('#assistant-question').fill('New synthetic draft while save is pending.');
  release();
  await page.waitForResponse(response => response.url().endsWith(fixture.save_url));
  assert.equal(await page.locator('#assistant-question').inputValue(), 'New synthetic draft while save is pending.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'), '');
  checks.push('Late save response does not replace a new chat or its draft.');
  await page.unroute(`**${fixture.save_url}`);
  await page.goto(fixture.base + fixture.reader);
  let releaseNavigation, navigationArrived;
  const navigationHold = new Promise(resolve => releaseNavigation = resolve);
  const navigationRequest = new Promise(resolve => navigationArrived = resolve);
  await page.route(`**${fixture.save_url}`, async route => {
    const response = await route.fetch(); navigationArrived(); await navigationHold;
    await route.fulfill({ response }).catch(() => {}); // old page may already be gone
  });
  await page.locator('[data-assistant-save-passage]').first().locator('button').click();
  await navigationRequest;
  const nextReader = fixture.base + fixture.reader.replace('unit=1', 'unit=2');
  await page.goto(nextReader);
  releaseNavigation();
  assert.equal(page.url(), nextReader);
  await page.locator('#assistant-question').fill('Draft in the next section.');
  assert.equal(await page.locator('#assistant-question').inputValue(), 'Draft in the next section.');
  checks.push('Navigation to the next source section remains intact while an earlier save is pending.');
  await page.unroute(`**${fixture.save_url}`);
  await page.goto(fixture.base + fixture.reader);
  const reader = page.url();
  await page.goto(`${fixture.base}${fixture.matter}/notebook`);
  // Let the existing saved-chat preference finish replacing the notebook's
  // default chat before testing browser history. A document load alone does
  // not await that independent fragment request.
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.conversation}"][data-bound="true"]`).waitFor();
  await page.goBack(); assert.equal(page.url(), reader);
  await page.goForward(); assert.equal(new URL(page.url()).pathname, `${fixture.matter}/notebook`);
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.conversation}"][data-bound="true"]`).waitFor();
  await page.goBack(); await page.reload();
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.conversation}"][data-bound="true"]`).waitFor();
  await page.locator('[data-assistant-save-passage]').first().locator('button').click();
  await page.locator('[data-save-passage-status]').filter({ hasText: 'already saved' }).waitFor();
  checks.push('Back, Forward and reload retain source navigation and saved-note deduplication.');
  // Transport failures are shown in the originating claim, without navigation.
  for (const status of [403, 409]) {
    await page.route(`**${fixture.save_url}`, route => route.fulfill({ status,
      contentType: 'application/json', body: JSON.stringify({ detail: status === 403 ? 'Access unavailable' : 'Source support changed' }) }));
    const state = await snapshot();
    await page.locator('[data-assistant-save-passage]').first().locator('button').click();
    await page.locator('[data-save-passage-status]').filter({ hasText: 'safely try saving again' }).waitFor();
    assert.deepEqual(await snapshot(), state);
    await page.unroute(`**${fixture.save_url}`);
  }
  checks.push('Lost-access and stale-support responses stay inline; route tests verify the real no-write boundaries.');
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.alternate}"]`).waitFor();
  await page.locator('[data-assistant-save-passage]').first().locator('button').click();
  await page.locator('[data-save-passage-status]').filter({ hasText: 'Passage saved' }).waitFor();
  assert.equal(page.url(), reader);
  checks.push('A replaced saved-chat fragment binds its own claim save without moving the reader.');
  await page.setViewportSize({ width: 390, height: 844 });
  if (!await page.locator('[data-assistant-save-passage]').first().isVisible())
    await page.locator('[data-assistant-expand]').click();
  await page.locator('[data-assistant-save-passage]').first().locator('button').click();
  await page.locator('[data-save-passage-status]').filter({ hasText: 'already saved' }).waitFor();
  assert.ok(await page.locator('[data-assistant-thread]').evaluate(e => e.scrollWidth <= e.clientWidth + 1));
  await page.screenshot({ path: path.join(output, 'synthetic-dock-save-mobile.png') });
  checks.push('Claim save and feedback remain usable at 390 pixels without horizontal thread overflow.');
  const nojs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 1440, height: 1000 } });
  const fallback = await nojs.newPage();
  await fallback.goto(fixture.base + fixture.reader);
  await fallback.locator('[data-assistant-save-passage]').first().locator('button').click();
  assert.equal(new URL(fallback.url()).pathname, new URL(fixture.base + fixture.reader).pathname);
  assert.equal(new URL(fallback.url()).searchParams.get('q'), 'gauge');
  assert.equal(new URL(fallback.url()).searchParams.get('unit'), '1');
  assert.match(new URL(fallback.url()).searchParams.get('notice'), /already saved/);
  checks.push('JavaScript-disabled submission returns to the same matter reader and filters.');
  fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ synthetic_only: true,
    passed: true, revision, dirty, browser: await browser.version(), checks }, null, 2));
  console.log(JSON.stringify(checks, null, 2));
})().catch(async error => {
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({ path: path.join(output, 'synthetic-failure.png') }).catch(() => {});
    console.error(await activePage.locator('[data-assistant-dock]').innerText().catch(() => 'Dock unavailable'));
  }
  console.error(error);
  fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ synthetic_only: true,
    passed: false, revision, dirty, checks, error: String(error).slice(0, 2000) }, null, 2));
  process.exitCode = 1;
}).finally(async () => {
  clearTimeout(deadline);
  if (browser) await browser.close();
  server.kill('SIGTERM');
});
