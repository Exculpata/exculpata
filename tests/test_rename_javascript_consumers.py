"""Execute the source-locked construction consumers with synthetic inputs."""
import json
from pathlib import Path
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


def declaration(name):
    source = (ROOT / 'src/case_intelligence/static/case-intelligence.js').read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(source)
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type == 'variable_declarator' and node.child_by_field_name('name').text.decode() == name:
            return node.parent.text.decode()
        pending.extend(node.named_children)
    raise AssertionError(name)


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
