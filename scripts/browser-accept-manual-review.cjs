#!/usr/bin/env node
// Synthetic manual-review acceptance with deterministic answers/transcription and a generated video.
// Usage: node scripts/browser-accept-manual-review.cjs /path/to/chromium /tmp/fresh-output
// Focused transport regressions: --refresh-only [--refresh-group=terminal|context|cancel|switch|ownership]
// Reproduce an older frontend without changing files: --baseline-js=REV (focused mode).
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
import json, socket, tempfile, sys, subprocess, time
from pathlib import Path
sys.path.insert(0, 'scripts')
from synthetic_browser_environment import isolate_environment
isolate_environment()
import uvicorn
from pytest import MonkeyPatch
from tests.test_saved_answer_report_support import cedar
with tempfile.TemporaryDirectory() as temporary:
    fixture = cedar.__wrapped__(Path(temporary), MonkeyPatch())
    client, bench, matter, documents = next(fixture)
    # The deterministic processor fixture accepts WAV only; its transcription
    # response is deliberately reused for generated video, not speech recognition.
    fixture_submit = bench.media.processor.submit
    bench.media.processor.submit = lambda owner, source, media_type, source_sha256: fixture_submit(owner,source,'audio/wav',source_sha256)
    video_path = Path(temporary) / 'synthetic-video.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=640x360:r=10:d=8','-f','lavfi','-i','anullsrc=r=8000:cl=mono','-t','8','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(video_path)],check=True)
    response = client.post(f'/matters/{matter.slug}/uploads',files=[('files',('Synthetic blue training video.mp4',video_path.read_bytes(),'video/mp4'))],follow_redirects=False)
    assert response.status_code == 303
    deadline = time.monotonic()+15
    continued = set()
    while time.monotonic()<deadline:
        video = next((doc for doc in bench.source_store(matter).documents.values() if doc.media_type=='video/mp4'),None)
        if video and video.state=='ready': break
        if video and video.state=='needs_review':
            job=bench.workspace.media_job(matter.matter_id,video.document_id,video.version_id)
            inspection=job.preflight.get('inspection_id') if job else None
            if inspection and inspection not in continued:
                client.post(f'/matters/{matter.slug}/sources/{bench.source_store(matter).action_token(video)}/recording-check',data=dict(action='continue',inspection_id=inspection),follow_redirects=False)
                continued.add(inspection)
        time.sleep(.02)
    assert video and video.state=='ready', [(doc.media_type,doc.state,doc.message) for doc in bench.source_store(matter).documents.values()]
    documents['video']=video
    conversation = bench.workspace.get_conversation(matter.matter_id)
    from tests.test_saved_answer_report_support import inspection_pdf
    response = client.post(f'/matters/{matter.slug}/uploads',files=[('files',('Synthetic extended review observation with a deliberately long descriptive filename for the Cedar training exercise.pdf',inspection_pdf()+bytes([10]),'application/pdf'))],follow_redirects=False)
    assert response.status_code==303
    pdf=next(doc for doc in bench.source_store(matter).documents.values() if doc.display_name.startswith('Synthetic extended'))
    documents['pdf']=pdf
    citation = bench._citation(matter, bench._candidate(matter, pdf, pdf.parsed_units()[0], 1))
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'user', 'What did the synthetic gauge show?')
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'assistant', 'Synthetic gauge observation', {'kind':'generated','claims':[{'text':'The synthetic gauge showed eighteen units.','citations':[bench._saved_answer_citation_payload(citation)]}]})
    alternate = bench.workspace.create_conversation(matter.matter_id, 'Synthetic alternate history', actor_id='development-taylor-morgan')
    bench.workspace.append_message(matter.matter_id, alternate.conversation_id, 'assistant', 'Historical synthetic alternate answer.')
    prefix = f'/matters/{matter.slug}'
    paths = {kind:f'{prefix}/sources/{bench.source_store(matter).action_token(doc)}?browse=sort%3Dname&unit=1&start_ms=1000&conversation={conversation.conversation_id}' for kind, doc in documents.items()}
    @client.app.get('/synthetic-note-evidence/{item_id}')
    def note_evidence(item_id: str):
        item=bench.workspace.notebook_item(matter.matter_id,'development-taylor-morgan',item_id)
        refs=bench.workspace.notebook_references(matter.matter_id,'development-taylor-morgan',item_id)
        support=bench.support(matter,refs[0].support_token)
        return dict(item_id=item.item_id,body=item.body,origin=item.origin,status=item.status,
            source_version_id=refs[0].source_version_id,document_id=refs[0].document_id,
            location=refs[0].location,start_ms=support.start_ms,end_ms=support.end_ms,
            total=len(bench.workspace.all_notebook_items(matter.matter_id,'development-taylor-morgan')))
    listener = socket.socket()
    listener.bind(('127.0.0.1',0))
    print(json.dumps(dict(base=f'http://127.0.0.1:{listener.getsockname()[1]}',reader=paths['pdf'],media=paths['video'],audio=paths['transcript'],matter=prefix,conversation=conversation.conversation_id,alternate=alternate.conversation_id,video_id=video.document_id,video_version=video.version_id)),flush=True)
    try:
        uvicorn.Server(uvicorn.Config(client.app,lifespan='off',log_level='warning')).run(sockets=[listener])
    finally:
        fixture.close()
`], { cwd: root, stdio: ['ignore', 'pipe', 'inherit'] });
let browser, activePage;
const deadline = setTimeout(() => {
  console.error('Synthetic browser acceptance exceeded 90 seconds.');
  server.kill('SIGTERM');
  if (browser) browser.close();
  process.exitCode = 1;
}, 90000);
const baselineJsRevision=process.argv.find(value=>value.startsWith('--baseline-js='))?.split('=')[1] || null;
const checks = [];
const observations = {};
const revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const dirty = Boolean(execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim());
// Isolated deterministic transport fixtures exercise browser ownership only;
// normal request persistence and authorization remain covered by route tests.
async function refreshRegressions(browser, fixture) {
  const group = process.argv.find(value => value.startsWith('--refresh-group='))?.split('=')[1];
  const baseline=baselineJsRevision;
  const oldScript = baseline ? execFileSync('git', ['show', `${baseline}:src/case_intelligence/static/case-intelligence.js`], {cwd:root,encoding:'utf8'}) : null;
  let sequence=0;
  const fresh = async () => {
    const ctx=await browser.newContext({viewport:{width:1440,height:1000}});
    if(oldScript)await ctx.route('**/static/case-intelligence.js*',route=>route.fulfill({status:200,contentType:'application/javascript',body:oldScript}));
    // Accelerate only the assistant's retry/poll intervals in isolated fixtures.
    await ctx.addInitScript(() => {const timer=window.setTimeout;window.setTimeout=(fn,delay,...args)=>timer(fn,[1000,3500].includes(delay)?80:delay,...args);});
    const p=await ctx.newPage();activePage=p;
    await p.goto(fixture.base+fixture.reader);
    const expand=p.locator('[data-assistant-expand]');if(await expand.isVisible())await expand.click();
    await p.locator('[data-assistant-dock][data-bound=true]').waitFor();
    return {ctx,p};
  };
  const heldTerminal = async (p,state,outcome='html') => {
    const statusUrl=`${fixture.matter}/answer-jobs/synthetic-refresh-${++sequence}`;
    let arrived,release,finished;
    const arrival=new Promise(r=>arrived=r),hold=new Promise(r=>release=r),done=new Promise(r=>finished=r);
    await p.route(`**${statusUrl}`,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state,status_url:statusUrl,can_cancel:false})}));
    await p.route('**/assistant?**',async route=>{
      const response=outcome==='html'?await route.fetch():null;
      arrived();await hold;
      try {
        if(outcome==='http')await route.fulfill({status:503,contentType:'text/plain',body:'Synthetic history unavailable'});
        else if(outcome==='network')await route.abort('failed');
        else await route.fulfill({response});
      } finally {finished();}
    });
    await p.locator('[data-assistant-status]').evaluate((e,url)=>{e.dataset.state='queued';e.dataset.statusUrl=url;},statusUrl);
    await p.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
    await arrival;
    return {release,done,statusUrl};
  };
  const contextControls = async (p,checked,revision) => {
    await p.locator('[data-assistant-question-form] details').evaluate(e=>e.open=true);
    await p.locator('[name=use_saved_context]').setChecked(checked);
    // A deliberately distinct synthetic revision proves the paired expected
    // revision is preserved, instead of silently adopting the fetched value.
    await p.locator('[name=expected_selection_revision]').evaluate((e,value)=>e.value=value,revision);
  };
  const failRoute = (route,failure) => failure==='network'?route.abort('failed'):route.fulfill({status:503,contentType:'text/plain',body:'Synthetic transport failure'});
  const mountCancel = async (p,statusUrl,state='queued') => {
    await p.locator('[data-assistant-status]').evaluate((e,{statusUrl,state})=>{
      e.hidden=false;e.dataset.statusUrl=statusUrl;e.dataset.state=state;
      const button=document.createElement('button');button.type='button';button.dataset.assistantCancel='';button.dataset.actionUrl=`${statusUrl}/cancel`;button.textContent='Cancel';e.append(button);
    },{statusUrl,state});
  };
  if(!group || group==='cancel') {
    // A successful Cancel POST can report still-running or already-terminal work.
    // History failure must retain recovery ownership in either case.
    for(const stage of ['fragment','post'])for(const failure of ['http','network'])for(const state of ['queued','running','cancelled']){
      if(stage==='post' && state==='cancelled')continue;
      const {ctx,p}=await fresh();let posts=0,polls=0,fragments=0;
      const statusUrl=`${fixture.matter}/answer-jobs/synthetic-cancel-${++sequence}`;
      await p.route(`**${statusUrl}`,route=>{polls++;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'cancelled',status_url:statusUrl,can_cancel:false})});});
      await p.route('**/assistant?**',route=>{fragments++;return failRoute(route,failure);});
      await p.route(`**${statusUrl}/cancel`,route=>{posts++;return stage==='post'?failRoute(route,failure):route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state,status_url:statusUrl,can_cancel:state!=='cancelled',cancel_url:`${statusUrl}/cancel`})});});
      await mountCancel(p,statusUrl,state==='cancelled'?'running':state);
      const draft=`Retain draft after Cancel ${stage} ${failure} ${state}.`;
      await p.locator('#assistant-question').fill(draft);
      await p.locator('[data-assistant-cancel]').click();
      if(state!=='cancelled')await p.waitForFunction(()=>document.querySelector('[data-assistant-status]').dataset.state==='cancelled',null,{timeout:4000});
      await p.locator('[data-assistant-status-copy]').filter({hasText:/Full conversation/i}).waitFor({timeout:3000});
      assert.equal(await p.locator('#assistant-question').inputValue(),draft);
      assert.equal(await p.locator('#assistant-question').isEnabled(),true);
      assert.equal(posts,1);assert.equal(polls,state==='cancelled'?0:1);assert.ok(fragments>=1);
      await p.waitForTimeout(200);assert.equal(posts,1);assert.equal(polls,state==='cancelled'?0:1);
      checks.push(`Cancel ${stage} ${failure} failure from ${state} retains draft and recovery, resumes only active polling, and never repeats the POST or terminal poll.`);
      await ctx.close();
    }
    for(const stage of ['post','fragment'])for(const change of ['scope','new-chat','submission']){
      const {ctx,p}=await fresh();let posts=0,arrived,release,finished;
      const arrival=new Promise(r=>arrived=r),hold=new Promise(r=>release=r),done=new Promise(r=>finished=r);
      const statusUrl=`${fixture.matter}/answer-jobs/synthetic-stale-cancel-${++sequence}`;
      await p.route(`**${statusUrl}`,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'queued',status_url:statusUrl,can_cancel:true,cancel_url:`${statusUrl}/cancel`})}));
      await p.route('**/assistant?**',async route=>{arrived();await hold;await failRoute(route,'http');finished();});
      await p.route(`**${statusUrl}/cancel`,async route=>{posts++;if(stage==='post'){arrived();await hold;}await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'cancelled',status_url:statusUrl,can_cancel:false})});if(stage==='post')finished();});
      await mountCancel(p,statusUrl);await p.locator('[data-assistant-cancel]').click();await arrival;
      let releaseAdmission,admissionDone;
      if(change==='scope'){
        await p.locator('[data-review-ask-source] button').click();await p.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
      }else if(change==='new-chat')await p.locator('[data-assistant-new-chat]').click();
      // Active origin jobs may intentionally keep the composer disabled on scope change.
      const draft='Keep current draft across stale Cancel response.';
      await p.locator('#assistant-question').evaluate((e,value)=>e.value=value,draft);
      if(change==='submission'){
        let admitted,completed;const admission=new Promise(r=>admitted=r),held=new Promise(r=>releaseAdmission=r);admissionDone=new Promise(r=>completed=r);
        const action=await p.locator('[data-assistant-question-form]').getAttribute('action');
        await p.route(`**${action}`,async route=>{admitted();await held;await route.abort('failed');completed();});
        await p.locator('[data-assistant-question-form] button[type=submit]').click();await admission;
      }
      await p.waitForTimeout(100);
      const dock=await p.locator('[data-assistant-dock]').elementHandle();
      const snapshot=()=>p.locator('[data-assistant-dock]').evaluate(e=>({conversation:e.dataset.conversationId,state:e.querySelector('[data-assistant-status]').dataset.state,copy:e.querySelector('[data-assistant-status-copy]').textContent,draft:e.querySelector('textarea').value,scope:e.querySelector('[name=source_set]').value,busy:e.querySelector('[data-assistant-question-form]').getAttribute('aria-busy')}));
      const before=await snapshot();release();await done;await p.waitForTimeout(150);
      assert.equal(await dock.evaluate(e=>e===document.querySelector('[data-assistant-dock]')),true);assert.deepEqual(await snapshot(),before);assert.equal(posts,1);
      if(releaseAdmission){releaseAdmission();await admissionDone;}
      checks.push(`Stale Cancel ${stage} response after ${change} cannot replace current dock, draft, scope or request ownership.`);
      await ctx.close();
    }
  }
  if(!group || group==='cancel' || group==='switch' || group==='ownership') {
    for(const action of (group==='cancel'?['cancel']:group==='switch'?['picker','preference']:['cancel','picker','preference'])) {
      const {ctx,p}=await fresh();let polls=0,fragments=0,posts=0,releasePoll,releaseResumedPoll,firstArrived;
      const firstArrival=new Promise(r=>firstArrived=r),firstHold=new Promise(r=>releasePoll=r),resumedHold=new Promise(r=>releaseResumedPoll=r);
      const statusUrl=`${fixture.matter}/answer-jobs/synthetic-owner-${++sequence}`;
      await p.route(`**${statusUrl}`,async route=>{
        polls++;
        if(polls===1){firstArrived();await firstHold;}else await resumedHold;
        await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'cancelled',status_url:statusUrl,can_cancel:false})}).catch(()=>{});
      });
      await p.route('**/assistant?**',async route=>{
        fragments++;
        if(fragments===1)return failRoute(route,'http');
        return route.fulfill({response:await route.fetch()});
      });
      if(action==='preference'){
        const matterId=await p.locator('[data-assistant-dock]').getAttribute('data-matter-id');
        await p.evaluate(({key,value})=>localStorage.setItem(key,value),{key:`recordbench:assistant:conversation:${matterId}`,value:fixture.alternate});
        await p.route('**/sources/**',async route=>{
          if(route.request().resourceType()!=='document')return route.continue();
          const response=await route.fetch();const html=(await response.text()).replace(/data-assistant-status data-status-url="[^"]*" data-state="[^"]*"/,`data-assistant-status data-status-url="${statusUrl}" data-state="queued"`);
          await route.fulfill({response,body:html});
        });
        await p.reload();await firstArrival;
      }else{
        await mountCancel(p,statusUrl);
        await p.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));await firstArrival;
        if(action==='cancel'){
          await p.route(`**${statusUrl}/cancel`,route=>{posts++;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'running',status_url:statusUrl,can_cancel:true,cancel_url:`${statusUrl}/cancel`})});});
          await p.locator('[data-assistant-cancel]').click();
        }else await p.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
      }
      await p.locator('[data-assistant-status-copy]').filter({hasText:action==='cancel'?/history could not refresh/i:/chat could not be opened/i}).waitFor({timeout:3000});
      // Releasing the older terminal poll must not refresh over the newer action.
      // Keep its response separate from the resumed GET scheduled by recovery.
      releasePoll();await p.waitForTimeout(100);assert.equal(fragments,1);
      releaseResumedPoll();
      await p.waitForFunction(()=>document.querySelector('[data-assistant-status]').dataset.statusUrl==='',null,{timeout:4000});
      assert.equal(polls,2);assert.equal(fragments,2);assert.equal(posts,action==='cancel'?1:0);
      assert.equal(await p.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),fixture.conversation);
      assert.equal(await p.locator('[data-assistant-conversation-picker]').isEnabled(),true);
      assert.equal(await p.locator('#assistant-question').isEnabled(),true);
      await p.waitForTimeout(200);assert.equal(polls,2);assert.equal(fragments,2);
      checks.push(`${action} refresh failure fences the older in-flight poll, resumes the origin active job once, and reaches terminal history without duplicate POST or terminal polling.`);
      await ctx.close();
    }
  }
  if(!group || group==='terminal') {
    for(const state of ['succeeded','failed','cancelled'])for(const failure of ['http','network']){
      const {ctx,p}=await fresh();
      const pending=await heldTerminal(p,state,failure);
      const draft=`Keep draft after ${state} history ${failure} failure.`;
      await p.locator('#assistant-question').fill(draft);
      pending.release();await pending.done;
      await p.locator('[data-assistant-status-copy]').filter({hasText:/draft.*kept/i}).waitFor({timeout:3000});
      assert.match(await p.locator('[data-assistant-status-copy]').innerText(),/Full conversation/i);
      assert.equal(await p.locator('#assistant-question').inputValue(),draft);
      assert.equal(await p.locator('#assistant-question').isEnabled(),true);
      assert.equal(await p.locator('[data-assistant-question-form] button[type=submit]').isEnabled(),true);
      assert.equal(await p.getByRole('link',{name:'Open case conversation and manage conversations',exact:true}).isVisible(),true);
      checks.push(`${state} history ${failure} failure exposes kept-draft recovery and a usable composer.`);
      await ctx.close();
    }
    for(const change of ['scope','new-chat','submission']){
      const {ctx,p}=await fresh();
      const pending=await heldTerminal(p,'cancelled','http');
      const draft='Current draft after invalidating old history failure.';
      await p.locator('#assistant-question').fill(draft);
      let releaseAdmission,admissionDone;
      if(change==='scope'){
        await p.locator('[data-review-ask-source] button').click();
        await p.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
      }else if(change==='new-chat'){
        await p.locator('[data-assistant-new-chat]').click();await p.locator('#assistant-question').fill(draft);
      }else{
        let arrived,finished;const arrival=new Promise(r=>arrived=r),hold=new Promise(r=>releaseAdmission=r);
        admissionDone=new Promise(resolve=>finished=resolve);
        const action=await p.locator('[data-assistant-question-form]').getAttribute('action');
        await p.route(`**${action}`,async route=>{arrived();await hold;await route.abort('failed');finished();});
        await p.locator('[data-assistant-question-form] button[type=submit]').click();await arrival;
      }
      const current=await p.locator('[data-assistant-dock]').elementHandle();
      const stateBefore=await p.locator('[data-assistant-status]').evaluate(e=>({text:e.textContent,hidden:e.hidden,state:e.dataset.state}));
      const scopeBefore=await p.locator('#assistant-source-set').inputValue();
      pending.release();await pending.done;await p.waitForTimeout(100);
      assert.equal(await current.evaluate(e=>e===document.querySelector('[data-assistant-dock]')),true);
      assert.deepEqual(await p.locator('[data-assistant-status]').evaluate(e=>({text:e.textContent,hidden:e.hidden,state:e.dataset.state})),stateBefore);
      assert.equal(await p.locator('#assistant-question').inputValue(),draft);
      assert.equal(await p.locator('#assistant-source-set').inputValue(),scopeBefore);
      if(change==='submission'){
        assert.equal(await p.locator('[data-assistant-question-form]').getAttribute('aria-busy'),'true');
        releaseAdmission();await admissionDone;
      }
      checks.push(`A stale history failure after ${change} cannot overwrite the current dock, draft, scope or status.`);
      await ctx.close();
    }
  }
  if(!group || group==='context') {
    for(const timing of ['before','during-on','during-off']){
      const {ctx,p}=await fresh();
      const initialOn=timing!=='during-on';
      await contextControls(p,initialOn,'314');
      const pending=await heldTerminal(p,'cancelled');
      if(timing!=='before')await contextControls(p,timing==='during-on','315');
      const expectedChecked=timing!=='during-off',expectedRevision=timing==='before'?'314':'315';
      const oldDock=await p.locator('[data-assistant-dock]').elementHandle();
      pending.release();await pending.done;
      await p.waitForFunction(e=>!e.isConnected,oldDock);
      assert.equal(await p.locator('[name=use_saved_context]').isChecked(),expectedChecked);
      assert.equal(await p.locator('[name=expected_selection_revision]').inputValue(),expectedRevision);
      checks.push(`Same-conversation refresh preserves saved-context checkbox and its paired revision ${timing} fetch.`);
      await ctx.close();
    }
    for(const change of ['conversation','scope']){
      const {ctx,p}=await fresh();await contextControls(p,true,'314');
      const pending=await heldTerminal(p,'cancelled');
      if(change==='conversation'){
        await p.unroute('**/assistant?**');
        await p.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
        await p.locator(`[data-assistant-dock][data-conversation-id="${fixture.alternate}"]`).waitFor();
        assert.equal(await p.locator('[name=use_saved_context]').isChecked(),false);
        assert.notEqual(await p.locator('[name=expected_selection_revision]').inputValue(),'314');
      }else{
        await p.locator('[data-review-ask-source] button').click();
        await p.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
        await contextControls(p,false,'316');
      }
      const expected=await p.locator('[data-assistant-dock]').evaluate(e=>({conversation:e.dataset.conversationId,checked:e.querySelector('[name=use_saved_context]').checked,revision:e.querySelector('[name=expected_selection_revision]').value,scope:e.querySelector('[name=source_set]').value}));
      pending.release();await pending.done;await p.waitForTimeout(100);
      assert.deepEqual(await p.locator('[data-assistant-dock]').evaluate(e=>({conversation:e.dataset.conversationId,checked:e.querySelector('[name=use_saved_context]').checked,revision:e.querySelector('[name=expected_selection_revision]').value,scope:e.querySelector('[name=source_set]').value})),expected);
      checks.push(`Invalidated old history cannot leak saved-context checkbox or revision across ${change} change.`);
      await ctx.close();
    }
  }
}

(async () => {
  const [data] = await Promise.race([once(server.stdout, 'data'), once(server,'exit').then(([code])=>{throw new Error(`Synthetic server exited before ready: ${code}`);})]);
  const fixture = JSON.parse(data.toString().trim());
  browser = await chromium.launch({ executablePath: process.argv[2], headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  activePage = page;
  if(process.argv.includes('--refresh-only')){await refreshRegressions(browser,fixture);fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({synthetic_only:true,passed:true,revision,dirty,javascript_baseline:baselineJsRevision,checks},null,2));console.log(checks);return;}
  await page.goto(fixture.base + fixture.reader);
  const go = async url => { const response = await page.goto(fixture.base+url); assert.equal(response.status(),200); };
  const expand = async () => { const button=page.locator('[data-assistant-expand]'); if(await button.isVisible()) await button.click(); };
  const noOverflow = async () => assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No horizontal page overflow');
  await go(fixture.reader);
  await page.setViewportSize({width:1280,height:604});
  await expand();
  await noOverflow();
  const dockGeometry=await page.locator('[data-assistant-dock]').evaluate(e=>[...e.children].filter(x=>getComputedStyle(x).display!=='none'&&x.getBoundingClientRect().height>0).map(x=>({name:x.className,top:x.getBoundingClientRect().top,bottom:x.getBoundingClientRect().bottom})));
  for(let i=1;i<dockGeometry.length;i++)assert.ok(dockGeometry[i].top>=dockGeometry[i-1].bottom-1,JSON.stringify(dockGeometry));
  const pdfTop = await page.locator('.document-reader').evaluate(e=>e.getBoundingClientRect().top);
  assert.ok(pdfTop<330,`PDF starts at ${pdfTop}`);
  await page.waitForTimeout(800);
  await page.screenshot({path:path.join(output,'synthetic-pdf-1280x604.png')});
  checks.push('Rendered PDF begins within first330px at1280x604 with assistant open.');
  await page.locator('[data-review-note-open]').click();
  const note=page.locator('[data-source-note-form]');
  await note.locator('textarea').fill('Synthetic human note preserves the page.');
  const reader=page.url();
  await note.locator('button[type=submit]').click();
  await note.locator('[role=status]').filter({hasText:/saved/i}).waitFor();
  assert.equal(page.url(),reader);
  assert.equal(await note.locator('button[type=submit]').isDisabled(),true);
  assert.equal(await note.locator('[data-source-note-new]').isVisible(),true);
  checks.push('Human note saves inline and success disables duplicate submission without moving PDF.');
  await page.screenshot({path:path.join(output,'synthetic-human-note-1280x604.png')});
  await note.locator('[data-source-note-new]').click();
  await note.locator('textarea').fill('Synthetic second deliberate note with uncertain response.');
  const noteAction=await note.getAttribute('action');
  await page.route(`**${noteAction}`,async route=>{await route.fetch();await route.abort('failed');});
  await note.locator('button[type=submit]').click();
  await note.locator('[role=status]').filter({hasText:/try|retry|failed/i}).waitFor();
  assert.equal(await note.locator('textarea').inputValue(),'Synthetic second deliberate note with uncertain response.');
  await page.unroute(`**${noteAction}`);
  await note.locator('button[type=submit]').click();
  await note.locator('[role=status]').filter({hasText:/already/i}).waitFor();
  checks.push('Lost save response retains draft and retries the same note without duplicate creation.');
  await note.locator('[data-source-note-new]').click();
  const navigationBody='Synthetic note saved before navigation response was received.';
  await note.locator('textarea').fill(navigationBody);
  const navigationSubmission=await note.evaluate(e=>Object.fromEntries(new FormData(e)));
  let releaseNoteNavigation,arrivedNoteNavigation,savedNavigation;
  const noteNavigationHold=new Promise(r=>releaseNoteNavigation=r),noteNavigationArrived=new Promise(r=>arrivedNoteNavigation=r);
  await page.route(`**${noteAction}`,async route=>{const response=await route.fetch();savedNavigation=await response.json();arrivedNoteNavigation();await noteNavigationHold;await route.fulfill({response}).catch(()=>{});});
  await note.locator('button[type=submit]').click();await noteNavigationArrived;
  const storedNoteMetadata=await page.evaluate(()=>history.state.recordbenchSourceNote);
  assert.deepEqual(Object.keys(storedNoteMetadata).sort(),['action','request_key','source_version_id','source_basis','unit','start_ms'].sort());
  assert.equal(await page.evaluate(value=>JSON.stringify(history.state).includes(value),navigationBody),false);

  const stayDialog=new Promise(resolve=>page.once('dialog',async dialog=>{assert.equal(dialog.type(),'beforeunload');await dialog.dismiss();resolve();}));
  await page.getByRole('link',{name:'Notes',exact:true}).click({noWaitAfter:true});await stayDialog;
  assert.equal(page.url(),reader);
  assert.equal(await note.locator('textarea').inputValue(),navigationBody);
  const leaveDialog=new Promise(resolve=>page.once('dialog',async dialog=>{assert.equal(dialog.type(),'beforeunload');await dialog.accept();resolve();}));
  await page.getByRole('link',{name:'Notes',exact:true}).click({noWaitAfter:true});await leaveDialog;
  await page.waitForURL('**/notebook');releaseNoteNavigation();
  await page.unroute(`**${noteAction}`);
  await page.goBack();await note.waitFor();
  observations.human_note_draft_after_confirmed_navigation=await note.locator('textarea').inputValue();
  observations.human_note_returned_request_key_matches=await note.locator('[name=request_key]').inputValue()===navigationSubmission.request_key;
  observations.human_note_returned_save_disabled=await note.locator('button[type=submit]').isDisabled();
  observations.human_note_returned_busy=await note.getAttribute('aria-busy');
  // The original request receipt is retained by this test, not by a claim that
  // the UI restores abandoned drafts. Verify its retry boundary independently.
  const navigationEvidence=await (await page.request.get(`${fixture.base}/synthetic-note-evidence/${savedNavigation.item_id}`)).json();
  assert.equal(navigationEvidence.body,navigationBody);
  const retryAfterNavigation=await page.request.post(fixture.base+noteAction,{form:navigationSubmission,headers:{Accept:'application/json'}});
  assert.equal(retryAfterNavigation.status(),200);
  const retriedNavigation=await retryAfterNavigation.json();
  assert.equal(retriedNavigation.created,false);assert.equal(retriedNavigation.item_id,savedNavigation.item_id);
  const navigationEvidenceAfter=await (await page.request.get(`${fixture.base}/synthetic-note-evidence/${savedNavigation.item_id}`)).json();
  assert.equal(navigationEvidenceAfter.total,navigationEvidence.total);
  const returnedUiResponse=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/notes'));
  await note.locator('button[type=submit]').click();
  const returnedUiResult=await returnedUiResponse;
  const returnedUiSave=await returnedUiResult.json();
  observations.human_note_returned_ui_status=returnedUiResult.status();
  observations.human_note_returned_ui_created=returnedUiSave.created;
  assert.equal(returnedUiSave.item_id,savedNavigation.item_id,'Restored human-note UI must retain its retry identity');
  assert.equal(returnedUiSave.created,false,'Returning after an uncertain save must not create a duplicate');
  const afterReturnedUi=await (await page.request.get(`${fixture.base}/synthetic-note-evidence/${savedNavigation.item_id}`)).json();
  assert.equal(afterReturnedUi.total,navigationEvidence.total);
  await note.locator('[data-source-note-new]').click();
  await note.locator('textarea').fill('Synthetic deliberate new note after returning to this source.');
  const intentionalResponse=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/notes'));
  await note.locator('button[type=submit]').click();
  const intentionalSave=await (await intentionalResponse).json();
  assert.equal(intentionalSave.created,true);assert.notEqual(intentionalSave.item_id,savedNavigation.item_id);
  const afterIntentional=await (await page.request.get(`${fixture.base}/synthetic-note-evidence/${intentionalSave.item_id}`)).json();
  assert.equal(afterIntentional.total,navigationEvidence.total+1);
  const missingDraft=await context.newPage();let missingDraftPosts=0;
  missingDraft.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/notes'))missingDraftPosts+=1;});
  await missingDraft.goto(fixture.base+fixture.reader);
  await missingDraft.evaluate(metadata=>{history.replaceState({...history.state,recordbenchSourceNote:metadata},'');window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));},storedNoteMetadata);
  await missingDraft.waitForFunction(()=>document.querySelector('[data-source-note-form] button[type=submit]').disabled);
  assert.equal(await missingDraft.locator('[data-source-note-form] textarea').inputValue(),'');
  assert.match(await missingDraft.locator('[data-source-note-status]').innerText(),/not restored|no text|saved case notes/i);
  assert.equal(await missingDraft.locator('[data-source-note-new]').isEnabled(),true);assert.equal(missingDraftPosts,0);
  await missingDraft.close();



  checks.push('Cancelling unload keeps a pending note; Back retries the original save without duplication, while explicit new-note creation remains distinct and history stores no prose.');

  await page.locator('[data-review-note-close]').click();
  await expand();
  // Delayed saved-chat fragment must not restore old history after New chat.
  let releaseFragment, arrivedFragment;
  const fragmentHold=new Promise(r=>releaseFragment=r), fragmentArrived=new Promise(r=>arrivedFragment=r);
  await page.route('**/assistant?**',async route=>{const response=await route.fetch();arrivedFragment();await fragmentHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
  await fragmentArrived;
  await page.locator('[data-assistant-new-chat]').click();
  await page.locator('#assistant-question').fill('New draft after delayed history.');
  releaseFragment();
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'New draft after delayed history.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),'');
  await page.unroute('**/assistant?**');
  checks.push('Delayed history fragment cannot replace New chat or clear its draft.');
  let releaseDraftFragment,arrivedDraftFragment;
  const draftFragmentHold=new Promise(r=>releaseDraftFragment=r),draftFragmentArrived=new Promise(r=>arrivedDraftFragment=r);
  await page.route('**/assistant?**',async route=>{const response=await route.fetch();arrivedDraftFragment();await draftFragmentHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);await draftFragmentArrived;
  await page.locator('[data-assistant-new-chat]').click();releaseDraftFragment();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'New draft after delayed history.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),'');
  assert.equal(await page.locator('[data-assistant-conversation-picker]').isDisabled(),false);
  await page.unroute('**/assistant?**');
  checks.push('Repeated New chat from a draft invalidates a pending saved-chat selection while preserving the draft.');
  let releaseScopeHistory,arrivedScopeHistory;
  const scopeHistoryHold=new Promise(r=>releaseScopeHistory=r),scopeHistoryArrived=new Promise(r=>arrivedScopeHistory=r);
  await page.route('**/assistant?**',async route=>{const response=await route.fetch();arrivedScopeHistory();await scopeHistoryHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);await scopeHistoryArrived;
  const scopeHistoryDraft=await page.locator('#assistant-question').inputValue();
  await page.locator('[data-review-ask-source] button').click();
  await page.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
  assert.equal(await page.locator('[data-assistant-conversation-picker]').isDisabled(),false);
  releaseScopeHistory();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),scopeHistoryDraft);
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),'');
  assert.match(await page.locator('#assistant-source-set option:checked').innerText(),/Synthetic extended/);
  await page.unroute('**/assistant?**');
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.alternate}"]`).waitFor();
  await page.locator('[data-assistant-new-chat]').click();
  await page.locator('#assistant-question').fill('New draft after delayed history.');
  checks.push('Source selection invalidates held history, preserves draft/scope and re-enables the saved-chat picker for another selection.');

  // Delay accepted question response, then deliberately change composer state.
  let releaseAsk, arrivedAsk;
  const askHold=new Promise(r=>releaseAsk=r),askArrived=new Promise(r=>arrivedAsk=r);
  const askAction=await page.locator('[data-assistant-question-form]').getAttribute('action');
  await page.route(`**${askAction}`,async route=>{const response=await route.fetch();arrivedAsk();await askHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('[data-assistant-question-form] button[type=submit]').click();
  await askArrived;
  await page.locator('[data-assistant-new-chat]').click();
  await page.locator('#assistant-question').fill('New draft after delayed accepted question.');
  releaseAsk();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'New draft after delayed accepted question.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),'');
  await page.unroute(`**${askAction}`);
  checks.push('Delayed accepted question cannot overwrite a newer chat or draft.');
  let releasePoll, arrivedPoll, releaseCancel, arrivedCancel;
  const pollHold=new Promise(r=>releasePoll=r), pollArrived=new Promise(r=>arrivedPoll=r);
  const cancelHold=new Promise(r=>releaseCancel=r), cancelArrived=new Promise(r=>arrivedCancel=r);
  await page.route('**/answer-jobs/*',async route=>{const response=await route.fetch();arrivedPoll();await pollHold;await route.fulfill({response}).catch(()=>{});});
  await page.route('**/answer-jobs/*/cancel',async route=>{const response=await route.fetch();arrivedCancel();await cancelHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('[data-assistant-question-form] button[type=submit]').click();
  await pollArrived;
  await page.locator('[data-assistant-cancel]').click();
  await cancelArrived;
  await page.locator('[data-assistant-new-chat]').click();
  await page.locator('#assistant-question').fill('Preserve draft after old polling and cancellation.');
  releasePoll();releaseCancel();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'Preserve draft after old polling and cancellation.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),'');
  await page.unroute('**/answer-jobs/*');await page.unroute('**/answer-jobs/*/cancel');
  checks.push('Delayed polling and cancellation responses cannot restore an old conversation or clear the new draft.');
  await go(fixture.media);
  const player=page.locator('video[data-media-player]');
  await player.waitFor();
  await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);
  await player.evaluate(async e=>{e.currentTime=1;e.muted=true;await e.play();});
  await page.locator('[data-review-note-open]').click();
  assert.equal(await player.evaluate(e=>e.paused),false);
  assert.ok(await player.evaluate(e=>e.currentTime>=1));
  await page.locator('[data-review-note-close]').click();
  await player.evaluate(e=>{e.pause();e.currentTime=1;});
  await page.waitForTimeout(150);
  await page.locator('[data-transcript-segment].is-active').waitFor();
  await page.locator('[data-review-note-open]').click();
  const mediaNote=page.locator('[data-source-note-form]');
  const displayedTime=Number(await mediaNote.locator('[data-note-media-time]').inputValue());
  assert.equal(displayedTime,Math.floor(await player.evaluate(e=>e.currentTime*1000)));
  await mediaNote.locator('textarea').fill('Synthetic human note at the displayed video position.');
  const mediaNoteResponse=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/notes'));
  await mediaNote.locator('button[type=submit]').click();
  const savedMedia=await (await mediaNoteResponse).json();
  await mediaNote.locator('[role=status]').filter({hasText:/saved/i}).waitFor();
  const mediaEvidence=await (await page.request.get(`${fixture.base}/synthetic-note-evidence/${savedMedia.item_id}`)).json();
  assert.equal(mediaEvidence.origin,'manual');assert.equal(mediaEvidence.status,'needs_review');
  assert.equal(mediaEvidence.document_id,fixture.video_id);assert.equal(mediaEvidence.source_version_id,fixture.video_version);
  assert.ok(mediaEvidence.start_ms<=displayedTime&&displayedTime<mediaEvidence.end_ms);
  assert.equal(await player.evaluate(e=>Math.floor(e.currentTime*1000)),displayedTime);
  await page.screenshot({path:path.join(output,'synthetic-video-human-note-1280x604.png')});
  await page.locator('[data-review-note-close]').click();
  checks.push('A real video note saves the displayed playback position against canonical source-version/transcript support as a human Needs-review note.');

  await expand();
  await page.locator('[data-assistant-conversation-picker]').selectOption(fixture.alternate);
  await page.locator(`[data-assistant-dock][data-conversation-id="${fixture.alternate}"]`).waitFor();
  const playerHandle=await player.elementHandle();
  await page.locator('[data-review-ask-source] button').click();
  await page.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
  assert.equal(await playerHandle.evaluate(e=>e===document.querySelector('video')),true);
  assert.equal(await player.evaluate(e=>Math.floor(e.currentTime)),1);
  assert.match(await page.locator('#assistant-source-set option:checked').innerText(),/Synthetic blue training video/);
  assert.match(await page.locator('[data-assistant-thread]').innerText(),/Historical answer/);
  checks.push('Ask using this video preserves player/time and explicitly separates next question scope from historical answer.');
  await page.screenshot({path:path.join(output,'synthetic-video-1280x604.png')});
  // A retained terminal job URL is historical status, never a reason to poll
  // and replace a live composer after a scope change or bfcache restoration.
  let terminalPolls=0;
  const terminalStatusUrl=`${fixture.matter}/answer-jobs/synthetic-terminal-status`;
  await page.route(`**${terminalStatusUrl}`,route=>{terminalPolls+=1;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'failed',message:'Synthetic historical terminal request.'})});});
  const terminalDock=await page.locator('[data-assistant-dock]').elementHandle();
  const savedStatus=await page.locator('[data-assistant-status]').evaluate(e=>({state:e.dataset.state,statusUrl:e.dataset.statusUrl}));
  for(const state of ['failed','cancelled','succeeded']){
    await page.locator('[data-assistant-status]').evaluate((e,value)=>{e.dataset.state=value.state;e.dataset.statusUrl=value.url;},{state,url:terminalStatusUrl});
    const draft=`Preserve draft beside historical ${state} request.`;
    await page.locator('#assistant-question').fill(draft);
    await page.locator('[data-review-ask-source] button').click();
    await page.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
    await page.waitForTimeout(150);
    assert.equal(terminalPolls,0,'Scope changes must not poll terminal jobs');
    assert.equal(await page.locator('#assistant-question').inputValue(),draft);
    await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
    await page.waitForTimeout(150);
    assert.equal(terminalPolls,0,'Restored pages must not poll terminal jobs');
    assert.equal(await page.locator('#assistant-question').inputValue(),draft);
    assert.equal(await terminalDock.evaluate(e=>e===document.querySelector('[data-assistant-dock]')),true);
  }
  await page.locator('[data-assistant-status]').evaluate((e,value)=>{if(value.state===undefined)delete e.dataset.state;else e.dataset.state=value.state;if(value.statusUrl===undefined)delete e.dataset.statusUrl;else e.dataset.statusUrl=value.statusUrl;},savedStatus);
  await page.unroute(`**${terminalStatusUrl}`);
  checks.push('Failed, cancelled and completed historical job URLs never poll or replace drafts after source selection or persisted pageshow.');
  assert.match(await page.locator('#assistant-source-set option:checked').innerText(),/Synthetic blue training video/);
  await page.locator('[data-assistant-new-chat]').click();
  assert.equal(await page.locator('#assistant-source-set').inputValue(),'');
  let releaseTerminalFragment,arrivedTerminalFragment;
  const terminalFragmentHold=new Promise(r=>releaseTerminalFragment=r),terminalFragmentArrived=new Promise(r=>arrivedTerminalFragment=r);
  await page.route('**/answer-jobs/*',async route=>{
    // Complete the real queued synthetic request through its ordinary cancel
    // endpoint, then return its terminal state to the waiting poll.
    const cancelled=await page.request.post(`${route.request().url()}/cancel`,{headers:{Accept:'application/json'}});
    assert.equal(cancelled.status(),200);
    await route.fulfill({status:200,contentType:'application/json',body:await cancelled.text()});
  });
  await page.route('**/assistant?**',async route=>{const response=await route.fetch();arrivedTerminalFragment();await terminalFragmentHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('#assistant-question').fill('Synthetic question whose completion refresh is delayed.');
  await page.locator('[data-assistant-question-form] button[type=submit]').click();
  await terminalFragmentArrived;
  const terminalConversation=await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id');
  const terminalScope=await page.locator('#assistant-source-set').inputValue();
  assert.equal(terminalScope,'');
  await page.locator('#assistant-question').fill('New same-conversation draft during terminal fragment refresh.');
  releaseTerminalFragment();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'New same-conversation draft during terminal fragment refresh.');
  assert.equal(await page.locator('[data-assistant-dock]').getAttribute('data-conversation-id'),terminalConversation);
  assert.equal(await page.locator('#assistant-source-set').inputValue(),terminalScope);
  assert.equal(await page.locator('#assistant-question').evaluate(e=>e===document.activeElement),true);
  assert.equal(await page.locator('#assistant-question').evaluate(e=>e.selectionStart), 'New same-conversation draft during terminal fragment refresh.'.length);
  await page.unroute('**/answer-jobs/*');await page.unroute('**/assistant?**');
  checks.push('A delayed terminal fragment retains a newer draft and source scope in the same conversation; New chat All-sources scope stays consistent through admission and refresh.');
  let releaseOldTerminal,arrivedOldTerminal;
  const oldTerminalHold=new Promise(r=>releaseOldTerminal=r),oldTerminalArrived=new Promise(r=>arrivedOldTerminal=r);
  await page.route('**/answer-jobs/*',async route=>{const cancelled=await page.request.post(`${route.request().url()}/cancel`,{headers:{Accept:'application/json'}});await route.fulfill({status:200,contentType:'application/json',body:await cancelled.text()});});
  await page.route('**/assistant?**',async route=>{const response=await route.fetch();arrivedOldTerminal();await oldTerminalHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('#assistant-question').fill('Synthetic request A before superseding request B.');
  await page.locator('[data-assistant-question-form] button[type=submit]').click();await oldTerminalArrived;
  await page.unroute('**/answer-jobs/*');
  let releaseNewAdmission,arrivedNewAdmission,newAdmissionPayload,newAdmissions=0;
  const newAdmissionHold=new Promise(r=>releaseNewAdmission=r),newAdmissionArrived=new Promise(r=>arrivedNewAdmission=r);
  const newerAction=await page.locator('[data-assistant-question-form]').getAttribute('action');
  await page.route(`**${newerAction}`,async route=>{newAdmissions+=1;const response=await route.fetch();newAdmissionPayload=await response.json();arrivedNewAdmission();await newAdmissionHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('#assistant-question').fill('Synthetic request B owns the current composer.');
  await page.locator('[data-assistant-question-form] button[type=submit]').click();await newAdmissionArrived;
  const newerDock=await page.locator('[data-assistant-dock]').elementHandle();
  releaseOldTerminal();await page.waitForTimeout(150);
  assert.equal(await newerDock.evaluate(e=>e===document.querySelector('[data-assistant-dock]')),true);
  assert.equal(await page.locator('[data-assistant-question-form]').getAttribute('aria-busy'),'true');
  await page.unroute('**/assistant?**');releaseNewAdmission();
  await page.waitForFunction(url=>document.querySelector('[data-assistant-status]')?.dataset.statusUrl===url,newAdmissionPayload.status_url);
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
  await page.waitForTimeout(150);
  assert.equal(await page.locator('[data-assistant-status]').getAttribute('data-status-url'),newAdmissionPayload.status_url);
  assert.equal(await page.locator('[data-assistant-question-form]').getAttribute('aria-busy'),'true');
  assert.equal(newAdmissions,1);
  await page.unroute(`**${newerAction}`);
  await page.locator('[data-assistant-cancel]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-assistant-question-form]').hasAttribute('aria-busy'));
  checks.push('Submitting request B invalidates delayed terminal-A history; B retains busy status and polling ownership across persisted pageshow with one admission.');

  let releaseScopedAsk, arrivedScopedAsk;
  const scopedHold=new Promise(r=>releaseScopedAsk=r),scopedArrived=new Promise(r=>arrivedScopedAsk=r);
  const scopedAction=await page.locator('[data-assistant-question-form]').getAttribute('action');
  await page.route(`**${scopedAction}`,async route=>{const response=await route.fetch();arrivedScopedAsk();await scopedHold;await route.fulfill({response}).catch(()=>{});});
  await page.locator('#assistant-question').fill('Question submitted before source scope change.');
  await page.locator('[data-assistant-question-form] button[type=submit]').click();await scopedArrived;
  await page.locator('[data-review-ask-source] button').click();
  await page.locator('[data-review-ask-source] [role=status]').filter({hasText:'Source selected'}).waitFor();
  await page.locator('#assistant-question').fill('Draft after source selection during pending admission.');
  releaseScopedAsk();await page.waitForTimeout(150);
  assert.equal(await page.locator('#assistant-question').inputValue(),'Draft after source selection during pending admission.');
  assert.match(await page.locator('#assistant-source-set option:checked').innerText(),/Synthetic blue training video/);
  await page.unroute(`**${scopedAction}`);
  checks.push('Selecting source during pending answer admission preserves the new scope and later draft.');
  checks.push('Synthetic video loads and continues playback while notes toggle; transcript remains mounted.');
  await page.locator('[data-review-queue-toggle]').focus();
  await page.locator('[data-review-queue-toggle]').press('Enter');
  assert.equal(await page.locator('[data-review-queue-toggle]').getAttribute('aria-expanded'),'true');
  await page.locator('summary').filter({hasText:'Review actions'}).click();
  const width=page.locator('[data-review-pane-width]');
  await width.focus();await width.press('ArrowRight');
  const savedWidth=await width.inputValue();
  await page.locator('[data-review-note-open]').click();
  await page.locator('#source-note-body').press('Escape');
  assert.equal(await page.locator('[data-review-note-open]').evaluate(e=>e===document.activeElement),true);
  await page.reload();await player.waitFor();
  assert.equal(await page.locator('[data-review-queue-toggle]').getAttribute('aria-expanded'),'true');
  assert.equal(await page.locator('[data-review-pane-width]').inputValue(),savedWidth);
  const previous=page.locator('a.review-sequence-neighbor.previous');
  await previous.click();
  assert.equal(new URLSearchParams(new URL(page.url()).searchParams.get('browse')).get('sort'),'name');
  assert.equal(await page.locator('[data-review-queue-toggle]').getAttribute('aria-expanded'),'true');
  await page.goBack();await player.waitFor();
  checks.push('Keyboard queue toggle, pane-width adjustment and note Escape focus work; filtered queue/layout survive previous source and reload.');
  await page.goBack();await page.goForward();
  assert.equal(new URL(page.url()).pathname,new URL(fixture.base+fixture.media).pathname);
  checks.push('Reload and Back/Forward return to the media source.');
  for(const size of [{width:390,height:844},{width:640,height:302}]){
    await page.setViewportSize(size);await page.waitForTimeout(300);
    const rail=page.locator('[data-rail-toggle]');
    if(await rail.getAttribute('aria-expanded')==='true')await rail.click();
    if(await page.locator('[data-review-queue-toggle]').getAttribute('aria-expanded')==='true')await page.locator('[data-review-queue-toggle]').click();
    await player.scrollIntoViewIfNeeded();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);await player.evaluate(async e=>{e.muted=true;await e.play();});await page.waitForTimeout(150);await player.evaluate(e=>e.pause());await page.waitForTimeout(150);await noOverflow();
    await page.screenshot({path:path.join(output,`synthetic-video-${size.width}x${size.height}.png`)});
  }
  checks.push('Media review has no page overflow at390px and640x302 (200% viewport-equivalent; not native zoom).');
  await page.setViewportSize({width:1280,height:604});
  await go(fixture.reader);
  await page.getByRole('link',{name:'Notes',exact:true}).click();
  const toolsPane=page.locator('.notebook-tools');
  const toolsStyle=await toolsPane.evaluate(e=>({overflow:getComputedStyle(e).overflowY,position:getComputedStyle(e).position,height:e.clientHeight,scroll:e.scrollHeight}));
  assert.equal(toolsStyle.overflow,'visible');assert.equal(toolsStyle.position,'static');assert.ok(toolsStyle.height>=toolsStyle.scroll-1);
  const suggestions=page.locator('.suggestion-tool button');
  await page.locator('.suggestion-tool select').focus();await page.keyboard.press('Tab');
  assert.equal(await suggestions.evaluate(e=>e===document.activeElement),true);
  await page.waitForFunction(()=>{const r=document.querySelector('.suggestion-tool button').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;},{},{timeout:3000});
  await page.screenshot({path:path.join(output,'synthetic-notebook-tools-1280x604.png')});
  await go(fixture.reader);
  await page.locator('.review-toolbar a').filter({hasText:'Discover entities'}).click();
  await page.locator('#discovery-heading').waitFor();
  assert.match(await page.locator('#discovery-heading').innerText(),/guided discovery/);
  checks.push('Case notes discovery tools use ordinary document flow and keyboard focus; entity discovery is directly reachable from reader.');
  const nojs=await browser.newContext({javaScriptEnabled:false,viewport:{width:1280,height:604}});
  const fallback=await nojs.newPage();await fallback.goto(fixture.base+fixture.reader);
  await fallback.locator('[data-source-note-form] textarea').fill('Synthetic no-JavaScript human note.');
  await fallback.locator('[data-source-note-form] button[type=submit]').click();
  assert.equal(new URL(fallback.url()).pathname,new URL(fixture.base+fixture.reader).pathname);
  assert.match(new URL(fallback.url()).searchParams.get('notice'),/saved/);
  checks.push('JavaScript-disabled human note form saves and returns to the same source.');
  await refreshRegressions(browser,fixture);
  fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({synthetic_only:true,passed:true,revision,dirty,javascript_baseline:baselineJsRevision,checks,observations,limitations:['No real model/GPU qualification','640x302 simulates the CSS viewport at200%; native browser zoom not tested','Transcript pagination and playback-history restoration not yet verified','Browser-native draft restoration varies; this run verifies Back retry in installed Chromium and a missing-text safe fallback, not cross-browser or browser-restart recovery']},null,2));
  console.log(JSON.stringify(checks,null,2));
})().catch(async error => {
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({ path: path.join(output, 'synthetic-failure.png') }).catch(() => {});
    console.error(await activePage.locator('[data-assistant-dock]').innerText().catch(() => 'Dock unavailable'));
  }
  console.error(error);
  fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ synthetic_only: true,
    passed: false, revision, dirty, javascript_baseline:baselineJsRevision, checks, observations, error: String(error).slice(0, 2000) }, null, 2));
  process.exitCode = 1;
}).finally(async () => {
  clearTimeout(deadline);
  if (browser) await browser.close();
  server.kill('SIGTERM');
});
