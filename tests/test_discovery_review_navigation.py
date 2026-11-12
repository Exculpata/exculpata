"""Execute the shipped script with a dependency-free synthetic DOM."""
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'src/case_intelligence/static/discovery-review.js'
STUB = r'''
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(process.argv[1]), calls = {fetch: [], history: [], navigation: []}, handlers = {};
const links = Object.fromEntries(['next', 'previous', 'sort', 'clear', 'show'].map(kind => [kind,
  {href: '/production?page=2&q=synthetic&sort=name&dir=desc&edge=last&view=text#list',
   dataset: {reviewNavigation: '', reviewPage: kind}}]));
const viewInput = {value: new URLSearchParams(input.query || '').get('view') || ''};
const row = {dataset: {url: '/production?doc=one&q=synthetic', fragment: '/items/one', token: 'one'},
  classList: {toggle() {}}, setAttribute() {}, removeAttribute() {}, scrollIntoView() {}};
const panel = {contains: () => true, querySelectorAll: () => [],
  querySelector: () => ({dataset: {view: input.shown || 'text', token: 'one'}})};
const root = {querySelector(s) {
  if (s === '[data-review-panel]') return panel;
  if (s === '[data-review-row].selected') return row;
  if (s.includes('data-review-page')) return links[s.includes('previous') ? 'previous' : 'next'];
  return null;
}, querySelectorAll(s) {
  if (s === '[data-review-row]') return [row];
  if (s === '[data-review-navigation]') return Object.values(links);
  if (s === '[data-review-view-input]') return [viewInput];
  return [];
}, addEventListener(k, f) {handlers[k] = f;}};
global.document = {querySelector: () => root, addEventListener(k, f) {handlers[k] = f;}};
global.window = {location: {origin: 'http://synthetic.test', href: 'http://synthetic.test/production' + (input.query || ''),
  assign: url => calls.navigation.push(url)}};
global.history = {replaceState: (_, __, url) => calls.history.push(url)};
global.sessionStorage = {getItem: () => {if (input.storageError) throw Error('synthetic'); return input.stored || ''}, setItem() {}};
global.fetch = async url => {calls.fetch.push(url); if (input.fail) throw Error('synthetic');
  return {ok: true, text: async () => '<synthetic>'};};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
if (input.key) handlers.keydown({key: input.key, target: {closest: () => null}, preventDefault() {}});
if (input.click) handlers.click({target: {closest: s => s === '[data-review-row]' ? row : null}, preventDefault() {}});
if (input.unit) handlers.click({target: {closest: s => s === '[data-review-fragment]' ? {dataset: {reviewFragment: '/items/one?view=text&unit=2'}, href: '/production?doc=one&view=text&unit=2'} : null}, preventDefault() {}});
if (input.choose) handlers.click({target: {closest: s => s === '[data-review-fragment]' ?
  {dataset: {reviewView: input.choose, reviewFragment: '/items/one?view=' + input.choose}} : null}, preventDefault() {}});
if (input.navigate) {
  const link = links[input.navigate]; let prevented = false;
  handlers.click({target: {closest: s => ['[data-review-navigation]', '[data-review-page]'].includes(s) ? link : null},
    preventDefault() {prevented = true;}});
  if (!prevented) window.location.assign(link.href);
}
calls.filter = new URLSearchParams({q: 'new synthetic', sort: 'name', dir: 'desc', view: viewInput.value}).toString();
setImmediate(() => process.stdout.write(JSON.stringify(calls)));
'''


def run_dom(**values):
    result = subprocess.run(['node', '-e', STUB, json.dumps(values), str(SCRIPT)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('key', ['j', 'k'])
def test_keyboard_page_boundary_preserves_fields_view(key):
    calls = run_dom(stored='fields', key=key)
    assert 'view=fields' in calls['navigation'][-1]
    assert all(part in calls['navigation'][-1] for part in ['q=synthetic', 'sort=name', 'dir=desc', 'edge=last'])


def test_initial_panel_restores_session_view_unless_url_is_explicit():
    assert any('view=fields' in url for url in run_dom(stored='fields')['fetch'])
    assert not any('view=fields' in url for url in run_dom(stored='fields', query='?view=text')['fetch'])


def test_row_history_and_fetch_failure_fallback_preserve_view():
    calls = run_dom(stored='fields', click=True, fail=True)
    assert 'view=fields' in calls['history'][-1]
    assert 'view=fields' in calls['navigation'][-1]


def test_unavailable_item_view_does_not_erase_preference():
    calls = run_dom(stored='document', shown='text', key='j')
    assert 'view=document' in calls['navigation'][-1]



def test_text_unit_failure_preserves_view_and_list_state():
    calls = run_dom(query='?view=document&q=synthetic&sort=name&dir=desc&edge=last', unit=True, fail=True)
    assert all(part in calls['navigation'][-1] for part in
               ['view=document', 'q=synthetic', 'sort=name', 'dir=desc', 'edge=last', 'unit=2'])


def test_unavailable_session_storage_uses_explicit_view():
    assert 'view=fields' in run_dom(query='?view=fields', storageError=True, key='j')['navigation'][-1]


@pytest.mark.parametrize('navigate', ['next', 'previous', 'sort', 'clear', 'show'])
def test_deliberate_view_selection_survives_navigation_click(navigate):
    calls = run_dom(query='?view=text', choose='fields', navigate=navigate)
    assert 'view=fields' in calls['navigation'][-1]
    assert all(part in calls['navigation'][-1] for part in
               ['page=2', 'q=synthetic', 'sort=name', 'dir=desc', 'edge=last', '#list'])


@pytest.mark.parametrize('query', ['?view=text', ''])
def test_deliberate_view_selection_updates_filter_submission(query):
    calls = run_dom(query=query, choose='fields')
    assert all(part in calls['filter'] for part in ['view=fields', 'q=new+synthetic', 'sort=name', 'dir=desc'])
