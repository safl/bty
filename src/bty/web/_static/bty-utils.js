// bty-utils.js -- shared client-side helpers.
//
// Three pages (Backups / Downloads / Hashing) used to carry identical
// copies of fmtBytes() + esc(). Centralised here so a future tweak
// (rounding precision, byte-unit labels, escape semantics) is a one-
// place change. Loaded by _layout.html so every authed page sees
// ``window.btyUtils``.
(function (root) {
    function esc(s) {
        // HTML-entity escape for safe textContent-like interpolation
        // into template strings. Mirrors the shape every <script>
        // block in the UI used inline before this file existed.
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            }[c];
        });
    }

    function fmtBytes(n) {
        // Bytes -> human-readable; 1024 base; one decimal place beyond
        // KiB. Returns "-" for null/undefined so call sites can pass
        // raw state fields without guarding first.
        if (n == null) return "-";
        if (n < 1024) return n + " B";
        var units = ["KiB", "MiB", "GiB", "TiB"], i = -1;
        do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
        return n.toFixed(1) + " " + units[i];
    }

    // Copy-to-clipboard binding: any element with class
    // ``bty-copy`` and a ``data-copy="<text>"`` attribute becomes
    // clickable to copy that text. Falls back to a hidden
    // textarea + ``document.execCommand("copy")`` for browsers
    // that ship without a working ``navigator.clipboard`` (older
    // Safari; non-HTTPS contexts; some embedded WebView shells).
    // On success the element pulses a tiny visual confirmation
    // (text content swapped to a check mark for ~1s) so the
    // operator gets feedback without a separate toast.
    //
    // Wire-up is event-delegated on ``document``, so adding the
    // class + attribute to a fresh DOM node (htmx swap, SSE
    // re-render) is immediately functional without a re-bind.
    //
    // Per-row labels stay flexible: place ``data-copy`` on the
    // outer ``<button>`` carrying the full value (a 64-char
    // sha256 / MAC / image-ref / URL); the visible content can
    // be a truncated ``<code>{{ sha[:8] }}</code>`` followed by
    // the clipboard icon. ``data-copy-label`` overrides the
    // post-click flash text (default: a unicode check mark).
    function fallbackCopy(text) {
        try {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.setAttribute("readonly", "");
            ta.style.position = "absolute";
            ta.style.left = "-9999px";
            document.body.appendChild(ta);
            ta.select();
            var ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        } catch (_e) {
            return false;
        }
    }

    function flashCopy(el) {
        var icon = el.querySelector(".bi-clipboard");
        if (!icon) return;
        icon.classList.remove("bi-clipboard");
        icon.classList.add("bi-clipboard-check", "text-success");
        setTimeout(function () {
            icon.classList.add("bi-clipboard");
            icon.classList.remove("bi-clipboard-check", "text-success");
        }, 1200);
    }

    function copyText(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(
                function () { return true; },
                function () { return fallbackCopy(text); }
            );
        }
        return Promise.resolve(fallbackCopy(text));
    }

    document.addEventListener("click", function (ev) {
        var target = ev.target.closest(".bty-copy");
        if (!target) return;
        ev.preventDefault();
        var text = target.getAttribute("data-copy");
        if (!text) return;
        copyText(text).then(function (ok) { if (ok) flashCopy(target); });
    });

    root.btyUtils = { esc: esc, fmtBytes: fmtBytes, copyText: copyText };
})(window);
