#!/usr/bin/env python3
"""Evidence-connection browser acceptance in a disposable synthetic app."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'
ORIGINALS = {
    'Generated supporting account.txt': 'Morgan Sample says Alex Example delivered the red parcel to Cedar Depot on 03/04/2026.',
    'Generated competing account.txt': 'Riley Demo says Alex Example did not deliver the red parcel to Cedar Depot on 03/04/2026.',
    'Generated unrelated identity.txt': 'Alex Example, the unrelated museum volunteer, catalogued postcards.',
}
EVENT_TITLE = 'Disputed parcel delivery'
DATE_UNCERTAINTY = 'Day/month order and time zone are unresolved.'
TEXT_CONTRAST = """
const element=arguments[0];
const rgba=s=>{const c=s.match(/[\\d.]+/g).map(Number); if(c.length===3)c.push(1); return c;};
const over=(front,back)=>front.slice(0,3).map((v,i)=>v*front[3]+back[i]*(1-front[3])).concat(1);
const chain=[]; for(let node=element;node;node=node.parentElement) chain.unshift(node);
const paint=(index,back)=>{
  const style=getComputedStyle(chain[index]), surface=over(rgba(style.backgroundColor),back);
  const pair=index+1<chain.length ? paint(index+1,surface) : [over(rgba(style.color),surface),surface];
  return pair.map(color=>over(color.slice(0,3).concat(Number(style.opacity)),back));
};
const [foreground,background]=paint(0,[255,255,255,1]);
const lum=color=>color.slice(0,3).map(value=>{value/=255;return value<=.04045?value/12.92:((value+.055)/1.055)**2.4;})
  .reduce((sum,value,index)=>sum+value*[.2126,.7152,.0722][index],0);
const a=lum(foreground), b=lum(background);
return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
"""


def app_at(runtime):
    return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='test')


def seed(runtime):
    """Set up saved knowledge, then stop its writers before browser startup."""
    with TestClient(app_at(runtime)) as client:
        response = client.post('/matters', data=dict(name='Generated connection review',
            descriptor='Synthetic source-backed browser acceptance'), follow_redirects=False)
        assert response.status_code == 303
        slug = response.headers['location'].split('/')[2]
        response = client.post(f'/matters/{slug}/uploads', files=[
            ('files', (name, content.encode(), 'text/plain')) for name, content in ORIGINALS.items()])
        assert response.status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        tokens = {document.display_name: bench._support_token(bench._candidate(
            matter, document, document.parsed_units()[0], 1))
            for document in bench.source_store(matter).documents.values()}
        entities = bench.entity_service(matter)
        supporting = tokens['Generated supporting account.txt']
        competing = tokens['Generated competing account.txt']
        person = entities.create(matter.matter_id, ACTOR, display_name='Alex Example', support=supporting)
        place = entities.create(matter.matter_id, ACTOR, display_name='Cedar Depot',
            entity_type='place', support=supporting)
        unrelated = entities.create(matter.matter_id, ACTOR, display_name='Alex Example',
            support=tokens['Generated unrelated identity.txt'])
        assertions = bench.assertion_service(matter)
        event = assertions.create(matter.matter_id, ACTOR, support=supporting,
            attributed_to='Morgan Sample, supporting account',
            roles=[dict(entity_id=person['entity_id'], expected_revision=1, role='subject'),
                   dict(entity_id=place['entity_id'], expected_revision=1, role='location')],
            record_type='event', title=EVENT_TITLE,
            statement='Morgan reports a delivery; Riley denies that delivery. Review has not resolved the disagreement.',
            raw_date='03/04/2026', date_uncertainty=DATE_UNCERTAINTY,
            sort_date='', status='disputed')
        assertions.attach(matter.matter_id, ACTOR, event['assertion_id'], expected_revision=1,
            support=competing, stance='competing', attributed_to='Riley Demo, competing account')
        return dict(slug=slug, person=person['entity_id'], place=place['entity_id'],
            unrelated=unrelated['entity_id'], event=event['assertion_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    isolate_environment()
    checks = []
    driver = None

    def record(message):
        checks.append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-connections-browser-") as temporary:
        runtime = Path(temporary).resolve() / 'runtime'
        context = seed(runtime)
        app = app_at(runtime)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        prefix = f'/matters/{context["slug"]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            options = Options()
            options.binary_location = str(args.chrome_binary)
            for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1100'):
                options.add_argument(flag)
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)

            def body():
                return driver.find_element(By.TAG_NAME, 'body').text

            def detached(element):
                def check(current):
                    try:
                        return EC.staleness_of(element)(current)
                    except WebDriverException as exc:
                        if 'Node with given id does not belong to the document' not in exc.msg:
                            raise
                        return True
                return check

            def click(element):
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                positions = []

                def stable_pointer_target(current):
                    position = current.execute_script('''const element=arguments[0], box=element.getBoundingClientRect();
                        const hit=document.elementFromPoint(box.left+box.width/2,box.top+box.height/2);
                        return box.top>=0 && box.bottom<=innerHeight && hit && element.contains(hit)
                          ? [box.left,box.top,box.width,box.height] : null;''', element)
                    positions.append(position)
                    return position is not None and len(positions) > 1 and position == positions[-2]

                # Source drawers animate into position on narrow screens. Wait
                # for a stable real pointer target, without dispatching JS clicks.
                wait.until(stable_pointer_target)
                element.click()
                wait.until(detached(element))
                wait.until(lambda current: current.execute_script('return document.readyState') == 'complete')

            def graph_ready(identity):
                wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, 'h1'), 'Evidence connections'))
                assert parse_qs(urlparse(driver.current_url).query)['entity_id'] == [identity]
                assert EVENT_TITLE in body() and DATE_UNCERTAINTY in body() and '03/04/2026' in body()
                assert 'Human review: Disputed' in body()

            def keyboard_link(predicate):
                for _ in range(100):
                    active = driver.switch_to.active_element
                    if active.tag_name == 'a' and predicate(active):
                        active.send_keys(Keys.ENTER)
                        wait.until(detached(active))
                        return
                    active.send_keys(Keys.TAB)
                raise AssertionError('Keyboard traversal did not reach the expected link')

            def open_accounts():
                for details in driver.find_elements(By.CSS_SELECTOR, 'details.graph-account'):
                    if not details.get_attribute('open'):
                        summary = details.find_element(By.TAG_NAME, 'summary')
                        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
                        summary.click()

            def assert_no_page_overflow():
                dimensions = driver.execute_script('return {page:document.documentElement.scrollWidth, viewport:innerWidth}')
                assert dimensions['page'] <= dimensions['viewport'], dimensions

            def readable_cards():
                for selector in ('.graph-focus h2', '.graph-focus p', '.graph-evidence-record > p',
                                 '.graph-account blockquote'):
                    for element in driver.find_elements(By.CSS_SELECTOR, selector):
                        if element.is_displayed():
                            ratio = driver.execute_script(TEXT_CONTRAST, element)
                            assert ratio >= 4.5, f'{selector}: contrast {ratio:.2f}:1'

            navigate_to(driver, base + prefix + '/entities/' + context['person'], diagnostics=args.output)
            keyboard_link(lambda element: element.text == 'Explore connections')
            graph_ready(context['person'])
            # Entry omits the default page; original links expose the canonical
            # current context including page=1 and its prior-review destination.
            original_href = driver.find_element(By.CSS_SELECTOR, 'a.graph-original-link').get_attribute('href')
            graph_url = base + parse_qs(urlparse(original_href).query)['entity_return_to'][0]
            assert context['unrelated'] not in driver.page_source
            record('Keyboard entry from the entity opens source-backed connections, with disputed review status and the raw uncertain date')

            navigate_to(driver, graph_url, diagnostics=args.output)
            keyboard_link(lambda element: '/connections?' in (element.get_attribute('href') or '')
                and parse_qs(urlparse(element.get_attribute('href')).query).get('entity_id') == [context['place']])
            graph_ready(context['place'])
            assert 'Cedar Depot' in driver.find_element(By.ID, 'focus-heading').text
            record('A neighboring graph link is reachable with Tab and Enter and refocuses the same evidence neighborhood')

            for stance, expected in (('supporting', ORIGINALS['Generated supporting account.txt']),
                                     ('competing', ORIGINALS['Generated competing account.txt'])):
                navigate_to(driver, graph_url, diagnostics=args.output)
                account = driver.find_element(By.CSS_SELECTOR, '.graph-account-' + stance)
                summary = account.find_element(By.TAG_NAME, 'summary')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
                summary.click()
                assert expected in account.text
                click(account.find_element(By.LINK_TEXT, 'Open original passage'))
                pane = wait.until(EC.visibility_of_element_located((By.ID, 'support-pane')))
                assert expected in pane.text
                if stance == 'competing':
                    click(driver.find_element(By.CSS_SELECTOR, 'a.support-header-open'))
                    assert expected in body()
                    return_link = driver.find_element(By.LINK_TEXT, 'Return to review context')
                else:
                    return_link = pane.find_element(By.CSS_SELECTOR, 'a.support-return-context')
                click(return_link)
                graph_ready(context['person'])
                assert driver.current_url == graph_url
            record('Supporting and competing accounts open exact original passages; the source pane and full reader return to the same graph context')

            click(driver.find_element(By.LINK_TEXT, 'Find more originals'))
            query = parse_qs(urlparse(driver.current_url).query)
            assert query.get('phrase') == ['Alex Example'] and query.get('search') == ['true']
            assert 'Alex Example' in body()
            record('Find more originals opens the selected name through the literal phrase search control')

            from browser_appearance import APPEARANCES, select_appearance
            for theme, accent in APPEARANCES:
                label = f'{theme}-{accent}'
                navigate_to(driver, graph_url, diagnostics=args.output)
                select_appearance(driver, wait, theme, accent)
                graph_ready(context['person'])
                assert_no_page_overflow()
                driver.execute_script('window.scrollTo(0,0)')
                driver.save_screenshot(str(output / f'connections-{label}-desktop.png'))
                driver.find_element(By.CSS_SELECTOR, '.graph-diagram-panel').screenshot(str(output / f'connections-{label}-diagram.png'))
                open_accounts()
                driver.find_element(By.ID, 'connection-evidence').screenshot(str(output / f'connections-{label}-evidence.png'))
                readable_cards()
                diagram_size = driver.find_element(By.CSS_SELECTOR, 'svg.evidence-graph').rect
                assert diagram_size['width'] >= 1000 and diagram_size['height'] >= 200, diagram_size
            record('All six mode/accent combinations preserve the diagram, labeled roles, competing accounts, and original-source actions')

            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(
                width=390, height=844, deviceScaleFactor=1, mobile=False))
            navigate_to(driver, graph_url, diagnostics=args.output)
            graph_ready(context['person'])
            assert_no_page_overflow()
            diagram = driver.find_element(By.CSS_SELECTOR, '.graph-diagram-scroll')
            assert driver.execute_script('return arguments[0].scrollWidth > arguments[0].clientWidth', diagram)
            driver.execute_script('window.scrollTo(0,0)')
            driver.save_screenshot(str(output / 'connections-light-amber-mobile.png'))
            open_accounts()
            evidence = driver.find_element(By.ID, 'connection-evidence')
            driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", evidence)
            assert_no_page_overflow()
            driver.save_screenshot(str(output / 'connections-light-amber-mobile-evidence.png'))
            click(driver.find_element(By.CSS_SELECTOR, '.graph-account-competing a.graph-original-link'))
            pane = wait.until(EC.visibility_of_element_located((By.ID, 'support-pane')))
            assert ORIGINALS['Generated competing account.txt'] in pane.text
            assert_no_page_overflow()
            return_link = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR,
                '#support-pane a.support-return-context')))
            driver.save_screenshot(str(output / 'connections-mobile-source-return.png'))
            click(return_link)
            graph_ready(context['person'])
            assert driver.current_url == graph_url
            record('At 390 pixels only the diagram scrolls horizontally; evidence and original-source return remain usable without document overflow')

            (output / 'receipt.json').write_text(json.dumps(dict(synthetic=True, success=True,
                checks=checks, browser=driver.capabilities.get('browserVersion'),
                screenshots=sorted(path.name for path in output.glob('*.png')),
                scope='Read-only connections over generated saved records. No model, installed node, external source, or private deployment used.'), indent=2))
        except Exception as exc:
            if driver is not None:
                driver.save_screenshot(str(output / 'failure.png'))
            (output / 'receipt.json').write_text(json.dumps(dict(synthetic=True, success=False,
                checks=checks, error=type(exc).__name__), indent=2))
            raise
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
