"""The first-run screen: now a shell for the guided wizard.

3.1.0 redesign. The historical incident this module remembers: the old page
generated its JavaScript inside a Python triple-quoted string, and backslash
escaping silently corrupted the JS until no button on the page worked. The
fix is structural, not cosmetic —

* this module renders ONLY static HTML markup: ``<link>`` and ``<script src>``
  tags, no inline ``<script>`` body at all;
* all behaviour lives in ``app/static/arena-wizard.js``, a real file served
  byte-for-byte by ``/static`` — it can never be mangled by Python string
  escaping again;
* asset URLs are fingerprinted with ``<version>-<commit>``, so an upgraded
  Arena can never show a stale cached page.

``render()`` keeps its historical signature (optional diagnostics dict) so
existing callers and tests continue to work.
"""

from __future__ import annotations

from app import build_info

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Arena __VERSION__ — Setup</title>
<link rel="stylesheet" href="/static/arena.css?v=__BUILD__">
</head>
<body>
<div id="wizard-root" style="max-width:980px;margin:0 auto;padding:30px 20px">
  <noscript>
    <div style="border:1px solid #2e3950;border-radius:10px;padding:18px;font-family:sans-serif;color:#e6ebf4;background:#121826">
      <b>Arena setup needs JavaScript enabled in this browser.</b><br>
      This page is served locally by Arena itself (127.0.0.1); enabling
      scripts here is safe and required.
    </div>
  </noscript>
  <div style="color:#7d89a3;font-family:sans-serif;padding:40px 0">Loading Arena setup…</div>
</div>
<script src="/static/arena-core.js?v=__BUILD__"></script>
<script src="/static/arena-wizard.js?v=__BUILD__"></script>
</body>
</html>
"""


def render(diagnostics: dict | None = None) -> str:
    """The wizard shell. Pure static markup — no secrets, no inline scripts."""
    return (_PAGE
            .replace("__VERSION__", build_info.version())
            .replace("__BUILD__", build_info.build_id()))
