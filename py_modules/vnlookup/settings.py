"""Plugin settings, persisted as JSON in Decky's settings dir."""

import copy
import json
import os
from typing import Any

DEFAULTS: dict[str, Any] = {
    # List of {"region": {x,y,w,h}, "button": "L4"|"R4"|"L5"|"R5"|None}.
    # Each area is a screen region (fraction of screen size) plus the back
    # button that triggers a capture of it. The default covers the usual
    # bottom-third VN text box.
    "capture_areas": [
        {"region": {"x": 0.03, "y": 0.62, "w": 0.94, "h": 0.36}, "button": "L5"},
    ],
    # Per-game overrides, keyed by Steam appid (string):
    # {"<appid>": {"display_name": str, "areas": [<capture_areas entry>, ...]}}.
    # A game with no entry here uses capture_areas ("Default") until the
    # user edits its areas, which creates an entry automatically.
    "capture_profiles": {},
    # "rapidocr" (local, default) | "gemini" (cloud, needs api key). No UI
    # for the Gemini options at the moment.
    "ocr_backend": "rapidocr",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "min_confidence": 0.4,
    # delivery — clipboard copy is done by the frontend in Steam's CEF;
    # gamescope (SteamOS >= 3.7.14) syncs it to Firefox/Yomitan
    "texthooker_port": 8766,
    "copy_to_clipboard": True,
    # how many past captures to keep on disk (the region editor reuses the
    # newest one as its background frame)
    "screenshot_history": 20,
    # master switch — off by default; Anki is an opt-in feature
    "anki_enabled": False,
    # cards are buffered locally and exported as a batch .apkg (scanned via
    # QR code). Field names map onto the plugin-owned note type embedded in
    # the .apkg; empty field names are skipped. Only anki_deck has UI
    # (Panel.tsx); the rest are fixed.
    "anki_deck": "Steam Deck Vocabulary",
    "anki_note_type": "VN Lookup",
    "anki_expression_field": "Front",
    "anki_reading_field": "Reading",
    "anki_glossary_field": "Back",
    "anki_sentence_field": "Sentence",
    # the dictionary's part-of-speech tags for the looked-up word (e.g.
    # "v1", "adj-i", "n") — written to the back of the card only
    "anki_word_type_field": "Word Type",
    # the running game's display name at capture time, written to the back
    # of the card only (see ROLE_ORDER in anki_export_worker.py)
    "anki_game_field": "Game",
    # kuma3 desktop on the Deck (AnkiConnect Extended, kuma3-headless.service):
    # cards go straight in as 🐻 Japanese notes when it answers, else into the
    # .apkg buffer above
    "kuma3_enabled": True,
    "kuma3_url": "http://127.0.0.1:8765",
    "kuma3_deck": "Japanese Mined",
    "kuma3_note_type": "🐻 Japanese",
    "kuma3_audio_db": "/run/media/deck/GF8S5/kumadeck/ja-audio.sqlite",
}

# settings keys retired by past versions — dropped from stored JSON on load
# so they don't linger forever in get_all_settings()'s output
_DEPRECATED_KEYS = (
    "ankiconnect_url", "anki_auto_enrich", "anki_picture_field", "anki_image",
    "auto_open_qam", "button_map", "capture_mode", "region", "region_alt",
    "strip_speaker_name", "trigger_button", "trigger_hold_ms",
)


def write_json_atomic(path: str, data: Any) -> None:
    """Write JSON via a temp file + rename, so a crash never truncates it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Settings:
    def __init__(self, settings_dir: str):
        self.path = os.path.join(settings_dir, "vn-lookup.json")
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = copy.deepcopy(DEFAULTS)
            return
        # merge so new defaults appear after plugin updates
        merged = {**copy.deepcopy(DEFAULTS), **stored}
        for key in _DEPRECATED_KEYS:
            merged.pop(key, None)
        self._data = merged

    def save(self) -> None:
        write_json_atomic(self.path, self._data)

    def get(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def all(self) -> dict[str, Any]:
        return dict(self._data)
