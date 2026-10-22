"""On-demand LAN-facing HTTP server for one-shot .apkg export downloads.

Not started until an export is triggered, and torn down again shortly
after. This deliberately does NOT reuse DeliveryServer's loopback-only
pattern (deliver.py) — the whole point is letting a phone on the same
Wi-Fi reach it — but it still avoids sitting open on the network any
longer than it has to: an unguessable per-export token path, plus a
debounced auto-shutdown (short idle grace window, hard cap), close the
exposure window.
"""

import asyncio
import contextlib
import logging
import secrets
import socket
import time

logger = logging.getLogger(__name__)

FILENAME = "kumadeck-export.apkg"
MAX_S = 300      # hard cap regardless of activity
GRACE_S = 20     # keep serving this long after the last hit, so a mobile
                 # browser's multi-request "Open in…" download flow (a
                 # HEAD/Range probe before the real fetch is common) isn't
                 # cut off mid-transfer


def get_lan_ip() -> str | None:
    """Best-effort LAN-facing IP — no packets are actually sent, this only
    asks the OS which local interface would route to the internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class AnkiExportServer:
    def __init__(self):
        self._runner = None
        self._served = asyncio.Event()
        self._shutdown_task = None

    async def start(self, file_path: str) -> str:
        """Start serving `file_path`; returns the download URL."""
        await self.stop()  # defensive: never let a stale server linger

        ip = get_lan_ip()
        if not ip:
            raise OSError(
                "No network connection detected — connect to Wi-Fi to export")

        from aiohttp import web

        token = secrets.token_urlsafe(16)

        async def handler(_request):
            self._served.set()
            return web.FileResponse(
                file_path,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": f'attachment; filename="{FILENAME}"',
                })

        app = web.Application()
        app.router.add_get(f"/{token}/{FILENAME}", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, 0))
        sock.listen(128)
        port = sock.getsockname()[1]

        site = web.SockSite(self._runner, sock)
        await site.start()

        self._served.clear()
        self._shutdown_task = asyncio.create_task(self._auto_shutdown())
        url = f"http://{ip}:{port}/{token}/{FILENAME}"
        logger.info(f"anki export server on {url}")
        return url

    async def _auto_shutdown(self):
        try:
            deadline = time.monotonic() + MAX_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._served.wait(), timeout=remaining)
                except TimeoutError:
                    break  # nobody ever fetched it
                self._served.clear()
                grace = min(GRACE_S, deadline - time.monotonic())
                if grace <= 0:
                    break
                try:
                    await asyncio.wait_for(self._served.wait(), timeout=grace)
                    continue  # another hit during the grace window — extend
                except TimeoutError:
                    break  # quiet for GRACE_S — done
        except asyncio.CancelledError:
            return
        finally:
            await self.stop()

    async def stop(self):
        task, self._shutdown_task = self._shutdown_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if self._runner is not None:
            runner, self._runner = self._runner, None
            with contextlib.suppress(Exception):
                await runner.cleanup()
