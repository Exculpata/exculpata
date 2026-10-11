/* Synthetic independent local sessions; no existing installation. */
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.RECORDBENCH_QA_ORIGIN;
assert.match(origin || '', /^https:\/\/127\.0\.0\.1:\d+$/);
const password = 'synthetic-browser-password';
(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.RECORDBENCH_QA_BROWSER_CHANNEL || undefined});
  try {
    const options = {ignoreHTTPSErrors: true, viewport: {width: 1440, height: 1000}};
    const admin = await (await browser.newContext(options)).newPage();
    const reviewer = await (await browser.newContext(options)).newPage();
    async function login(page, username) {
      await page.goto(origin + '/auth/login');
      await page.getByRole('textbox', {name: 'Username', exact: true}).fill(username);
      await page.getByLabel('Password', {exact: true}).fill(password);
      await page.getByRole('button', {name: 'Open Exculpata'}).click();
      await page.waitForURL(url => !url.pathname.startsWith('/auth/'));
    }
    await login(admin, 'alice.admin');
    await admin.goto(origin + '/admin/people');
    await admin.getByLabel('Name shown in Exculpata').fill('Synthetic Reviewer');
    await admin.getByLabel('Sign-in username', {exact: true}).fill('synthetic.reviewer');
    await admin.getByLabel('Password', {exact: true}).fill(password);
    await admin.getByLabel('Enter password again', {exact: true}).fill(password);
    await admin.getByRole('button', {name: 'Create account', exact: true}).click();
    await admin.waitForURL(url => url.pathname === '/admin/people/accounts/synthetic.reviewer');
    await login(reviewer, 'synthetic.reviewer');
    const slugs = [];
    for (const name of ['Synthetic Alpha', 'Synthetic Beta']) {
      await admin.goto(origin + '/matters/new');
      await admin.getByLabel('Matter name', {exact: true}).fill(name);
      await admin.getByRole('button', {name: 'Create matter', exact: true}).click();
      await admin.waitForURL(/\/matters\/[^/]+\/setup/);
      slugs.push(new URL(admin.url()).pathname.split('/')[2]);
    }
    assert.equal((await reviewer.goto(origin + '/matters/' + slugs[0] + '/home')).status(), 404);
    assert.equal((await reviewer.goto(origin + '/admin/groups')).status(), 403);
    await admin.goto(origin + '/admin/groups');
    for (const name of ['Alpha team', 'Beta team']) {
      await admin.getByLabel('Group name', {exact: true}).fill(name);
      await admin.getByRole('button', {name: 'Create group', exact: true}).click();
      await admin.getByRole('heading', {name, exact: true}).waitFor();
    }
    await admin.getByLabel('Add person to Alpha team', {exact: true}).selectOption({label: 'Synthetic Reviewer'});
    await admin.getByRole('button', {name: 'Add to Alpha team', exact: true}).click();
    await admin.getByRole('button', {name: 'Remove Synthetic Reviewer', exact: true}).waitFor();
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'team-groups-desktop.png'), fullPage: true, animations: 'disabled'});
    await admin.setViewportSize({width: 390, height: 844});
    await admin.reload();
    await admin.getByRole('heading', {name: 'Team groups', exact: true}).waitFor();
    assert.equal(await admin.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'team-groups-mobile.png'), fullPage: true, animations: 'disabled'});
    await admin.setViewportSize({width: 1440, height: 1000});
    for (const [i, name] of ['Alpha team', 'Beta team'].entries()) {
      await admin.goto(origin + '/matters/' + slugs[i] + '/setup');
      await admin.getByLabel('Grant a group access to this matter').selectOption({label: name});
      await admin.getByRole('button', {name: 'Grant group access', exact: true}).click();
      await admin.getByRole('button', {name: 'Remove ' + name + ' grant', exact: true}).waitFor();
    }
    assert.equal((await reviewer.goto(origin + '/matters/' + slugs[0] + '/home')).status(), 200);
    assert.equal((await reviewer.request.get(origin + '/matters/' + slugs[0] + '/notebook/export?format=markdown')).status(), 200);
    assert.equal((await reviewer.goto(origin + '/matters/' + slugs[1] + '/home')).status(), 404);
    await admin.goto(origin + '/matters/' + slugs[0] + '/setup');
    await admin.getByText('Group: Alpha team', {exact: true}).waitFor();
    await admin.getByLabel('Add a case team member').selectOption({label: 'Synthetic Reviewer'});
    await admin.getByRole('button', {name: 'Add member', exact: true}).click();
    await admin.getByText('Direct member; Group: Alpha team', {exact: true}).waitFor();
    await admin.screenshot({path: path.join(process.env.RECORDBENCH_QA_ARTIFACTS, 'matter-access-reasons.png'), fullPage: true, animations: 'disabled'});
    await admin.goto(origin + '/admin/groups');
    await admin.getByRole('button', {name: 'Remove Synthetic Reviewer', exact: true}).click();
    await admin.getByText('No members yet.').first().waitFor();
    assert.equal((await reviewer.goto(origin + '/matters/' + slugs[0] + '/home')).status(), 200);
    await admin.goto(origin + '/matters/' + slugs[0] + '/setup');
    await admin.getByRole('button', {name: 'Remove direct grant', exact: true}).click();
    await admin.getByText('Direct grant removed for Synthetic Reviewer; any group grants still apply', {exact: true}).waitFor();
    assert.equal((await reviewer.goto(origin + '/matters/' + slugs[0] + '/home')).status(), 404);
    assert.equal((await reviewer.request.get(origin + '/matters/' + slugs[0] + '/notebook/export?format=markdown')).status(), 404);
    console.log(JSON.stringify({result: 'passed', checks: ['two independent local sign-ins', 'two groups and matters', 'group-only access', 'foreign matter refusal', 'administrator separation', 'mixed access reasons', 'direct grant survives group removal', 'last-grant revocation in open session', 'export access and revocation', 'desktop and mobile layout']}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
