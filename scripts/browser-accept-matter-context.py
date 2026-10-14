#!/usr/bin/env python3
"""Synthetic durable context workflow through the existing pinned browser runner."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import urlparse

from fastapi.testclient import TestClient
import uvicorn
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

spec = importlib.util.spec_from_file_location('knowledge_browser', Path(__file__).with_name('browser-accept-matter-knowledge.py'))
knowledge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(knowledge)
from case_intelligence.matter_context import MatterContextService


def activate_context_action(driver, wait, element):
    """Activate once with native Enter, then wait for navigation to finish."""
    element.send_keys(Keys.ENTER)
    def detached(current):
        try:
            return EC.staleness_of(element)(current)
        except WebDriverException as exc:
            # Chrome can report an old document's node through its inspector
            # instead of Selenium's usual stale-element exception.
            if 'Node with given id does not belong to the document' not in exc.msg:
                raise
            return True
    wait.until(detached)
    wait.until(lambda _: driver.execute_script('return document.readyState') == 'complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Choose an empty output directory.')
    knowledge.isolate_environment()
    checks = []
    driver = None
    def record(message):
        checks.append(message)
        print(message, flush=True)
    with tempfile.TemporaryDirectory(prefix='recordbench-context-browser-') as temporary:
        runtime = Path(temporary)/'runtime'
        context = knowledge.seed(runtime)
        # Persist a note selection, stop all app writers, and open a new app below.
        with TestClient(knowledge.app_at(runtime)) as client:
            bench = client.app.state.workbench
            matter = bench.matter(context['slug'],knowledge.ACTOR)
            service = MatterContextService(bench.assertion_service(matter))
            note = bench.workspace.notebook_page(matter.matter_id,knowledge.ACTOR).items[0]
            selection,candidate = service.inspect(matter.matter_id,knowledge.ACTOR,kind='notebook_item',object_id=note.item_id)
            service.change(matter.matter_id,knowledge.ACTOR,expected_revision=0,action='add',kind='notebook_item',object_id=note.item_id,approval=candidate['approval'])
        app = knowledge.app_at(runtime)
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        path = f'/matters/{context["slug"]}/context'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning',access_log=False,ws='none'))
        thread = threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        thread.start()
        try:
            options = Options()
            options.binary_location = str(args.chrome_binary)
            for flag in ('--headless=new','--no-sandbox','--disable-dev-shm-usage','--window-size=1440,1100'):
                options.add_argument(flag)
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)),options=options)
            driver.set_page_load_timeout(30)
            wait = WebDriverWait(driver,20, ignored_exceptions=(StaleElementReferenceException,))
            wait.until(lambda _: server.started)
            def body():
                return driver.find_element(By.TAG_NAME,'body').text
            def button(label, root=None):
                root = root or driver
                return next(e for e in root.find_elements(By.TAG_NAME,'button') if e.text == label)
            def activate(element):
                activate_context_action(driver, wait, element)
            def inspect(kind, identifier):
                driver.get(base+path+f'?kind={kind}&object_id={identifier}')
            def no_overflow():
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            driver.get(base+path)
            assert 'Working note about Alex' in body() and 'Unchanged' in body()
            assert 'Saved selections change answers only when' in body()
            record('Restart retains approved selection and explains explicit opt-in; selection operations call no generator')
            for identifier in (context['person'],context['other']):
                inspect('entity',identifier)
                activate(button('Use as context'))
                assert 'Saved selection' in body()
            assert len(driver.find_elements(By.CSS_SELECTOR,'article[id^="selected-"]')) == 3
            assert context['person'] in body() and context['other'] in body()
            record('Two same-name identities are independently selected by stable ID; unavailable support stays labeled')
            driver.get(base+path)
            original_tab = driver.current_window_handle
            driver.switch_to.new_window('tab')
            driver.get(base+path)
            other = driver.find_element(By.ID,'selected-'+context['other'])
            activate(button('Move up',other))
            driver.switch_to.window(original_tab)
            activate(button('Clear selection'))
            assert 'Your unsaved choice' in body() and 'another tab' in body()
            assert len(driver.find_elements(By.CSS_SELECTOR,'article[id^="selected-"]')) == 3
            driver.refresh()
            assert len(driver.find_elements(By.CSS_SELECTOR,'article[id^="selected-"]')) == 3
            record('Concurrent tabs cannot overwrite a reorder; conflict retains the unsaved choice and current selection')
            bench = app.state.workbench
            matter = bench.matter(context['slug'],knowledge.ACTOR)
            entities = bench.entity_service(matter)
            entities.update(matter.matter_id,knowledge.ACTOR,context['person'],expected_revision=1,display_name='Alex Corrected')
            inspect('entity',context['person'])
            assert 'Review changes: version, record' in body()
            activate(button('Accept current basis'))
            assert 'Alex Corrected' in body()
            current = driver.find_element(By.ID,'selected-'+context['person'])
            activate(button('Remove from context',current))
            driver.refresh()
            assert not driver.find_elements(By.ID,'selected-'+context['person'])
            record('Source-record edits require explicit reapproval; explicit removal survives reload without re-add')
            inspect('entity',context['person'])
            source = driver.find_element(By.LINK_TEXT,'Open original passage')
            activate(source)
            wait.until(lambda _: driver.find_elements(By.ID,'support-pane'))
            activate(driver.find_element(By.LINK_TEXT,'Return to review context'))
            assert urlparse(driver.current_url).path == path
            driver.get(base+path+f'?kind=entity&object_id={context["person"]}')
            for _ in range(180):
                active = driver.switch_to.active_element
                if active.tag_name == 'button' and active.text == 'Use as context':
                    activate(active)
                    break
                active.send_keys(Keys.TAB)
            else:
                raise AssertionError('Approval control is not keyboard reachable')
            assert driver.find_element(By.ID,'selected-'+context['person'])
            record('Original passage returns to exact context inspection; native keyboard traversal and Enter save work')
            assertions = bench.assertion_service(matter)
            records, _ = assertions.list(matter.matter_id, knowledge.ACTOR)
            assertion_id = next(row['assertion_id'] for row in records if row['title'] == knowledge.TITLE)
            inspect('assertion', assertion_id)
            activate(button('Use as context'))
            entities.update(matter.matter_id, knowledge.ACTOR, context['person'],
                expected_revision=2, display_name='Alex Role Corrected', status='dismissed')
            inspect('assertion', assertion_id)
            assert 'Review changes: roles' in body()
            assert 'Recorded identity: Alex Example' in body() and 'Current identity: Alex Role Corrected' in body()
            assert 'Current human review: Dismissed' in body()
            activate(driver.find_element(By.LINK_TEXT, 'Inspect current role identity'))
            assert 'Alex Role Corrected' in body() and context['person'] in body()
            inspect('assertion', assertion_id)
            activate(button('Accept current basis'))
            for theme in ('dark', 'light','dusk'):
                inspect('assertion', assertion_id)
                Select(driver.find_element(By.ID,'appearance-theme')).select_by_value(theme)
                wait.until(lambda _: driver.find_element(By.TAG_NAME,'html').get_attribute('data-theme') == theme)
                no_overflow()
                driver.save_screenshot(str(output/f'context-{theme}-desktop.png'))
            record('Changed role identity retains recorded name and current review status; explicit approval and Dark/Light/Dusk inspection fit desktop')
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=False))
            inspect('assertion', assertion_id)
            no_overflow()
            assert 'Recorded identity: Alex Example' in body() and 'Current human review: Dismissed' in body()
            inspect('entity',context['other'])
            no_overflow()
            driver.save_screenshot(str(output/'context-dusk-mobile.png'))
            activate(button('Remove from context',driver.find_element(By.ID,'selected-'+context['other'])))
            no_overflow()
            assert not driver.find_elements(By.ID,'selected-'+context['other'])
            record('390-pixel mobile inspection preserves complete support labels and usable removal controls')
            # C uses a deterministic transport fixture, not a real model gate.
            from case_intelligence import generation
            from case_intelligence.generation import GroundedGenerationService, OpenAICompatibleGenerator
            class SyntheticClient(OpenAICompatibleGenerator):
                @property
                def available(self):
                    return True
            original_transport = generation._bounded_json_request
            def synthetic_transport(url, payload, **kwargs):
                if url.endswith('/tokenize'):
                    return dict(count=2400, max_model_len=8192, tokens=list(range(2400)))
                import re
                passages = re.findall(r'\[(S\d+)\] \[DOCUMENT\] [^\n]+\n([^\n]+)', payload['messages'][1]['content'])
                claims = [dict(text=text.split('. ')[0]+('.' if not text.split('. ')[0].endswith('.') else ''), evidence_ids=[identifier]) for identifier,text in passages[:2]]
                answer = dict(answerable=True,claims=claims,limitation=None,missing_information='')
                return dict(model='synthetic-browser-model',usage=dict(prompt_tokens=2400),choices=[dict(message=dict(content=json.dumps(answer)))])
            generation._bounded_json_request = synthetic_transport
            try:
                bench.generator = GroundedGenerationService(SyntheticClient('http://127.0.0.1:18080','synthetic-browser-model',disable_thinking=True))
                service = MatterContextService(bench.assertion_service(matter))
                selection, _ = service.inspect(matter.matter_id,knowledge.ACTOR)
                service.change(matter.matter_id,knowledge.ACTOR,expected_revision=selection['revision'],action='clear')
                selection,candidate = service.inspect(matter.matter_id,knowledge.ACTOR,kind='assertion',object_id=assertion_id)
                service.change(matter.matter_id,knowledge.ACTOR,expected_revision=selection['revision'],action='add',kind='assertion',object_id=assertion_id,approval=candidate['approval'])
                conversation = bench.workspace.create_conversation(matter.matter_id, 'Synthetic C new conversation', actor_id=knowledge.ACTOR)
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride',{})
                driver.get(base+f'/matters/{context["slug"]}?conversation={conversation.conversation_id}')
                driver.find_element(By.CSS_SELECTOR,'.composer-notebook-context > summary').send_keys(Keys.ENTER)
                checkbox = driver.find_element(By.NAME,'use_saved_context')
                assert not checkbox.is_selected()
                checkbox.send_keys(Keys.SPACE)
                question = driver.find_element(By.ID,'matter-question')
                question.send_keys('What do Morgan Sample and Riley Demo say about the parcel delivery?')
                driver.find_element(By.CSS_SELECTOR,'button[name=review_task][value=answer]').send_keys(Keys.ENTER)
                wait.until(lambda _: 'Context supplied for this answer' in body())
                assert 'Morgan Sample' in body() and 'Riley Demo' in body()
                assert not driver.find_element(By.NAME,'use_saved_context').is_selected()
                record('C is off by default; keyboard submission in a new conversation retains competing originals and a supplied-context notice')
                details = next(e for e in driver.find_elements(By.TAG_NAME,'summary') if e.text == 'Context supplied for this answer')
                details.send_keys(Keys.ENTER)
                activate(driver.find_element(By.LINK_TEXT,'Inspect as-submitted context, current changes and actual dispatch manifest'))
                assert 'As submitted' in body() and 'tokenization' in body() and 'generation' in body()
                no_overflow()
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=False))
                for detail in driver.find_elements(By.CSS_SELECTOR,'main details'):
                    detail.find_element(By.TAG_NAME,'summary').send_keys(Keys.ENTER)
                no_overflow()
                assert 'runtime window 8192' in body()
                driver.save_screenshot(str(output/'context-supplied-mobile.png'))
                activate(driver.find_elements(By.LINK_TEXT,'Open original passage')[0])
                assert driver.find_element(By.ID,'support-pane')
                record('C receipt exposes exact dispatch and budget, complete submitted groups and original navigation at 390 pixels; mocks do not qualify model quality')
            finally:
                generation._bounded_json_request = original_transport
            (output/'receipt.json').write_text(json.dumps(dict(synthetic_only=True,passed=True,checks=checks,
                browser=driver.capabilities.get('browserVersion'),screenshots=sorted(p.name for p in output.glob('*.png'))),indent=2))
        except Exception as exc:
            if driver:
                driver.save_screenshot(str(output/'failure.png'))
            (output/'receipt.json').write_text(json.dumps(dict(synthetic_only=True,passed=False,checks=checks,error=type(exc).__name__),indent=2))
            raise
        finally:
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
