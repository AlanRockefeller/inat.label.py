(function () {
    "use strict";

    const endpoint = "/labels/client_event";
    const sent = new Set();
    let remaining = 20;

    function text(value) {
        if (value === undefined || value === null) {
            return "";
        }
        try {
            return typeof value === "string" ? value : JSON.stringify(value);
        } catch (_) {
            return String(value);
        }
    }

    function report(type, details) {
        if (remaining <= 0) {
            return;
        }

        const payload = Object.assign({
            type: type,
            page_url: window.location.href
        }, details || {});
        const body = JSON.stringify(payload);
        const fingerprint = [type, payload.message, payload.source, payload.line].join("|");
        if (sent.has(fingerprint)) {
            return;
        }
        sent.add(fingerprint);
        remaining -= 1;

        try {
            if (navigator.sendBeacon) {
                const blob = new Blob([body], { type: "application/json" });
                if (navigator.sendBeacon(endpoint, blob)) {
                    return;
                }
            }
            window.fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: body,
                credentials: "same-origin",
                keepalive: true
            }).catch(function () {});
        } catch (_) {
            // Telemetry must never interfere with the label generator UI.
        }
    }

    window.addEventListener("error", function (event) {
        if (event.target && event.target !== window) {
            report("resource_load_error", {
                message: "Failed to load page resource",
                source: event.target.src || event.target.href || event.target.tagName
            });
            return;
        }

        report("javascript_error", {
            message: event.message,
            source: event.filename,
            line: event.lineno,
            column: event.colno,
            stack: event.error && event.error.stack
        });
    }, true);

    window.addEventListener("unhandledrejection", function (event) {
        const reason = event.reason;
        report("unhandled_rejection", {
            message: text(reason && reason.message ? reason.message : reason),
            stack: reason && reason.stack
        });
    });
})();
