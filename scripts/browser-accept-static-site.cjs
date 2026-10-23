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
const mime = {
  '.html': 'text/html', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
  '.mp4': 'video/mp4', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
};
const headers = Object.fromEntries(fs.readFileSync(path.join(site, '_headers'), 'utf8')
  .split('\n').filter(line => line.startsWith('  ')).map(line => {
    const colon = line.indexOf(':');
    return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()];
  }));
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const file = path.resolve(site, `.${pathname === '/' ? '/index.html' : pathname}`);
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
      const headerLinks = await page.locator('header a').evaluateAll(links => links.map(link => ({
        label: link.getAttribute('aria-label') || link.textContent.trim(),
        href: link.getAttribute('href'),
      })));
      assert.deepEqual(headerLinks, [
        { label: 'Exculpata home', href: '#main' },
        { label: 'GitHub', href: 'https://github.com/Exculpata/exculpata' },
      ]);
      assert.equal(await page.locator('header nav').count(), 0);
      for (const name of ['From discovery to understanding.', 'Built for the defense.']) {
        await page.getByRole('heading', { name, exact: true }).scrollIntoViewIfNeeded();
        assert(await page.getByRole('heading', { name, exact: true }).isVisible());
      }
      await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
      const footer = await page.locator('footer > a').evaluate(el => ({
        bottomClearance: innerHeight - el.getBoundingClientRect().bottom,
        visible: el.getBoundingClientRect().top >= 0 && el.getBoundingClientRect().bottom <= innerHeight,
      }));
      assert(footer.visible && footer.bottomClearance >= 60, 'Footer needs bottom breathing room');
      if (width === 1440 && height === 1000 || width === 390) {
        await page.screenshot({ path: path.join(output, width === 390 ? 'mobile-bottom.png' : 'desktop-bottom.png') });
      }
      observations.push({ width, height, headerLinks, sections: 'visible', footer });
    }
    await page.goto(origin);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Skip to content');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
    assert([...requests].every(url => url.startsWith('/')), 'External resource request');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'receipt.json'), `${JSON.stringify({
      head: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
      dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim()),
      observations, requests: [...requests], errors,
    }, null, 2)}\n`);
    console.log('Static-site browser acceptance passed: five viewports, simple GitHub header, readable sections, keyboard and local resources.');
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    server.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
