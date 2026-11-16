import json
import subprocess
from pathlib import Path


def test_sse_and_polling_can_only_download_and_archive_once():
    html = (Path(__file__).resolve().parents[1] / 'templates/index.html').read_text()
    start = html.index('                let settled = false;')
    end = html.index('                const pollStatus = async', start)
    script = html[start:end]
    harness = '''
const vm = require('node:vm'), assert = require('node:assert/strict');
let downloads = 0, archived = 0, closed = 0;
const node = {html() {return this;}, removeClass() {return this;}, prop() {return this;}, text() {return this;}, append() {return this;}};
const ctx = vm.createContext({
 btn: node, originalText: 'Print', downloadName: 'labels.pdf', successMsg: 'ready',
 $: () => node, clearTimeout() {}, es: {close() {closed++;}},
 recordSuccessfulPrint() {archived++;}, showToast() {}, showErrorModal() {}, escapeHtml: x => x,
 document: {createElement: () => ({click() {downloads++;}})},
});
vm.runInContext(SCRIPT, ctx);
vm.runInContext("finishPrint({success: true, download_url: '/file.pdf'}); finishPrint({success: true, download_url: '/file.pdf'});", ctx);
assert.equal(downloads, 1); assert.equal(archived, 1); assert.equal(closed, 1);
'''.replace('SCRIPT', json.dumps(script))
    subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)


def test_healthy_sse_stops_polls_and_watchdog_recovers_status():
    html = (Path(__file__).resolve().parents[1] / 'templates/index.html').read_text()
    start = html.index('                let settled = false;')
    end = html.index('                es.addEventListener("log",', start)
    script = html[start:end]
    harness = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
(async () => {
let fetched = 0, downloads = 0, timers = new Map(), serial = 0;
const node = {html(){return this}, removeClass(){return this}, prop(){return this}, text(){return this}, append(){return this}};
const es = {close(){}, addEventListener(name, fn){this[name] = fn}};
const ctx = vm.createContext({
 btn: node, originalText: 'Print', downloadName: 'labels.pdf', successMsg: 'ready', job_id: 'test-job',
 $: () => node, es, AbortController, encodeURIComponent,
 setTimeout(fn, delay){const key = ++serial; timers.set(key, {fn, delay}); return key},
 clearTimeout(key){timers.delete(key)}, recordSuccessfulPrint(){}, showToast(){}, escapeHtml:x=>x,
 fetch: async () => {fetched++; return {ok: true, status:200, json:async()=>({status:'completed',success:true,download_url:'/file.pdf'})}},
 document: {createElement: () => ({click(){downloads++}})},
});
vm.runInContext(SCRIPT, ctx);
assert.equal(timers.size, 1);
es.onopen();
await vm.runInContext('pollStatus()', ctx);
assert.equal(fetched, 0);
assert.equal(timers.size, 1);
const watchdog = [...timers.values()][0];
assert.equal(watchdog.delay, 20000);
watchdog.fn();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(fetched, 1); assert.equal(downloads, 1);
})().catch(e=>{console.error(e); process.exitCode=1});
'''.replace('SCRIPT', json.dumps(script))
    subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)
