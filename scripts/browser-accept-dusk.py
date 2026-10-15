#!/usr/bin/env python3
"""Computed contrast on real source controls, using only disposable synthetic data."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

# Composite every ancestor background and opacity, including transparent children.
CONTRAST = """
const el = arguments[0], pseudo = arguments[1], property = arguments[2];
const rgba = s => {const c=s.match(/[\\d.]+/g).map(Number); if(c.length===3)c.push(1); return c;};
const over = (f,b) => f.slice(0,3).map((v,i) => v*f[3]+b[i]*(1-f[3])).concat(1);
const chain = []; for(let n=el;n;n=n.parentElement) chain.unshift(n);
if(arguments[3]) chain.pop();
let bg=[255,255,255,1], fg;
const paint = (i, back) => {
  const s=getComputedStyle(chain[i]);
  const surface=over(rgba(s.backgroundColor),back);
  let pair;
  if(i+1<chain.length) pair=paint(i+1,surface);
  else {
    const style=getComputedStyle(el,pseudo);
    const ink=rgba(style[property]);
    if(pseudo) ink[3]*=Number(style.opacity);
    pair=[over(ink,surface),surface];
  }
  return pair.map(c => over(c.slice(0,3).concat(Number(s.opacity)),back));
};
[fg,bg]=paint(0,bg);
const lum = c => c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4})
  .reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
const a=lum(fg),b=lum(bg);
return {ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),fg,bg};
"""


def wait_for_light_snapshot(wait, snapshot, light):
    actual = None

    def matches(_):
        nonlocal actual
        actual = snapshot()
        return actual == light

    try:
        wait.until(matches)
    except TimeoutException as exc:
        if len(actual) != len(light):
            difference = f'element count: expected {len(light)}, got {len(actual)}'
        else:
            properties = ('color', 'backgroundColor', 'opacity', 'borderColor')
            difference = next(
                f'element {index} {property}: expected {expected!r}, got {observed!r}'
                for index, (expected_row, actual_row) in enumerate(zip(light, actual))
                for property, expected, observed in zip(properties, expected_row, actual_row)
                if expected != observed
            )
        raise AssertionError(f'Light computed styles did not settle: {difference}') from exc


def main():
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_only': True, 'passed': False, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='recordbench-dusk-') as temporary:
        app = create_workbench_app(Path(temporary) / 'runtime', generator=UnavailableGenerator(), auth_mode='test')
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic theme acceptance', 'Generated readability fixtures', 'development-taylor-morgan')
        store = bench.source_store(matter)
        documents = []
        for index in range(26):
            document, _ = store.store_stream(f'synthetic-ledger-{index:02}.txt', 'text/plain',
                io.BytesIO(f'Synthetic amber bicycle ledger {index}.'.encode()))
            documents.append(document)
        bench._sync_source_catalog(matter, documents)
        collection = bench.workspace.create_source_collection(matter.matter_id, 'Synthetic collection',
            'upload', 'development-taylor-morgan')
        bench.workspace.register_source_organizations(matter.matter_id, collection.collection_id,
            tuple((d.document_id, d.display_name) for d in documents), 'development-taylor-morgan')
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--window-size=1440,1100'):
            options.add_argument(flag)
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            wait = WebDriverWait(driver, 30)
            wait.until(lambda _: server.started)
            driver.get(base + f'/matters/{matter.slug}/setup?view=list&page_size=25&q=synthetic')
            def one(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)

            def check(selector, pseudo=None, minimum=4.5, property='color', adjacent=False):
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                visible = [e for e in elements if e.is_displayed()]
                assert visible, f'Missing visible contrast target: {selector}'
                for element in visible:
                    result = driver.execute_script(CONTRAST, element, pseudo, property, adjacent)
                    assert result['ratio'] >= minimum, f'{selector} {pseudo}: {result}'
                report['checks'].append(f'{selector} {pseudo or property}{' adjacent' if adjacent else ''}: >= {minimum}:1')

            def snapshot():
                return driver.execute_script('''return [...document.querySelectorAll('.source-stat-card, .source-stat-card span, .source-library-filters input[type=search], .source-library-filters select, .source-bulk-toolbar, .button:disabled')].map(e=>{const s=getComputedStyle(e);return [s.color,s.backgroundColor,s.opacity,s.borderColor]})''')

            # Guard the measurement itself against RGB/alpha and inherited-opacity errors.
            probe = driver.execute_script("""
                const parent=document.createElement('div');
                parent.style.cssText='background:white;color:black';
                const child=document.createElement('span');
                child.style.opacity='.5'; child.textContent='Synthetic contrast probe';
                parent.append(child); document.body.append(parent); return child;
            """)
            measured = driver.execute_script(CONTRAST, probe, None, 'color', False)
            assert abs(measured['ratio'] - 3.976653) < .001, measured
            driver.execute_script('arguments[0].parentElement.remove()', probe)
            report['checks'].append('Contrast calculation composites transparent text and ancestor backgrounds')
            one('[data-assistant-collapse]').click()
            # Dark is the default appearance. Capture Light only after it is applied.
            Select(one('[data-theme-picker]')).select_by_value('light')
            wait.until(lambda _: one('html').get_attribute('data-theme') == 'light')
            light = snapshot()
            def outside_disabled_style():
                return driver.execute_script("""
                    const button=document.createElement('button');
                    button.className='button button-danger'; button.disabled=true;
                    button.textContent='Synthetic unrelated disabled action';
                    document.body.append(button);
                    const s=getComputedStyle(button);
                    const result=[s.backgroundColor,s.color,s.opacity,s.borderColor];
                    button.remove(); return result;
                """)
            from browser_appearance import APPEARANCES, select_appearance
            for mode, accent in APPEARANCES:
                select_appearance(driver, wait, mode, accent)
                for selector in ('.source-stat-card strong', '.source-stat-card span',
                                 '.source-library-filters input[type=search]', '.source-library-filters select',
                                 '.source-bulk-toolbar strong', '.source-bulk-toolbar select',
                                 '[data-source-bulk-submit]:disabled'):
                    check(selector)
                outside = outside_disabled_style()
                # Disabled actions remain readable on their own theme surface.
                from exculpata_theme_contrast import cr
                def hex_color(value):
                    return '#' + ''.join(f'{int(v):02X}' for v in value.removeprefix('rgb(').removesuffix(')').split(','))
                assert cr(hex_color(outside[0]), hex_color(outside[1])) >= 4.5
                assert float(outside[2]) == 1
            select_appearance(driver, wait, 'dark', 'amber')
            report['checks'].append('All six palettes keep source controls and disabled actions readable')
            one('.source-status-overview').screenshot(str(output / 'accent-status.png'))
            one('.source-library-filters').screenshot(str(output / 'accent-filters.png'))
            one('.source-bulk-toolbar').screenshot(str(output / 'accent-bulk.png'))
            for selector in ('.source-stat-card strong', '.source-stat-card span', '.source-library-filters input[type=search]',
                             '.source-library-filters select', '.source-library-filters > a',
                             '.source-bulk-toolbar strong', '.source-bulk-toolbar select',
                             '[data-source-bulk-submit]:disabled', '.source-pagination .disabled'):
                check(selector)
            check('.source-library-filters input[type=search]', '::placeholder')
            driver.get(base + f'/matters/{matter.slug}/setup')
            check('.source-chip span')
            check('.source-chip small')
            driver.get(base + f'/matters/{matter.slug}/setup?view=list&page_size=25&q=synthetic')
            card = one('.source-stat-card.ready')
            ActionChains(driver).move_to_element(card).perform()
            check('.source-stat-card.ready span')
            card.click()
            wait.until(lambda _: one('.source-stat-card.ready').get_attribute('class').endswith('selected'))
            check('.source-stat-card.ready.selected span')
            driver.get(base + f'/matters/{matter.slug}/setup?view=list&page_size=25&q=synthetic')
            # Keyboard focus must be visible on links, inputs, selects and buttons.
            for selector in ('.source-stat-card', '.source-library-filters input[type=search]', '.source-library-filters select', '.source-library-filters button'):
                target = one(selector)
                driver.execute_script('arguments[0].focus()', target)
                target.send_keys(Keys.TAB)
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.TAB).key_up(Keys.SHIFT).perform()
                assert driver.execute_script('return arguments[0]===document.activeElement && arguments[0].matches(":focus-visible")', target)
                style = driver.execute_script('const s=getComputedStyle(arguments[0]);return [s.outlineStyle,parseFloat(s.outlineWidth)]', target)
                assert style[0] == 'solid' and style[1] >= 2, style
                check(selector, minimum=3, property='outlineColor')
                check(selector, minimum=3, property='outlineColor', adjacent=True)
            one('.source-library-filters').screenshot(str(output / 'accent-focus.png'))
            select_all = one('[data-source-select-all]')
            driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"})', select_all)
            select_all.click()
            Select(one('[data-source-bulk-action]')).select_by_value('create_set')
            name = one('[data-bulk-set-name]')
            wait.until(lambda _: name.is_displayed())
            check('[data-bulk-set-name]', '::placeholder')
            for control in (name, one('[data-source-bulk-action]')):
                driver.execute_script('arguments[0].disabled=true', control)
                check('[data-bulk-set-name]' if control == name else '[data-source-bulk-action]')
                driver.execute_script('arguments[0].disabled=false', control)
            name.send_keys('Synthetic focused set')
            wait.until(lambda _: one('[data-source-bulk-submit]').is_enabled())
            check('[data-bulk-set-name]')
            check('[data-source-bulk-submit]')
            one('.source-bulk-toolbar').screenshot(str(output / 'accent-bulk-active.png'))
            driver.set_window_size(430, 1000)
            wait.until(lambda _: driver.execute_script('return document.querySelector("[data-matter-rail]").getBoundingClientRect().right <= 0'))
            driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"})', one('.source-library-filters'))
            driver.save_screenshot(str(output / 'accent-mobile.png'))
            check('.source-library-filters input[type=search]')
            driver.set_window_size(1440, 1100)
            driver.get(base + f'/matters/{matter.slug}/setup?view=list&page_size=25&q=synthetic')
            select_appearance(driver, wait, 'light', 'cyan')
            wait_for_light_snapshot(wait, snapshot, light)
            report['checks'].append('Light computed styles preserved after theme round trip')
            report['passed'] = True
        finally:
            if driver:
                if not report['passed']:
                    driver.save_screenshot(str(output / 'failure.png'))
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            (output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
