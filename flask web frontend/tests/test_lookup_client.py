"""Exercise real browser lookup broker under row bursts and restoration."""
import subprocess


def test_rows_repeated_ids_and_restore_share_batched_successes():
    script = r'''
const assert = require('node:assert/strict');
const {create} = require('./static/lookup_client.js');
(async () => {
 let requests = [], now = 0, fail = false;
 const client = create(async ids => {
   requests.push(ids);
   if (fail) throw Object.assign(new Error('upstream busy'), {status: 429});
   return {items: ids.map(id => ({inat_id: id}))};
 }, {now: () => now});
 const rows = Array.from({length: 65}, (_, i) => String(i + 1));
 const results = await Promise.all(rows.map(id => client.lookup([id])));
 assert.equal(results.length, 65);
 assert.deepEqual(requests.map(ids => ids.length), [30, 30, 5]);
 await Promise.all([client.lookup(rows), client.lookup(['1', '1', '2'])]);
 assert.equal(requests.length, 3); // queue restore and duplicate row success
 now += 900001;
 fail = true;
 let errors = await client.lookup(['1', '2']);
 assert.equal(errors[0].status, 429);
 assert.equal(errors[0].retryable, true);
 fail = false;
 let success = await client.lookup(['1', '2']); // failed batch can be retried
 assert.equal(success[0].inat_id, '1');
 assert.equal(requests.length, 5);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
