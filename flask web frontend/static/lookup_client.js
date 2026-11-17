/* Batch row events and share successful lookups within an open page. */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.LabelsLookupClient = factory();
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function create(transport, options = {}) {
        const cache = new Map(), pending = new Map();
        const now = options.now || Date.now;
        let timer = null, active = false;
        const schedule = () => {
            if (!active && timer === null && pending.size) timer = setTimeout(flush, 40);
        };
        async function flush() {
            timer = null;
            active = true;
            const batch = [...pending.entries()].filter(([, value]) => !value.sent).slice(0, 30);
            batch.forEach(([, value]) => { value.sent = true; });
            try {
                const response = await transport(batch.map(([key]) => key));
                batch.forEach(([key, value], index) => {
                    const item = response.items?.[index] || {error: 'No lookup result returned', retryable: true, status: 503};
                    if (!item.error) {
                        cache.delete(key);
                        cache.set(key, {item, expires: now() + 900000});
                        if (cache.size > 500) cache.delete(cache.keys().next().value);
                    }
                    pending.delete(key);
                    value.resolve(item);
                });
            } catch (error) {
                batch.forEach(([key, value]) => {
                    pending.delete(key);
                    value.resolve({error: error.message || 'Could not fetch observation',
                                   status: error.status || 503, retryable: true});
                });
            } finally {
                active = false;
                schedule();
            }
        }
        function lookup(ids) {
            return Promise.all(ids.map(key => {
                key = String(key).trim();
                const cached = cache.get(key);
                if (cached && cached.expires > now()) return Promise.resolve(cached.item);
                if (pending.has(key)) return pending.get(key).promise;
                let resolve;
                const promise = new Promise(done => { resolve = done; });
                pending.set(key, {promise, resolve, sent: false});
                schedule();
                return promise;
            }));
        }
        return {lookup};
    }
    return {create};
}));
