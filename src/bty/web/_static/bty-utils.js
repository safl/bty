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

    root.btyUtils = { esc: esc, fmtBytes: fmtBytes };
})(window);
