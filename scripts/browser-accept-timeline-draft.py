#!/usr/bin/env python3
"""Desktop/mobile timeline acceptance against a disposable synthetic app."""
from browser_navigation import navigate_to

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from fastapi.testclient import TestClient
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.workbench import create_workbench_app
from case_intelligence.generation import UnavailableGenerator


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary',type=Path,required=True)
    parser.add_argument('--chromedriver',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Choose a new or empty output directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    isolate_environment()
    checks=[]
    driver=None
    with tempfile.TemporaryDirectory(prefix='exculpata-timeline-browser-') as temporary:
        runtime=Path(temporary).resolve()/'runtime'
        app=create_workbench_app(runtime,auth_mode='test',generator=UnavailableGenerator())
        with TestClient(app) as client:
            response=client.post('/matters',data={'name':'Synthetic timeline browser','descriptor':'Synthetic source chronology'},follow_redirects=False)
            assert response.status_code==303
            slug=response.headers['location'].split('/')[2]
            documents={'first.txt':'Alex Example delivered the parcel at Cedar Depot on April 18, 2034.',
                       'second.txt':'Alex Example delivered the parcel at Cedar Depot on April 19, 2034.',
                       'undated.txt':'The next morning Alex Example inspected the parcel.'}
            assert client.post(f'/matters/{slug}/uploads',files=[('files',(name,text.encode(),'text/plain')) for name,text in documents.items()]).status_code==200
            assert client.post(f'/matters/{slug}/entities/actions',data={'action':'create','display_name':'Alex Example','entity_type':'person'},follow_redirects=False).status_code==303
        app=create_workbench_app(runtime,auth_mode='test',generator=UnavailableGenerator())
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False,ws='none'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        thread.start()
        try:
            options=Options()
            options.binary_location=str(args.chrome_binary)
            for flag in ('--headless=new','--no-sandbox','--disable-dev-shm-usage'):
                options.add_argument(flag)
            driver=webdriver.Chrome(service=Service(str(args.chromedriver)),options=options)
            wait=WebDriverWait(driver,20)
            wait.until(lambda _:server.started)
            for width in (1440,390):
                driver.set_window_size(width,1000)
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width':width,'height':1000,'deviceScaleFactor':1,'mobile':width<=480})
                navigate_to(driver, base+f'/matters/{slug}/timeline/draft', diagnostics=args.output)
                wait.until(lambda d:d.find_element(By.TAG_NAME,'h1').text=='Draft timeline')
                assert 'for attorney review' in driver.find_element(By.TAG_NAME,'body').text
                checks.append(f'{width}: review banner visible')
                assert len(driver.find_elements(By.CSS_SELECTOR,'.timeline-conflict-pair .timeline-entry'))==2
                checks.append(f'{width}: both conflicting dates visible')
                assert 'The next morning' in driver.find_element(By.TAG_NAME,'body').text
                checks.append(f'{width}: unresolved mention visible')
                assert driver.execute_script('return innerWidth') == width
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
                if width <= 480:
                    cards=driver.find_elements(By.CSS_SELECTOR,'.timeline-conflict-pair .timeline-entry')
                    assert cards[1].rect['y'] >= cards[0].rect['y']+cards[0].rect['height']
                checks.append(f'{width}: no horizontal overflow')
                citation=driver.find_element(By.CSS_SELECTOR,'.cited-context summary')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",citation)
                citation.click()
                assert driver.find_element(By.CSS_SELECTOR,'.cited-context-excerpt').is_displayed()
                checks.append(f'{width}: citation drawer opens')
                driver.save_screenshot(str(args.output/f'timeline-{width}.png'))
                link=driver.find_element(By.CSS_SELECTOR,'.timeline-source')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",link)
                link.click()
                wait.until(lambda d:'support=' in d.current_url)
                checks.append(f'{width}: original passage opens')
            navigate_to(driver, base+f'/matters/{slug}/timeline/draft', diagnostics=args.output)
            form=driver.find_element(By.CSS_SELECTOR,'.timeline-promote')
            Select(form.find_element(By.NAME,'entity_selection')).select_by_index(1)
            button=form.find_element(By.TAG_NAME,'button')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",button)
            button.click()
            wait.until(lambda d:'/assertions/' in d.current_url)
            assert 'Needs Review' in driver.find_element(By.TAG_NAME,'body').text
            checks.append('Promotion creates an event needing review')
            (args.output/'receipt.json').write_text(json.dumps({'checks':checks,'passed':True,'synthetic_only':True},indent=2)+'\n')
            print(json.dumps({'checks':len(checks),'passed':True}))
        finally:
            if driver: driver.quit()
            server.should_exit=True
            thread.join(timeout=10)
            listener.close()

if __name__=='__main__':
    main()
