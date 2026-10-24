"""Execute the source-locked construction consumers with synthetic inputs."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from tree_sitter import Language, Parser
import tree_sitter_javascript

ROOT = Path(__file__).resolve().parents[1]


def run_node(source):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for JavaScript consumer behavior checks')
    result = subprocess.run([node, '-e', source], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def declaration(name, relative='src/case_intelligence/static/case-intelligence.js'):
    source = (ROOT / relative).read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(source)
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type == 'variable_declarator' and node.child_by_field_name('name').text.decode() == name:
            return node.parent.text.decode()
        pending.extend(node.named_children)
    raise AssertionError(name)


def declarations(*names):
    return '\n'.join(declaration(name) for name in names)


STORAGE = r'''
const assert = require('node:assert/strict');
const values = new Map();
const storage = {
 getItem: key => values.has(key) ? values.get(key) : null,
 setItem: (key, value) => values.set(key, String(value)),
 removeItem: key => values.delete(key),
};
const window = {localStorage: storage, sessionStorage: storage};
'''


def test_matter_namespace_inputs_are_generated_instead_of_using_display_names(tmp_path):
    from case_intelligence.workspace_store import WorkspaceStore

    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        owner = store.upsert_principal('test', 'synthetic-owner', 'Synthetic Owner', 'synthetic-owner')
        matters = [store.create_matter('Synthetic: ../selection-receipt', '', owner.principal_id) for _ in range(2)]
        assert all(re.fullmatch(r'm-[0-9a-f]{12}', matter.slug) for matter in matters)
        assert all(re.fullmatch(r'ci-matter-[0-9a-f]{32}', matter.matter_id) for matter in matters)
        assert len({matter.slug for matter in matters}) == 2
        assert len({matter.matter_id for matter in matters}) == 2
    finally:
        store.close()


def test_upload_and_selection_keys_keep_matters_and_checkpoint_kinds_separate():
    source = declarations('uploadSessionKey', 'intakeResumeKey', 'memoryUploadCheckpoint',
                          'memoryIntakeCheckpoint', 'storedUploadCheckpoint',
                          'writeUploadResumeState', 'clearUploadResumeState', 'clearIntakeResumeState')
    run_node(STORAGE + 'function page(uploadForm) { const maximumUploadItems = 2000;\n' + source + r'''
 return {uploadSessionKey,intakeResumeKey,writeUploadResumeState,storedUploadCheckpoint,
         clearUploadResumeState,clearIntakeResumeState};
}
const a=page({dataset:{matterSlug:'m-000000000001'}});
const b=page({dataset:{matterSlug:'m-000000000002'}});
assert.equal(a.uploadSessionKey,'case-intelligence:upload:m-000000000001');
assert.equal(a.intakeResumeKey,'case-intelligence:upload:m-000000000001:selection-receipt');
const fingerprint='a'.repeat(64), session='upload-session-'+'b'.repeat(32), collection='source-collection-'+'c'.repeat(32);
a.writeUploadResumeState(fingerprint,session,collection,0,1,null);
assert.equal(a.storedUploadCheckpoint().session_id,session);
assert.equal(b.storedUploadCheckpoint(),null);
storage.setItem(a.intakeResumeKey,JSON.stringify({selection_key:'d'.repeat(32)}));
storage.setItem(b.intakeResumeKey,'other matter');
a.clearUploadResumeState();
assert.equal(a.storedUploadCheckpoint(),null);
assert.ok(storage.getItem(a.intakeResumeKey));
a.writeUploadResumeState(fingerprint,session,collection,0,1,null);
a.clearIntakeResumeState();
assert.equal(storage.getItem(a.intakeResumeKey),null);
assert.equal(storage.getItem(b.intakeResumeKey),'other matter');
assert.equal(a.storedUploadCheckpoint().session_id,session);
storage.setItem(a.uploadSessionKey,JSON.stringify({version:3,plan_fingerprint:fingerprint,session_id:'../other',collection_id:collection,batch_index:0}));
assert.equal(a.storedUploadCheckpoint(),null);
for(const form of [null,{dataset:{}},{dataset:{matterSlug:''}}]){
 const empty=page(form);assert.equal(empty.uploadSessionKey,'');assert.equal(empty.intakeResumeKey,'');
 const before=[...values];empty.writeUploadResumeState(fingerprint,session,collection,0,1,null);
 empty.clearUploadResumeState();empty.clearIntakeResumeState();assert.deepEqual([...values],before);
}
''')


def test_assistant_preferences_migrate_only_the_selected_namespace_and_scope():
    source = declarations('previousStorageKey', 'readBrowserValue', 'writeBrowserValue',
                          'assistantPreferenceKey', 'assistantDisplayKey',
                          'readAssistantPreference', 'saveAssistantPreference',
                          'assistantConversationPreferenceKey', 'readAssistantConversationPreference',
                          'saveAssistantConversationPreference')
    run_node(STORAGE + r'''
const body={dataset:{assistantDefault:'open'}};
const compactAssistant={matches:false};
window.matchMedia=()=>compactAssistant;
let assistantDock={dataset:{matterId:'ci-matter-0001'}};
''' + source + r'''
const desktop='exculpata:assistant:display:v2',compact=desktop+':compact';
const oldDesktop=LEGACY_PREFIX+'assistant:display:v2',oldCompact=oldDesktop+':compact';
storage.setItem(oldDesktop,'collapsed');storage.setItem(oldCompact,'open');
assert.equal(readAssistantPreference(),true);
assert.equal(storage.getItem(oldDesktop),null);assert.equal(storage.getItem(oldCompact),'open');
compactAssistant.matches=true;assert.equal(assistantDisplayKey(),compact);
assert.equal(readAssistantPreference(),false);assert.equal(storage.getItem(oldCompact),null);
saveAssistantPreference(true);assert.equal(storage.getItem(compact),'collapsed');
compactAssistant.matches=false;saveAssistantPreference(false);
assert.equal(storage.getItem(desktop),'open');assert.equal(storage.getItem(compact),'collapsed');
storage.setItem(oldDesktop,'collapsed');assert.equal(readAssistantPreference(),false);
assert.equal(storage.getItem(oldDesktop),'collapsed');
const key=assistantConversationPreferenceKey();
assert.equal(key,'exculpata:assistant:conversation:ci-matter-0001');
storage.setItem(LEGACY_PREFIX+'assistant:conversation:ci-matter-0001','conversation-old');
assert.equal(readAssistantConversationPreference(),'conversation-old');
saveAssistantConversationPreference('conversation-new');
assistantDock.dataset.matterId='ci-matter-0002';assert.equal(readAssistantConversationPreference(),'');
saveAssistantConversationPreference('conversation-other');
assert.equal(storage.getItem(key),'conversation-new');
assistantDock=null;const before=[...values];saveAssistantConversationPreference('ignored');
assert.equal(readAssistantConversationPreference(),'');assert.deepEqual([...values],before);
storage.getItem=()=>{throw Error('synthetic unavailable storage')};
assert.equal(readAssistantPreference(),false);assert.equal(readAssistantConversationPreference(),'');
'''.replace('LEGACY_PREFIX', json.dumps('record' + 'bench:')))


def test_media_and_review_preferences_scope_by_source_path_and_matter():
    source = declarations('mediaFollowResumeKey', 'mediaToolResumeKey')
    run_node(STORAGE + 'function media(mediaReview, pathname) { window.location={pathname};\n' + source + r'''
return [mediaFollowResumeKey,mediaToolResumeKey]; }
const first=media({},'/matters/m-1/sources/source-1');
const second=media({},'/matters/m-1/sources/source-2');
assert.deepEqual(first,['case-intelligence:media-follow:/matters/m-1/sources/source-1','case-intelligence:media-tool:/matters/m-1/sources/source-1']);
assert.equal(new Set([...first,...second]).size,4);
assert.deepEqual(media(null,'/matters/m-1/sources/source-1'),['','']);
''' + declarations('previousStorageKey', 'readBrowserValue', 'writeBrowserValue') +
             'function review(reviewWorkspace) {\n' + declaration('preference') + r'''
return preference; }
const a=review({dataset:{reviewMatter:'ci-matter-0001'}}),b=review({dataset:{reviewMatter:'ci-matter-0002'}});
assert.equal(a,'exculpata:review-layout:ci-matter-0001');assert.notEqual(a,b);
storage.setItem(LEGACY_PREFIX+'review-layout:ci-matter-0001','{"width":380,"queue":true}');
assert.equal(readBrowserValue(storage,a),'{"width":380,"queue":true}');
assert.equal(storage.getItem(LEGACY_PREFIX+'review-layout:ci-matter-0001'),null);
writeBrowserValue(storage,b,'{"width":500}');assert.equal(JSON.parse(storage.getItem(a)).width,380);
'''.replace('LEGACY_PREFIX', json.dumps('record' + 'bench:')))


def test_selection_receipt_routes_require_valid_identity_and_preserve_retry_key():
    source = declarations('uploadSessionKey', 'intakeResumeKey', 'memoryIntakeCheckpoint',
                          'clearIntakeResumeState', 'intakeCollectionName', 'preflightDescriptor',
                          'recordConfirmedSelection')
    run_node(STORAGE + r'''
const uploadForm={dataset:{matterSlug:'m-000000000001',intakeUrl:'/matters/m-000000000001/intake-receipts'}};
const uploadCollectionName={value:'Synthetic selection'};
const rawSelectionFingerprint=async()=>'a'.repeat(64),selectionNonce=()=>'b'.repeat(32);
const preflightVersion=1,maximumUploadBatchItems=1,maximumPreflightRequestBytes=10000,csrfToken='synthetic';
const uploadPreflightStatus={textContent:''},link={},linkBox={hidden:true};
const document={querySelector:selector=>selector==='[data-intake-receipt-open]'?link:linkBox};
const readUploadJson=response=>response.json();
const calls=[];let receiptId='intake-'+'c'.repeat(32);
const fetch=async(url,options)=>{calls.push([url,JSON.parse(options.body)]);return {json:async()=>({receipt_id:receiptId,state:'ready',recorded_count:2})};};
''' + source + r'''
(async()=>{
 const files=[{name:'a.txt',size:1,type:'text/plain'},{name:'b.txt',size:2,type:'text/plain'}];
 const preview={eligible_indexes:[0,1],items:files.map(()=>({path_safety_validated:true,state:'ready'}))};
 const result=await recordConfirmedSelection(files,preview,1);
 assert.equal(result.receipt_id,receiptId);
 assert.deepEqual(calls.map(x=>x[0]),[uploadForm.dataset.intakeUrl,uploadForm.dataset.intakeUrl+'/'+receiptId+'/items',uploadForm.dataset.intakeUrl+'/'+receiptId+'/items',uploadForm.dataset.intakeUrl+'/'+receiptId+'/seal']);
 assert.deepEqual(calls.slice(1,3).map(x=>x[1].start),[0,1]);
 assert.equal(link.href,'/matters/m-000000000001/intake/'+receiptId);assert.equal(linkBox.hidden,false);
 const saved=storage.getItem(intakeResumeKey);calls.length=0;
 await recordConfirmedSelection(files,preview,1);assert.equal(storage.getItem(intakeResumeKey),saved);
 assert.equal(calls[0][1].selection_key,'b'.repeat(32));
 for(const invalid of ['../other','intake-short','https://synthetic.invalid/receipt']){
  receiptId=invalid;calls.length=0;await assert.rejects(recordConfirmedSelection(files,preview,1),/could not be verified/);
  assert.equal(calls.length,1);
 }
 calls.length=0;await assert.rejects(recordConfirmedSelection(files,preview,2),/selection changed/);assert.equal(calls.length,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_assistant_fragment_paths_encode_conversation_and_discard_remote_origin():
    run_node("const assert=require('node:assert/strict'); const window={location:{origin:'https://synthetic.example.test'}}; let assistantDock;\n" +
             declaration('assistantFragmentUrl') + r'''
assistantDock={dataset:{fragmentUrl:'/matters/m-1/assistant?assistant_from=sources'}};
const result=new URL(assistantFragmentUrl('conversation/one?x=1#fragment'),window.location.origin);
assert.equal(result.origin,window.location.origin);assert.equal(result.pathname,'/matters/m-1/assistant');
assert.equal(result.searchParams.get('conversation'),'conversation/one?x=1#fragment');
assert.equal(result.searchParams.get('assistant_from'),'sources');assert.equal(result.hash,'');
assistantDock.dataset.fragmentUrl='https://foreign.example.test/matters/m-2/assistant';
assert.equal(assistantFragmentUrl('conversation-two'),'/matters/m-2/assistant?conversation=conversation-two');
assistantDock=null;assert.equal(assistantFragmentUrl('ignored'),'');
''')


def node_expressions(relative, node_type, predicate):
    """Extract real expressions; expected results below do not come from receipts."""
    source = (ROOT / relative).read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(source)
    pending = [tree.root_node]
    found = []
    while pending:
        node = pending.pop()
        if node.type == node_type and predicate(node.text.decode()):
            found.append((node.start_point.row + 1, node.text.decode()))
        pending.extend(node.named_children)
    return sorted(found)


@pytest.mark.parametrize('filename', [
    'browser-accept-assistant-passage.cjs', 'browser-accept-manual-review.cjs',
    'browser-accept-static-site.cjs', 'qa-people-browser.cjs', 'qa-team-groups-browser.cjs',
])
def test_browser_artifact_paths_keep_fixed_leaves_under_explicit_roots(filename):
    calls = node_expressions('scripts/' + filename, 'call_expression', lambda text: text.startswith('path.join('))
    expected = {
        'browser-accept-assistant-passage.cjs': ['.venv/bin/python','synthetic-dock-save.png','synthetic-dock-save-mobile.png','receipt.json','synthetic-failure.png','receipt.json'],
        'browser-accept-manual-review.cjs': ['.venv/bin/python','receipt.json','synthetic-pdf-1280x604.png','synthetic-human-note-1280x604.png','synthetic-video-human-note-1280x604.png','synthetic-video-1280x604.png','synthetic-video-1280x604.png','synthetic-notebook-tools-1280x604.png','receipt.json','synthetic-failure.png','receipt.json'],
        'browser-accept-static-site.cjs': ['site','_headers','mobile.png','mobile-bottom.png','receipt.json'],
        'qa-people-browser.cjs': ['setup-desktop.png','setup-mobile.png','people-desktop.png','people-mobile.png'],
        'qa-team-groups-browser.cjs': ['team-groups-desktop.png','team-groups-mobile.png','matter-access-reasons.png'],
    }[filename]
    expressions = '[' + ','.join(source for _, source in calls) + ']'
    run_node("const assert=require('node:assert/strict');\n" + r'''
for(const path of [require('node:path').posix,require('node:path').win32]){
 const root=path.resolve('/synthetic/checkout'),output=path.resolve('/synthetic/evidence'),site=path.join(root,'site');
 const qa=name=>{assert.equal(name,'ARTIFACTS');return output;};
 const width=390,size={width:1280,height:604};
 const actual=EXPRESSIONS,leaves=LEAVES;
 assert.equal(actual.length,leaves.length);
 actual.forEach((value,index)=>{
  const leaf=leaves[index];const base=leaf==='.venv/bin/python'||leaf==='site'?root:leaf==='_headers'?site:output;
  assert.equal(value,path.resolve(base,leaf));
  assert.ok(value.startsWith(base+path.sep));
 });
}
'''.replace('EXPRESSIONS', expressions).replace('LEAVES', json.dumps(expected)))


def test_static_site_request_paths_cannot_escape_lexical_root():
    relative = 'scripts/browser-accept-static-site.cjs'
    source = declaration('file', relative)
    run_node("const assert=require('node:assert/strict'); const path=require('node:path'); const site='/synthetic/site';\n" +
             'function resolve(pathname) {\n' + source + r'''
return {file,inside:file.startsWith(site+path.sep)}; }
assert.deepEqual(resolve('/'),{file:'/synthetic/site/index.html',inside:true});
assert.deepEqual(resolve('/images/example.svg'),{file:'/synthetic/site/images/example.svg',inside:true});
for(const input of ['/../outside','/../../site-other/file'])assert.equal(resolve(input).inside,false);
assert.equal(resolve('/%2e%2e/outside').file,'/synthetic/site/%2e%2e/outside');
''')


def test_static_site_origin_uses_only_the_allocated_loopback_port():
    run_node("const assert=require('node:assert/strict'); const server={address:()=>({port:43210})};\n" +
             declaration('origin', 'scripts/browser-accept-static-site.cjs') + r'''
assert.equal(origin,'http://127.0.0.1:43210');
assert.equal(new URL(origin).hostname,'127.0.0.1');
''')


@pytest.mark.parametrize('filename,lines', [
    ('browser-accept-assistant-passage.cjs', [58,87,99,100,104,110,117,118,120,126,135,141,160,162]),
    ('browser-accept-manual-review.cjs', [104,110,113,139,148,149,151,168,169,171,183,200,201,224,274,340,341,369,373,383,396,404,406,410,420,428,432,493,501,540,564,565,584,594,616,624,638,646,655,678,706,709]),
    ('qa-people-browser.cjs', [19,28,35,53,61,62,63,64,65,67,82,84]),
    ('qa-team-groups-browser.cjs', [16,23,33,39,40,41,58,63,64,65,66,72,75,76,79,80]),
])
def test_synthetic_browser_routes_stay_on_fixture_origin_and_matter(filename, lines):
    relative = 'scripts/' + filename
    templates = node_expressions(relative, 'template_string', lambda source: '${' in source)
    additions = node_expressions(relative, 'binary_expression', lambda source: source.startswith('fixture.base'))
    selected = [(line, source) for line, source in templates + additions if line in lines]
    assert {line for line, _ in selected} == set(lines)
    expressions = '[' + ','.join(source for _, source in selected) + ']'
    run_node("const assert=require('node:assert/strict');\n" + r'''
const origin='https://127.0.0.1:8443',slug='m-000000000001',slugs=[slug,'m-000000000002'],i=1;
const username='first.reviewer';
const fixture={base:origin,matter:'/matters/'+slug,reader:'/matters/'+slug+'/sources/synthetic-source?unit=1',media:'/matters/'+slug+'/sources/synthetic-video',save_url:'/matters/'+slug+'/assistant/save'};
const statusUrl=fixture.matter+'/answer-jobs/synthetic-status',terminalStatusUrl=fixture.matter+'/answer-jobs/synthetic-terminal-status';
const action=fixture.matter+'/assistant',askAction=action,newerAction=action,scopedAction=action;
const noteAction=fixture.matter+'/sources/synthetic-source/notes',url=fixture.reader;
const savedNavigation={item_id:'note-0001'},intentionalSave={item_id:'note-0002'},savedMedia={item_id:'note-0003'};
const route={request:()=>({url:()=>origin+statusUrl})};let sequence=0;
for(const raw of EXPRESSIONS){
 const value=raw.startsWith('**')?raw.slice(2):raw;
 const parsed=new URL(value,origin);
 assert.equal(parsed.origin,origin);
 const parts=parsed.pathname.split('/');
 assert.ok(['auth','admin','matters','synthetic-note-evidence'].includes(parts[1]),raw);
 if(parts[1]==='matters'&&parts[2]!=='new')assert.ok(slugs.includes(parts[2]),raw);
 assert.equal(parsed.hash,'');assert.ok(!parsed.pathname.includes('undefined'));
}
'''.replace('EXPRESSIONS', expressions))


def test_application_route_suffixes_preserve_scope_and_encoding():
    wanted = [66,1394,1602,1605,1630,3746,4700]
    expressions = dict(node_expressions('src/case_intelligence/static/case-intelligence.js',
                                       'template_string', lambda source: '${' in source))
    source = '[' + ','.join(expressions[line] for line in wanted) + ']'
    run_node("const assert=require('node:assert/strict');\n" + r'''
const details={dataset:{contextUrl:'/matters/m-1/conversations/c-1/messages/msg-1/cited-context/0/1'}};
const activeUpload={status_url:'/matters/m-1/upload-sessions/upload-session-1?existing=1'},separator='&';
const sessionRoot='/matters/m-1/upload-sessions',resumeState={session_id:'../other?x=1#fragment'};
const sessionUrl='/matters/m-1/upload-sessions/upload-session-1';
const next=new URL('https://foreign.example.test/matters/m-1/assistant?assistant_from=sources#ignored');
const base='/matters/m-1/sources/source-1/content?download=0#page=2',fit='width';
assert.deepEqual(EXPRESSIONS,[
 '/matters/m-1/conversations/c-1/messages/msg-1/cited-context/0/1?format=json',
 '/matters/m-1/upload-sessions/upload-session-1?existing=1&compact=1',
 '/matters/m-1/upload-sessions/..%2Fother%3Fx%3D1%23fragment',
 '/matters/m-1/upload-sessions/upload-session-1?compact=true',
 '/matters/m-1/upload-sessions/upload-session-1/cancel',
 '/matters/m-1/assistant?assistant_from=sources',
 '/matters/m-1/sources/source-1/content?download=0#page=2&view=FitH&navpanes=0'
]);
'''.replace('EXPRESSIONS', source))


def test_request_keys_use_crypto_bytes_and_fixed_request_namespace():
    run_node("const assert=require('node:assert/strict');\n" + r'''
const answerRequestKey={value:'unchanged'},field={value:'unchanged'};
const assistantDock={querySelector:()=>field};
const window={crypto:{randomUUID:()=> '00010203-0405-0607-0809-0a0b0c0d0e0f',getRandomValues:bytes=>bytes.forEach((_,index)=>bytes[index]=index)}};
''' + declarations('newAnswerRequestKey','newAssistantRequestKey') + r'''
newAnswerRequestKey();newAssistantRequestKey();
assert.equal(answerRequestKey.value,'answer-request-000102030405060708090a0b0c0d0e0f');
assert.equal(field.value,answerRequestKey.value);
window.crypto={};answerRequestKey.value='kept';field.value='kept';
newAnswerRequestKey();newAssistantRequestKey();assert.equal(answerRequestKey.value,'kept');assert.equal(field.value,'kept');
''')


@pytest.mark.parametrize('filename', ['case-intelligence.js', 'briefing.js', 'one-box.js'])
def test_all_event_helpers_preserve_fixed_callsite_suffixes(filename):
    helpers = node_expressions('src/case_intelligence/static/' + filename, 'variable_declarator',
                               lambda source: source.startswith('onWorkbenchEvent ='))
    assert len(helpers) == (3 if filename == 'case-intelligence.js' else 1)
    for _, source in helpers:
        run_node("const assert=require('node:assert/strict'); const calls=[]; const window={addEventListener:(...args)=>calls.push(args)}; const " + source + r''';
const handler=()=>{};
for(const name of ['readiness','answer-complete']){
 calls.length=0;onWorkbenchEvent(name,handler);
 assert.deepEqual(calls.map(x=>x[0]),['exculpata:'+name,LEGACY_PREFIX+name]);
 assert.ok(calls.every(x=>x[1]===handler));
}
'''.replace('LEGACY_PREFIX', json.dumps('record' + 'bench:')))


def test_upload_serialization_keeps_exact_utf8_bytes_and_boundary_accounting():
    source = '\n'.join(declaration(name) for name in ['preflightDescriptor', 'localPreflightFailure', 'buildPreflightBatches'])
    run_node("const assert=require('node:assert/strict');\n" + source + r'''
let maximumUploadBatchItems=2;
let maximumPreflightRequestBytes=10000;
const files=[
 {name:'synthetic-α.txt',size:7,type:'text/plain',webkitRelativePath:'folder/synthetic-α.txt'},
 {name:'synthetic-😀.txt',size:11,type:'text/plain',webkitRelativePath:''},
 {name:'synthetic-c.txt',size:13,type:'text/plain',webkitRelativePath:''}
];
const checkpoint={session_id:'synthetic-session',collection_id:'synthetic-collection'};
const expected=(selected)=>JSON.stringify({selection_nonce:'synthetic-nonce',checkpoint_session_id:'synthetic-session',checkpoint_collection_id:'synthetic-collection',files:selected.map(preflightDescriptor)});
let result=buildPreflightBatches(files,'synthetic-nonce',checkpoint);
assert.deepEqual(result.batches.map(x=>x.body),[expected(files.slice(0,2)),expected(files.slice(2))]);
for(const batch of result.batches)assert.equal(batch.bytes,Buffer.byteLength(batch.body,'utf8'));
assert.equal(result.failures.size,0);
maximumPreflightRequestBytes=Buffer.byteLength(expected(files.slice(0,1)),'utf8');
result=buildPreflightBatches(files.slice(0,1),'synthetic-nonce',checkpoint);
assert.equal(result.batches.length,1);
maximumPreflightRequestBytes-=1;
result=buildPreflightBatches(files.slice(0,1),'synthetic-nonce',checkpoint);
assert.equal(result.batches.length,0);
assert.deepEqual([...result.failures.keys()],[0]);
assert.equal(result.failures.get(0).display_name,'Selected file 1');
''')


def test_existing_event_helper_retains_alias_names_and_interpolation_order():
    helper = declaration('onWorkbenchEvent')
    run_node("const assert=require('node:assert/strict'); const calls=[]; const window={addEventListener:(...args)=>calls.push(args)};\n" + helper + r'''
const conversions=[];
const name={ [Symbol.toPrimitive](hint){conversions.push(hint);return 'readiness';} };
const handler=()=>{};
onWorkbenchEvent(name,handler);
assert.deepEqual(conversions,['string','string']);
assert.deepEqual(calls.map(x=>x[0]),['exculpata:readiness',LEGACY_EVENT]);
assert.ok(calls.every(x=>x[1]===handler));
'''.replace('LEGACY_EVENT', json.dumps('record' + 'bench:readiness')))


def test_range_headers_and_body_keep_inclusive_byte_bounds():
    source = (ROOT / 'site-worker/range.js').read_text().replace('export default', 'const worker =', 1)
    run_node("const assert=require('node:assert/strict');\n" + source + r'''
(async()=>{
const env={ASSETS:{fetch:async()=>new Response(Uint8Array.from([10,11,12,13,14]),{headers:{'Content-Length':'5'}})}};
for(const [range,expected,status,bytes] of [['bytes=1-3','bytes 1-3/5',206,[11,12,13]],['bytes=-2','bytes 3-4/5',206,[13,14]],['bytes=8-9','bytes */5',416,[]]]){
 const response=await worker.fetch(new Request('https://synthetic.example.test/video.mp4',{headers:{Range:range}}),env);
 assert.equal(response.status,status);assert.equal(response.headers.get('Content-Range'),expected);
 assert.deepEqual([...new Uint8Array(await response.arrayBuffer())],bytes);
 if(status===206)assert.equal(response.headers.get('Content-Length'),String(bytes.length));
}
const head=await worker.fetch(new Request('https://synthetic.example.test/video.mp4',{method:'HEAD',headers:{Range:'bytes=1-3'}}),env);
assert.equal(head.headers.get('Content-Length'),'3');assert.equal((await head.arrayBuffer()).byteLength,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
