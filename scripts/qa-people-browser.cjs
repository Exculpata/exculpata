/* Synthetic companion to qa-people-browser.py; uses no existing installation. */
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.RECORDBENCH_QA_ORIGIN;
assert.match(origin || '', /^https:\/\/127\.0\.0\.1:\d+$/);
const password = 'synthetic-browser-password';
const changedPassword = 'synthetic-reset-password';

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.RECORDBENCH_QA_BROWSER_CHANNEL || undefined});
  try {
    const options = {ignoreHTTPSErrors: true, viewport: {width: 1440, height: 1000}};
    const admin = await (await browser.newContext(options)).newPage();
    const first = await (await browser.newContext(options)).newPage();
    const second = await (await browser.newContext(options)).newPage();
    async function login(page, username, value = password) {
      await page.goto(origin + '/auth/login');
      assert.equal(await page.title(), 'Sign in · Exculpata');
      await page.getByRole('textbox', {name: 'Username', exact: true}).fill(username);
      await page.getByLabel('Password', {exact: true}).fill(value);
      await page.getByRole('button', {name: 'Open Exculpata'}).click();
      await page.waitForURL(url => !url.pathname.startsWith('/auth/'));
      await page.getByRole('link', {name: 'Exculpata home', exact: true}).waitFor();
    }
    async function create(username, name) {
      await admin.goto(origin + '/admin/people');
      await admin.getByLabel('Name shown in Exculpata').fill(name);
      await admin.keyboard.press('Tab');
      await admin.keyboard.type(username);
      await admin.getByLabel('Password', {exact: true}).fill(password);
      await admin.getByLabel('Enter password again', {exact: true}).fill(password);
      await admin.getByRole('button', {name: 'Create account', exact: true}).click();
      await admin.waitForURL(url => url.pathname === '/admin/people/accounts/' + username);
      assert.ok((await admin.getByRole('status').innerText()).includes('Account created'));
      assert.ok(!(await admin.content()).includes(password));
    }
    await login(admin, 'alice.admin');
    assert.equal(new URL(admin.url()).pathname, '/admin/setup');
    await admin.getByRole('heading', {name: 'Bring your team into Exculpata'}).waitFor();
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'setup-desktop.png'), fullPage: true});
    await admin.setViewportSize({width: 390, height: 844});
    await admin.reload();
    assert.equal(await admin.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'setup-mobile.png'), fullPage: true});
    await admin.setViewportSize({width: 1440, height: 1000});
    await admin.reload();
    await create('first.reviewer', 'First Reviewer');
    await create('second.reviewer', 'Second Reviewer');
    await login(first, 'first.reviewer');
    await login(second, 'second.reviewer');
    await admin.goto(origin + '/matters/new');
    await admin.getByLabel('Matter name', {exact: true}).fill('Synthetic browser acceptance matter');
    await admin.getByRole('button', {name: 'Create matter', exact: true}).click();
    await admin.waitForURL(/\/matters\/[^/]+\/setup/);
    const slug = new URL(admin.url()).pathname.split('/')[2];
    await admin.getByLabel('Add a case team member').selectOption({label: 'First Reviewer'});
    await admin.getByRole('button', {name: 'Add member', exact: true}).click();
    await admin.getByText('First Reviewer added to the case team', {exact: true}).waitFor();
    assert.equal((await first.goto(origin + '/matters/' + slug + '/home')).status(), 200);
    assert.ok([403, 404].includes((await second.goto(origin + '/matters/' + slug + '/home')).status()));
    assert.equal((await second.goto(origin + '/admin/people')).status(), 403);
    assert.equal((await second.goto(origin + '/admin/setup')).status(), 403);
    await admin.goto(origin + '/admin/setup?matter=' + slug);
    await admin.getByText('A current teammate has opened this matter since their latest access grant.', {exact: true}).waitFor();
    await admin.goto(origin + '/admin/people/accounts/first.reviewer');
    await admin.getByLabel('New password', {exact: true}).fill(changedPassword);
    await admin.getByLabel('Enter new password again', {exact: true}).fill(changedPassword);
    await admin.getByRole('button', {name: 'Change password', exact: true}).click();
    await admin.getByRole('status').filter({hasText: 'Password changed'}).waitFor();
    await first.reload();
    assert.equal(new URL(first.url()).pathname, '/auth/login');
    await login(first, 'first.reviewer', changedPassword);
    await admin.getByRole('button', {name: 'Disable sign-in', exact: true}).click();
    await admin.getByRole('status').filter({hasText: 'Sign-in access updated'}).waitFor();
    await first.reload();
    assert.equal(new URL(first.url()).pathname, '/auth/login');
    await admin.getByLabel('Name shown in Exculpata', {exact: true}).fill('Renamed Offline Reviewer');
    await admin.getByRole('button', {name: 'Save name', exact: true}).click();
    await admin.getByRole('status').filter({hasText: 'Name updated'}).waitFor();
    await admin.goto(origin + '/matters/' + slug + '/setup');
    await admin.getByText('Renamed Offline Reviewer', {exact: true}).first().waitFor();
    await admin.goto(origin + '/admin/people');
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'people-desktop.png'), fullPage: true});
    await admin.setViewportSize({width: 390, height: 844});
    await admin.reload();
    assert.equal(await admin.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'people-mobile.png'), fullPage: true});
    console.log('PASS: Team setup entry and access evidence, People creation, keyboard entry, two-user matter authorization, reset/disable revocation, disabled-account team-name refresh, and 390px layout.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
