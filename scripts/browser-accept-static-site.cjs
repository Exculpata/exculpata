#!/usr/bin/env node
// Static landing-page acceptance. No application, case fixtures, or external requests.
// Usage: node scripts/browser-accept-static-site.cjs /path/to/chromium /tmp/fresh-output
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { once } = require('node:events');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const site = path.join(root, 'site');
assert(process.argv[2] && process.argv[3], 'Provide a Chromium executable and fresh output directory.');
const output = path.resolve(process.argv[3]);
fs.mkdirSync(output); // Never overwrite previous evidence.
const mime = { '.html': 'text/html', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const headers = Object.fromEntries(fs.readFileSync(path.join(site, '_headers'), 'utf8')
  .split('\n').filter(line => line.startsWith('  ')).map(line => {
    const colon = line.indexOf(':');
    return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()];
  }));
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const file = path.resolve(site, '.' + (pathname === '/' ? '/index.html' : pathname));
  if (!file.startsWith(site + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, { ...headers, 'Content-Type': mime[path.extname(file)] || 'text/plain' });
  fs.createReadStream(file).pipe(response);
});

(async () => {
  let browser;
  const observations = [];
  const requests = new Set();
  const errors = [];
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ executablePath: process.argv[2], headless: true });
    const context = await browser.newContext();
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      requests.add(url.origin === origin ? url.pathname : url.href);
      return url.origin === origin ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (['error', 'warning'].includes(message.type())) errors.push(message.text());
    });
    for (const [width, height] of [[1010, 1558], [1440, 1200], [1440, 1000], [390, 844], [320, 740]]) {
      await page.setViewportSize({ width, height });
      await page.goto(origin);
      await page.evaluate(() => document.fonts.ready);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow');
      assert.equal(await page.locator('header').evaluate(el => getComputedStyle(el).position), 'static');
      if (width === 1440 && height === 1000 || width === 390) {
        await page.screenshot({ path: path.join(output, width === 390 ? 'mobile.png' : 'desktop.png'), fullPage: true });
      }
      const destinations = [];
      for (const [name, id] of [['What it does', 'what-it-does'], ['Who it’s for', 'who-its-for']]) {
        await page.getByRole('link', { name, exact: true }).click();
        assert.equal(new URL(page.url()).hash, '#' + id);
        await page.waitForFunction(id => Math.abs(document.getElementById(id).getBoundingClientRect().top - 32) <= 2, id);
        const result = await page.locator('#' + id).evaluate(el => ({
          top: el.getBoundingClientRect().top,
          scrollY,
          heading: el.querySelector('h2').innerText.replace(/\s+/g, ' '),
          headingColor: getComputedStyle(el.querySelector('h2')).color,
        }));
        assert.equal(result.headingColor, 'rgb(34, 211, 238)');
        destinations.push(result);
        await page.screenshot({ path: path.join(output, `${id}-${width}x${height}.png`) });
      }
      assert(Math.abs(destinations[0].scrollY - destinations[1].scrollY) > 100, 'Anchors land at the same scroll position');
      assert.notEqual(destinations[0].heading, destinations[1].heading);
      await page.goBack();
      assert.equal(new URL(page.url()).hash, '#what-it-does');
      await page.waitForFunction(() => Math.abs(document.getElementById('what-it-does').getBoundingClientRect().top - 32) <= 2);
      await page.goto(origin + '/#who-its-for');
      await page.waitForFunction(() => Math.abs(document.getElementById('who-its-for').getBoundingClientRect().top - 32) <= 2);
      observations.push({ width, height, destinations, backAndDirectLink: 'pass' });
    }
    await page.goto(origin);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Skip to content');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
    assert([...requests].every(url => url.startsWith('/')), 'External resource request');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({
      head: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
      dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim()),
      observations, requests: [...requests], errors,
    }, null, 2) + '\n');
    console.log('Static-site browser acceptance passed: five viewports, distinct anchors, history, direct links, keyboard and local resources.');
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    server.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
