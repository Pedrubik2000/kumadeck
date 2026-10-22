"""Getting captured text in front of Yomitan.

Primary path: a tiny local web server (aiohttp ships with Decky Loader).
It serves a texthooker-style page at http://127.0.0.1:<port>/ and pushes
each captured line over a WebSocket at /ws. Open the page in a Firefox
window (with Yomitan) running as a non-Steam app; every capture appears
instantly and Yomitan hover-lookup works on the accumulated lines. The
/ws endpoint speaks the plain-text protocol texthooker UIs expect, so
renji-xd/texthooker-ui etc. can connect instead of the built-in page.

Secondary path: the FRONTEND copies the text to the clipboard inside
Steam's CEF (execCommand trick, as decky-clipboard does). gamescope syncs
UTF-8 clipboard across all its XWayland servers (SteamOS >= 3.7.14), so
Firefox + Yomitan's clipboard monitor see it. Note: wl-copy does NOT work
under gamescope (no data-control protocol), which is why no clipboard
handling lives in this backend.
"""

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kumadeck</title>
<style>
  :root { color-scheme: dark; }
  body { background:#111; color:#eee; font-family: "Noto Sans CJK JP","Hiragino Sans",sans-serif;
         margin:0; padding:1rem 1rem 40vh; font-size:1.6rem; line-height:1.9; }
  #status { position:fixed; top:.4rem; right:.8rem; font-size:.8rem; color:#888; }
  #status.ok { color:#4a4; }
  p.line { margin:.3em 0; border-bottom:1px solid #222; padding-bottom:.3em; }
  p.line:last-child { color:#fff; }
</style></head><body>
<div id="status">connecting…</div>
<div id="lines"></div>
<script>
  const status = document.getElementById('status');
  const lines = document.getElementById('lines');
  function connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => { status.textContent = 'connected'; status.className = 'ok'; };
    ws.onclose = () => { status.textContent = 'reconnecting…'; status.className = '';
                         setTimeout(connect, 1500); };
    ws.onmessage = (ev) => {
      let text = ev.data;
      try { const o = JSON.parse(ev.data); if (o && o.sentence) text = o.sentence; } catch (e) {}
      if (!text) return;
      const p = document.createElement('p');
      p.className = 'line';
      p.textContent = text;
      lines.appendChild(p);
      window.scrollTo(0, document.body.scrollHeight);
    };
  }
  connect();
</script></body></html>"""


class DeliveryServer:
    def __init__(self, port: int = 8766):
        self.port = port
        self._runner = None
        self._clients = set()
        self._history = []  # replayed to newly connected clients

    async def start(self):
        if self._runner is not None:
            return
        from aiohttp import web

        async def index(_request):
            return web.Response(text=PAGE, content_type="text/html")

        async def ws_handler(request):
            ws = web.WebSocketResponse(heartbeat=30)
            await ws.prepare(request)
            self._clients.add(ws)
            logger.info(f"texthooker client connected ({len(self._clients)} total)")
            try:
                for line in self._history[-50:]:
                    await asyncio.wait_for(ws.send_str(line), timeout=2)
                async for _msg in ws:
                    pass  # clients only listen
            except TimeoutError:
                pass
            finally:
                self._clients.discard(ws)
            return ws

        app = web.Application()
        app.router.add_get("/", index)
        app.router.add_get("/ws", ws_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # loopback only: gamescope Firefox is on the same host, and this
        # serves your whole captured-dialogue history unauthenticated
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info(f"delivery server on http://127.0.0.1:{self.port}/")

    async def stop(self):
        if self._runner is not None:
            for ws in list(self._clients):
                with contextlib.suppress(Exception):
                    await ws.close()
            await self._runner.cleanup()
            self._runner = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, text: str):
        self._history.append(text)
        del self._history[:-200]
        # snapshot: the set mutates when clients (dis)connect during awaits,
        # and a stalled client must not wedge the capture pipeline
        for ws in list(self._clients):
            try:
                await asyncio.wait_for(ws.send_str(text), timeout=2)
            except Exception:
                self._clients.discard(ws)
                with contextlib.suppress(Exception):
                    await ws.close()
