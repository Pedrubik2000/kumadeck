"""kumadeck — Decky plugin backend (fork of scallsen/japanese-lookup).

Pipeline per trigger press: capture frame via gamescope's PipeWire source →
OCR (local RapidOCR in a venv subprocess, or Gemini Vision) → rule-based
cleanup → broadcast to the texthooker page (Yomitan hovers it there).
Anki cards (native lookup path) are buffered locally and exported as a
batch .apkg on demand, scanned onto a phone via QR code.
"""

import asyncio
import base64
import os
import re
import time
import uuid

import decky
from vnlookup import cleanup, kuma3
from vnlookup.anki_export import AnkiExportError, build_apkg
from vnlookup.anki_server import AnkiExportServer
from vnlookup.buffer import PendingCards
from vnlookup.capture import CaptureError, PngencMissing, ScreenCapture
from vnlookup.deliver import DeliveryServer
from vnlookup.deps import RuntimeInstaller
from vnlookup.dictionary import Dictionary
from vnlookup.hid_monitor import HidrawButtonMonitor
from vnlookup.models import ModelDownloader
from vnlookup.ocr import GeminiBackend, OCRError, RapidOCRBackend, tokenize
from vnlookup.settings import Settings
from vnlookup.uninstall import (
    disk_usage,
    downloaded_data_paths,
    remove_paths,
    spawn_uninstall_cleanup,
)

logger = decky.logger

RUNTIME_DIR = decky.DECKY_PLUGIN_RUNTIME_DIR
SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR
LOG_DIR = decky.DECKY_PLUGIN_LOG_DIR
PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
CAPTURES_DIR = os.path.join(RUNTIME_DIR, "captures")
PREVIEW_PNG = os.path.join(RUNTIME_DIR, "preview.png")
# one PNG per capture area, keyed by a minted id (see save_area_screenshot) —
# lives under SETTINGS_DIR so it survives updates but is wiped on uninstall
AREA_SCREENSHOTS_DIR = os.path.join(SETTINGS_DIR, "area_screenshots")
AREA_SCREENSHOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# bucket for captures with no Steam appid to key by — matches
# UNKNOWN_APP_KEY in src/api.ts
UNKNOWN_PROFILE_KEY = "unknown"

# role -> settings key naming the note-type field it's written to
ANKI_FIELD_SETTINGS = (
    ("expression", "anki_expression_field"),
    ("reading", "anki_reading_field"),
    ("glossary", "anki_glossary_field"),
    ("word_type", "anki_word_type_field"),
    ("sentence", "anki_sentence_field"),
    ("game", "anki_game_field"),
)

BUSY = {"ok": False, "error": "capture already in progress"}


async def _read_b64(path: str) -> str:
    def read():
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return await asyncio.to_thread(read)


class Plugin:
    async def _main(self):
        # One-time migration from the plugin's old folder name ("vn-lookup")
        # to "kumadeck" — safe/idempotent: each call is a no-op once
        # the old path is gone. Must run before Settings/etc. below read
        # from SETTINGS_DIR/RUNTIME_DIR/LOG_DIR, which already point at the
        # new location.
        decky.migrate_settings(os.path.join(decky.DECKY_HOME, "settings", "vn-lookup"))
        decky.migrate_runtime(os.path.join(decky.DECKY_HOME, "data", "vn-lookup"))
        decky.migrate_logs(os.path.join(decky.DECKY_HOME, "logs", "vn-lookup"))

        self.settings = Settings(SETTINGS_DIR)
        self.installer = RuntimeInstaller(RUNTIME_DIR)
        self.downloader = ModelDownloader(RUNTIME_DIR)
        self.capture = ScreenCapture(
            gst_plugin_path=os.path.join(PLUGIN_DIR, "bin", "gstreamer-1.0"),
            lib_path=os.path.join(PLUGIN_DIR, "bin"),
        )
        self.monitor = HidrawButtonMonitor()
        self.delivery = DeliveryServer(port=int(self.settings.get("texthooker_port")))
        self.anki_buffer = PendingCards(RUNTIME_DIR)
        self.anki_server = AnkiExportServer()
        self.dictionary = Dictionary(
            os.path.join(RUNTIME_DIR, "dictionary.sqlite3"),
            os.path.join(RUNTIME_DIR, "dicts"))
        self._token_cache = {"text": None, "tokens": []}
        self._word_states, self._word_states_at = {}, 0.0

        self._busy = False
        self._last_result = None

        os.makedirs(CAPTURES_DIR, exist_ok=True)
        os.makedirs(AREA_SCREENSHOTS_DIR, exist_ok=True)
        self.monitor.start()
        try:
            await self.delivery.start()
        except Exception as e:
            logger.error(f"delivery server failed to start: {e}")

        logger.info("kumadeck backend up")

    async def _unload(self):
        # Must be first: Decky SIGKILLs this process 5s after calling
        # _unload, no matter what — confirmed happening on every single
        # unload logged so far, including ones well under 5s of real work.
        # subprocess.Popen returns near-instantly, so spawning here survives
        # the SIGKILL; spawning it last (as this used to) meant it never
        # even ran, since the awaits below reliably eat the whole 5s budget.
        #
        # On this Decky Loader build, uninstalling only ever calls _unload —
        # _uninstall (below) is never invoked, confirmed by its absence from
        # the backend log across several real uninstall tests. _unload also
        # fires on every update/restart, so spawning here is a safe no-op
        # then too: the plugin folder never actually disappears for the 15s
        # the watcher requires before it'll delete anything.
        self._spawn_uninstall_watcher()

        monitor = getattr(self, "monitor", None)
        if monitor:
            # stop() joins the reader thread; keep it off the event loop
            await asyncio.to_thread(monitor.stop)
        delivery = getattr(self, "delivery", None)
        if delivery:
            await delivery.stop()
        anki_server = getattr(self, "anki_server", None)
        if anki_server:
            await anki_server.stop()

    async def _uninstall(self):
        # Decky also calls this when updating, and kills us 5s later — so
        # don't delete here; a detached watcher deletes only if the plugin
        # stays gone (see vnlookup/uninstall.py). Kept as a second trigger
        # alongside _unload's call above, in case some Decky builds do call
        # this — spawning the watcher twice is harmless (idempotent).
        self._spawn_uninstall_watcher()

    def _spawn_uninstall_watcher(self):
        folder = os.path.basename(PLUGIN_DIR)
        # every dir Decky gave this plugin is named after its folder; never
        # hand the watcher anything else, even if an env var is off
        paths = [p for p in (RUNTIME_DIR, SETTINGS_DIR, LOG_DIR)
                 if p and os.path.isabs(p) and os.path.basename(p) == folder]
        try:
            spawn_uninstall_cleanup(
                plugin_name=decky.DECKY_PLUGIN_NAME,
                plugins_root=os.path.dirname(PLUGIN_DIR),
                paths=paths,
                log_path="/tmp/kumadeck-uninstall.log")
            logger.info(f"uninstall: cleanup watcher started for {paths}")
        except Exception as e:
            logger.error(f"uninstall: couldn't start cleanup watcher: {e}")

    # ---- helpers -----------------------------------------------------------

    async def _emit(self, stage: str, **payload):
        await decky.emit("vnl_event", {"stage": stage, **payload})

    async def _fail(self, message: str, **extra):
        """Tell the frontend (toast) and return the callable's error result."""
        await self._emit("error", message=message, **extra)
        return {"ok": False, "error": message, **extra}

    def _local_ocr(self) -> RapidOCRBackend:
        return RapidOCRBackend(self.installer.python, self.downloader.target_dir)

    def _areas_for(self, appid: str | None):
        """This game's capture areas, or the Default list if it has none."""
        profiles = self.settings.get("capture_profiles") or {}
        key = str(appid) if appid else UNKNOWN_PROFILE_KEY
        profile = profiles.get(key)
        areas = profile.get("areas") if profile else None
        return areas or self.settings.get("capture_areas") or []

    # ---- trigger support ---------------------------------------------------

    async def get_button_state(self):
        return {"success": True, "buttons": self.monitor.get_button_state()}

    # ---- the pipeline ------------------------------------------------------

    async def capture_and_mine(self, button: str | None = None, appid: str | None = None):
        if self._busy:
            return BUSY
        self._busy = True
        try:
            return await self._run_pipeline(button, appid)
        finally:
            self._busy = False

    async def _run_pipeline(self, button: str | None = None, appid: str | None = None):
        ts = int(time.time() * 1000)
        full_png = os.path.join(CAPTURES_DIR, f"capture_{ts}.png")
        crop_png = os.path.join(CAPTURES_DIR, f"crop_{ts}.png")

        # 1. capture — give the compositor a beat before grabbing the frame
        await self._emit("capturing")
        await asyncio.sleep(0.15)
        try:
            await self.capture.capture_png(full_png)
        except PngencMissing as e:
            # No pngenc in the system GStreamer: grab a raw RGB frame and
            # let the venv (Pillow) encode it instead.
            if not self.installer.is_installed():
                return await self._fail(f"Capture failed: {e}")
            try:
                raw, (w, h) = await self.capture.capture_raw_rgb()
                await self._local_ocr().encode_raw(raw, w, h, full_png)
            except (CaptureError, OCRError) as e2:
                return await self._fail(f"Capture failed: {e2}")
        except CaptureError as e:
            return await self._fail(f"Capture failed: {e}")

        # 2. OCR — the trigger button picks which capture area gets cropped.
        # region rides along on the "ocr" event so the frontend can outline
        # the area actively being scanned, directly over the running game.
        area = next(
            (a for a in self._areas_for(appid) if a.get("button") == button),
            None)
        region = area["region"] if area else None
        await self._emit("ocr", region=region)

        runtime_ok = self.installer.is_installed()
        cloud_full_frame = False
        try:
            if self.settings.get("ocr_backend") == "gemini":
                image_for_ocr = full_png
                if region and runtime_ok:
                    # crop locally so the cloud sees only the text box
                    try:
                        image_for_ocr = await self._local_ocr().crop(
                            full_png, region, crop_png)
                    except OCRError as e:
                        logger.warning(f"crop failed, sending full frame: {e}")
                        cloud_full_frame = True
                elif region:
                    cloud_full_frame = True
                gemini = GeminiBackend(self.settings.get("gemini_api_key"),
                                       self.settings.get("gemini_model"))
                result = await gemini.recognize(image_for_ocr)
            else:
                if not runtime_ok:
                    return await self._fail(
                        "Local OCR runtime not installed — install it in "
                        "the kumadeck settings panel")
                if not self.downloader.is_installed():
                    return await self._fail(
                        "OCR models not downloaded — download them in the "
                        "kumadeck settings panel")
                result = await self._local_ocr().recognize(
                    full_png, region=region, crop_out=crop_png,
                    min_confidence=float(self.settings.get("min_confidence")))
        except OCRError as e:
            return await self._fail(f"OCR failed: {e}")

        # 3. cleanup — first drop UI chrome (Auto/Skip/…) around the box,
        # then normalize. raw_text keeps the unfiltered read for debugging.
        raw_text = result.text
        confidence = round(result.mean_confidence, 3)
        kept, dropped = cleanup.filter_ui_regions(result.regions)
        if dropped:
            logger.info(f"dropped UI fragments: {dropped}")
            result.regions = kept
        cleaned = cleanup.clean_ocr_text(raw_text, remove_speaker=False)

        if not cleaned:
            # total OCR miss: still record it as the last result (empty
            # text) so the polled sidebar shows a "no text found" notice
            # instead of a stale sentence
            self._last_result = {"text": "", "raw": raw_text, "confidence": confidence}
            return await self._fail("No text detected in the capture region",
                                    raw=raw_text)

        warning = None
        if not cleanup.looks_like_japanese(cleaned):
            warning = "Text doesn't look Japanese — check the capture region"
        elif cloud_full_frame:
            warning = ("Region crop unavailable — the FULL screen was sent "
                       "to the cloud OCR")

        # 4. deliver (clipboard copy happens frontend-side, in Steam's CEF —
        # gamescope propagates it to the other XWayland windows)
        await self.delivery.broadcast(cleaned)

        self._prune_captures()

        payload = {
            "text": cleaned,
            "raw": raw_text,
            "confidence": confidence,
            "clients": self.delivery.client_count,
            "copy_to_clipboard": bool(self.settings.get("copy_to_clipboard")),
            "warning": warning,
        }
        self._last_result = payload
        await self._emit("done", **payload)
        return {"ok": True, **payload}

    def _prune_captures(self):
        # full + crop per capture; never below one capture's worth
        keep = max(2, int(self.settings.get("screenshot_history")) * 2)
        try:
            files = sorted(
                (os.path.join(CAPTURES_DIR, f) for f in os.listdir(CAPTURES_DIR)),
                key=os.path.getmtime, reverse=True)
            for f in files[keep:]:
                os.remove(f)
        except OSError as e:
            logger.warning(f"capture pruning failed: {e}")

    # ---- visual region editor ----------------------------------------------
    # The editor works on the LATEST pipeline capture, never a fresh frame:
    # a fresh capture would photograph the editor modal itself (the PipeWire
    # source is the composited screen, UI included). Pipeline captures are
    # taken in-game with no UI up, so they're always clean.

    def _latest_capture_path(self):
        """Newest clean frame: pipeline captures or the editor's refresh."""
        try:
            files = [os.path.join(CAPTURES_DIR, f)
                     for f in os.listdir(CAPTURES_DIR)
                     if f.startswith("capture_")]
            if os.path.exists(PREVIEW_PNG):
                files.append(PREVIEW_PNG)
            return max(files, key=os.path.getmtime) if files else None
        except OSError:
            return None

    async def get_editor_frame(self):
        """Latest clean full-frame capture for the region editor."""
        path = self._latest_capture_path()
        if not path:
            return {"ok": False,
                    "error": "no capture yet — hold your capture button "
                             "in-game first"}
        return {"ok": True, "image": await _read_b64(path)}

    async def capture_editor_frame(self):
        """Fresh frame for the editor. Only call with all UI closed — the
        frontend choreographs: close modal + QAM, wait, capture, reopen."""
        if self._busy:
            return BUSY
        self._busy = True
        try:
            await self.capture.capture_png(PREVIEW_PNG)
            return {"ok": True, "image": await _read_b64(PREVIEW_PNG)}
        except CaptureError as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._busy = False

    async def detect_region(self):
        """Snap a region to the Japanese text in the latest capture."""
        if not (self.installer.is_installed() and self.downloader.is_installed()):
            return {"ok": False,
                    "error": "auto-detect needs the local OCR runtime + models"}
        path = self._latest_capture_path()
        if not path:
            return {"ok": False,
                    "error": "no capture yet — hold your capture button "
                             "in-game first"}
        if self._busy:
            return BUSY
        self._busy = True
        try:
            result = await self._local_ocr().recognize(
                path, region=None,
                min_confidence=float(self.settings.get("min_confidence")))
            if not result.image_size:
                return {"ok": False, "error": "OCR returned no image size"}
            width, height = result.image_size

            # prefer Japanese lines of some substance; fall back to any text
            candidates = [r for r in result.regions
                          if cleanup.looks_like_japanese(r["text"], 0.25)
                          and len(r["text"]) >= 3] or result.regions
            # drop button/hint bars (Auto | Skip | Log…) sitting near the
            # text box — they OCR as real Japanese, so the length filter
            # above doesn't catch them; only their layout does
            without_chrome = cleanup.drop_ui_chrome_rows(candidates)
            if without_chrome:
                candidates = without_chrome
            if not candidates:
                return {"ok": False, "error": "no text detected on screen"}

            left = min(r["rect"]["left"] for r in candidates) / width
            top = min(r["rect"]["top"] for r in candidates) / height
            right = max(r["rect"]["right"] for r in candidates) / width
            bottom = max(r["rect"]["bottom"] for r in candidates) / height
            pad = 0.02
            x = max(0.0, round(left - pad, 4))
            y = max(0.0, round(top - pad, 4))
            region = {
                "x": x,
                "y": y,
                "w": min(1.0 - x, round(right - left + 2 * pad, 4)),
                "h": min(1.0 - y, round(bottom - top + 2 * pad, 4)),
            }
            return {"ok": True, "region": region, "image": await _read_b64(path)}
        except OCRError as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._busy = False

    def _area_screenshot_path(self, area_id: str) -> str | None:
        if not AREA_SCREENSHOT_ID_RE.match(area_id or ""):
            return None
        return os.path.join(AREA_SCREENSHOTS_DIR, f"{area_id}.png")

    async def save_area_screenshot(self, image_b64: str, existing_id: str | None = None):
        """Persist the screenshot shown in the region editor for one capture
        area, so the QAM thumbnail can preview it later. Reuses existing_id's
        file when given (re-editing an area), otherwise mints a new id."""
        area_id = existing_id if existing_id and AREA_SCREENSHOT_ID_RE.match(existing_id) else uuid.uuid4().hex
        path = os.path.join(AREA_SCREENSHOTS_DIR, f"{area_id}.png")
        try:
            data = base64.b64decode(image_b64)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        def write():
            os.makedirs(AREA_SCREENSHOTS_DIR, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)

        try:
            await asyncio.to_thread(write)
            return {"ok": True, "id": area_id}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    async def get_area_screenshot(self, area_id: str):
        path = self._area_screenshot_path(area_id)
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "no screenshot saved"}
        return {"ok": True, "image": await _read_b64(path)}

    async def delete_area_screenshot(self, area_id: str):
        path = self._area_screenshot_path(area_id)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return {"ok": True}

    async def delete_all_area_screenshots(self):
        try:
            for f in os.listdir(AREA_SCREENSHOTS_DIR):
                try:
                    os.remove(os.path.join(AREA_SCREENSHOTS_DIR, f))
                except OSError:
                    pass
        except OSError:
            pass
        return {"ok": True}

    # ---- native lookup -----------------------------------------------------

    async def tokenize_line(self, text: str):
        """Split a sentence into word tokens with dictionary-form keys."""
        if not text:
            return {"ok": False, "error": "no text"}
        if not self.installer.is_lookup_installed():
            return {"ok": False, "error": "lookup runtime not installed"}
        if self._token_cache["text"] == text:
            return {"ok": True, "tokens": self._token_cache["tokens"]}
        try:
            tokens = await tokenize(self.installer.python, text)
        except OCRError as e:
            return {"ok": False, "error": str(e)}
        self._token_cache = {"text": text, "tokens": tokens}
        return {"ok": True, "tokens": tokens}

    async def lookup_word(self, queries):
        """Look a word up by candidate keys, best-first.

        `queries` come from a token: [dict_form, lemma, surface, reading].
        UniDic marks loanword lemmas like データ-data; strip the suffix.
        """
        cleaned = []
        for q in queries or []:
            key = q.split("-")[0] if q and not q.startswith("-") else q
            if key and key not in cleaned:
                cleaned.append(key)
        entries = await asyncio.to_thread(self.dictionary.lookup, cleaned)
        return {"ok": True, "entries": entries}

    async def get_lookup_status(self):
        return {
            "runtime_installed": self.installer.is_lookup_installed(),
            "runtime": self.installer.get_status(),
            "dictionary": self.dictionary.get_status(),
        }

    async def install_lookup_runtime(self):
        return {"started": self.installer.start_install_lookup()}

    async def import_dictionaries(self, download_jitendex: bool = False):
        return {"started": self.dictionary.start_import(download_jitendex)}

    # ---- Anki queue --------------------------------------------------------

    async def create_anki_card(self, expression: str, reading: str,
                               glosses: str, sentence: str, game: str = "",
                               word_type: str = "", surface: str = ""):
        """Into kuma3 on the Deck when it answers (a 🐻 Japanese note in
        Japanese Mined, then an AnkiWeb sync); else buffered for a .apkg."""
        if not self.settings.get("anki_enabled"):
            return {"ok": False, "error": "Anki integration is disabled in settings"}
        url = self.settings.get("kuma3_url")
        if self.settings.get("kuma3_enabled") and await kuma3.reachable(url):
            card = {"expression": expression, "reading": reading, "glosses": glosses,
                    "sentence": sentence, "game": game, "surface": surface}
            try:
                r = await kuma3.add_card(
                    url, card, deck=self.settings.get("kuma3_deck"),
                    note_type=self.settings.get("kuma3_note_type"),
                    image_path=await self._card_picture(),
                    audio_db=self.settings.get("kuma3_audio_db"))
            except (kuma3.Kuma3Error, OSError, ValueError) as e:
                return {"ok": False, "error": f"kuma3: {e}"}
            self._word_states_at = 0.0  # the new word's colour at the next look
            if not r["existing"]:
                self._kuma3_sync_task = asyncio.create_task(self._kuma3_sync(url))
            return {"ok": True, "id": f"kuma3:{r['note']}", "kuma3": True,
                    "existing": r["existing"], "deck": self.settings.get("kuma3_deck")}
        card = self.anki_buffer.add(expression, reading, glosses, sentence, game, word_type)
        return {"ok": True, "buffered": self.anki_buffer.count(), "id": card["id"]}

    def _latest_full_capture(self):
        """The newest in-game frame (never the editor's preview, which can show the UI)."""
        try:
            files = [os.path.join(CAPTURES_DIR, f) for f in os.listdir(CAPTURES_DIR)
                     if f.startswith("capture_")]
        except OSError:
            return None
        return max(files, key=os.path.getmtime) if files else None

    async def _card_picture(self):
        """The newest in-game frame as .webp for the card (the OCR runtime's Pillow), else the PNG itself."""
        png = self._latest_full_capture()
        if not png or not self.installer.is_installed():
            return png
        webp = os.path.join(RUNTIME_DIR, "card.webp")
        try:
            return await self._local_ocr().crop(png, None, webp)
        except OCRError as e:
            logger.warning(f"card picture stays PNG: {e}")
            return png

    async def _kuma3_sync(self, url: str):
        try:
            await kuma3.sync(url)
        except Exception as e:  # a missed sync is retried by the next card or kuma3 itself
            logger.warning(f"kuma3 sync failed: {e}")

    async def clear_anki_buffer(self):
        self.anki_buffer.clear()
        return {"ok": True}

    async def get_anki_buffer(self):
        return {"cards": self.anki_buffer.all()}

    async def remove_anki_buffer_card(self, card_id: str):
        if card_id.startswith("kuma3:"):  # undo a card just added to kuma3
            try:
                await kuma3.delete_note(self.settings.get("kuma3_url"), int(card_id[6:]))
            except (kuma3.Kuma3Error, OSError, ValueError) as e:
                return {"ok": False, "error": f"kuma3: {e}"}
            self._word_states_at = 0.0
            return {"ok": True, "buffered": self.anki_buffer.count()}
        self.anki_buffer.remove(card_id)
        return {"ok": True, "buffered": self.anki_buffer.count()}

    async def get_word_states(self):
        """kuma3's mined words for the line's colours ({word: "d"|"b"|"k"}), read again after a minute."""
        if not self.settings.get("kuma3_enabled"):
            return {"ok": False, "states": {}}
        if time.monotonic() - self._word_states_at > 60:
            try:
                states = await kuma3.word_states(self.settings.get("kuma3_url"), self.settings.get("kuma3_deck"))
            except (kuma3.Kuma3Error, OSError, ValueError):
                return {"ok": False, "states": {}}
            self._word_states, self._word_states_at = states, time.monotonic()
        return {"ok": True, "states": self._word_states}

    async def install_anki_export_runtime(self):
        return {"started": self.installer.start_install_anki()}

    async def export_anki_buffer(self):
        """Package the buffer into a .apkg and serve it over the LAN for a
        QR-code scan. Does not clear the buffer — that's a separate,
        explicit action."""
        if not self.anki_buffer.count():
            return {"ok": False, "error": "No cards in Anki queue to export"}
        if not self.installer.is_anki_installed():
            return {"ok": False, "error": "Anki export runtime not installed",
                    "needs_install": True}
        # blank field names are skipped (mirrored in AnkiBufferModal.tsx)
        field_map = {}
        for role, key in ANKI_FIELD_SETTINGS:
            name = (self.settings.get(key) or "").strip()
            if name:
                field_map[role] = name
        if not field_map:
            return {"ok": False, "error": "no Anki fields configured in settings"}

        out_path = os.path.join(RUNTIME_DIR, "anki_export.apkg")
        try:
            await build_apkg(self.installer.python, self.anki_buffer.all(),
                             self.settings.get("anki_deck"),
                             self.settings.get("anki_note_type"),
                             field_map, out_path)
        except AnkiExportError as e:
            return {"ok": False, "error": str(e)}
        try:
            url = await self.anki_server.start(out_path)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "url": url, "count": self.anki_buffer.count()}

    # ---- setup / status ----------------------------------------------------

    async def get_status(self):
        try:
            probe = await self.capture.probe()
        except Exception as e:
            probe = {"error": str(e)}
        return {
            "version": decky.DECKY_PLUGIN_VERSION,
            "monitor": self.monitor.get_status(),
            "runtime": self.installer.get_status(),
            "models": self.downloader.get_status(),
            "capture": probe,
            "delivery": {
                "port": self.delivery.port,
                "clients": self.delivery.client_count,
            },
            "anki_buffered": self.anki_buffer.count(),
            "last_result": self._last_result,
            "busy": self._busy,
        }

    async def install_runtime(self):
        return {"started": self.installer.start_install()}

    async def download_models(self):
        return {"started": self.downloader.start_download()}

    async def get_downloaded_data_size(self):
        paths = downloaded_data_paths(RUNTIME_DIR)
        return {"bytes": await asyncio.to_thread(disk_usage, paths)}

    async def delete_downloaded_data(self):
        """Advanced settings → Delete downloaded data: OCR/lookup/export
        runtime, OCR models, dictionary, captures. Settings, capture areas
        and the Anki queue are kept."""
        if (self._busy
                or self.installer.get_status()["installing"]
                or self.downloader.get_status()["downloading"]
                or self.dictionary.get_status()["importing"]):
            return {"ok": False,
                    "error": "Wait for the current download or capture to finish"}
        self._busy = True
        try:
            await self.anki_server.stop()
            paths = downloaded_data_paths(RUNTIME_DIR)
            freed = await asyncio.to_thread(disk_usage, paths)
            failures = await asyncio.to_thread(remove_paths, paths)
            if failures:
                logger.error(f"delete_downloaded_data: failed to remove {failures}")
            # back to a first-launch state: empty dictionary db + captures dir
            os.makedirs(CAPTURES_DIR, exist_ok=True)
            self.dictionary = await asyncio.to_thread(
                Dictionary,
                os.path.join(RUNTIME_DIR, "dictionary.sqlite3"),
                os.path.join(RUNTIME_DIR, "dicts"))
            self._token_cache = {"text": None, "tokens": []}
            logger.info(f"deleted downloaded data ({freed} bytes)")
            return {"ok": True, "freed_bytes": freed}
        finally:
            self._busy = False

    # ---- settings ----------------------------------------------------------

    async def get_all_settings(self):
        return self.settings.all()

    async def set_setting(self, key, value):
        self.settings.set(key, value)
        if key == "texthooker_port":
            await self.delivery.stop()
            self.delivery = DeliveryServer(port=int(value))
            try:
                await self.delivery.start()
            except Exception as e:
                logger.error(f"delivery restart failed: {e}")
        # let every frontend instance (trigger watcher) pick up the change
        await decky.emit("vnl_settings", self.settings.all())
        return {"ok": True}
