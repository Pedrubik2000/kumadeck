"""Cards straight into kuma3 desktop on the Deck (steamdeck_plan.md step 5).

kuma3 runs headless as a systemd --user service with AnkiConnect Extended on
127.0.0.1:8765. A looked-up word becomes a 🐻 Japanese note in "Japanese
Mined", filled the way kumapie fills it (Miner.kt): Sentence with the word in
<b>, Word 漢字[かな], the bilingual definition, the screen as Image, a person's
recording as Word Audio (the local ja-audio.sqlite), Context = the game. Then
kuma3 syncs to AnkiWeb, so the phone gets the card.
"""

import asyncio
import base64
import html
import json
import os
import re
import sqlite3
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"


class Kuma3Error(Exception):
    """kuma3 refused the note (missing deck/note type, full-sync prompt...)."""


def _call(url: str, action: str, timeout: float, **params):
    body = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.loads(r.read())
    if reply.get("error"):
        raise Kuma3Error(str(reply["error"]))
    return reply.get("result")


async def call(url: str, action: str, timeout: float = 10, **params):
    return await asyncio.to_thread(_call, url, action, timeout, **params)


async def reachable(url: str) -> bool:
    try:
        return bool(await call(url, "version", timeout=2))
    except (OSError, ValueError, Kuma3Error):
        return False


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def has_kanji(s: str) -> bool:
    return any(0x3400 <= ord(c) <= 0x9FFF for c in s)


def word_field(expression: str, reading: str) -> str:
    """届く[とどく]: the reading only when the word has kanji (kumapie's Word)."""
    e = html.escape(expression)
    return f"{e}[{html.escape(reading)}]" if reading and has_kanji(expression) else e


def bold_sentence(sentence: str, expression: str) -> str:
    """The sentence with the word's first appearance in <b> (the word as it stands in the line: 行こう)."""
    at = sentence.find(expression) if expression else -1
    if at < 0:
        return html.escape(sentence)
    end = at + len(expression)
    return html.escape(sentence[:at]) + "<b>" + html.escape(expression) + "</b>" + html.escape(sentence[end:])


def slug(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s.lower()).strip("_")


def recording(db_path: str, word: str, reading: str = "") -> bytes | None:
    """A person saying the word, as dojo-feed's ja_recording(): NHK16 > Shinmeikai 8 > JapanesePod101 > Forvo,
    by reading when the word has kanji."""
    if not db_path or not os.path.isfile(db_path):
        return None
    order = ("CASE w.source WHEN 'nhk16' THEN 0 WHEN 'shinmeikai8' THEN 1 WHEN 'jpod' THEN 2 ELSE 3 END")
    if reading and has_kanji(word):
        where, args = "w.term = ? AND (w.reading = ? OR w.reading = '')", (word, reading)
    else:
        where, args = "w.term = ?", (word,)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT a.data FROM words w JOIN audio a ON a.source = w.source AND a.file = w.file "
                         f"WHERE {where} ORDER BY {order} LIMIT 1", args).fetchone()
    return row[0] if row else None


def mined_query(deck: str, expression: str) -> str:
    """Anki search for a note of this word in the deck (Word is 'word' or 'word[reading]...')."""
    return f'"deck:{deck}" "Word:re:^{re.escape(expression)}(\\[|$)"'


async def add_card(url: str, card: dict, *, deck: str, note_type: str, image_path: str | None,
                   audio_db: str) -> dict:
    """Adds the word's note, or finds the one already mined. Returns {"note": id, "existing": bool}."""
    expression, reading = card["expression"], card.get("reading", "")
    found = await call(url, "findNotes", query=mined_query(deck, expression))
    if found:
        return {"note": found[0], "existing": True}
    stamp = int(time.time() * 1000)
    fields = {
        "Sentence": bold_sentence(card.get("sentence", ""), card.get("surface") or expression),
        "Word": word_field(expression, reading),
        "Definition (bilingual)": html.escape(card.get("glosses", "")).replace("\n", "<br>"),
        "Context": html.escape(f"Mined in kumadeck: {card['game']}." if card.get("game") else "Mined in kumadeck."),
    }
    if image_path and os.path.isfile(image_path):
        data = base64.b64encode(await asyncio.to_thread(_read, image_path)).decode()
        # ponytail: the capture's PNG as it is (~1 MB at 1280x800); webp when card size matters
        name = await call(url, "storeMediaFile", filename=f"kumadeck_{stamp}{os.path.splitext(image_path)[1]}",
                          data=data)
        fields["Image"] = f'<img src="{name}">'
    audio = await asyncio.to_thread(recording, audio_db, expression, reading)
    if audio:
        name = await call(url, "storeMediaFile", filename=f"kumadeck-ja-{slug(expression) or stamp}.opus",
                          data=base64.b64encode(audio).decode())
        fields["Word Audio"] = f"[audio:{name}]"
    tags = [t for t in (slug(card.get("game", "")), "kumadeck", "_ja::word") if t]
    note = await call(url, "addNote", note={"deckName": deck, "modelName": note_type, "fields": fields,
                                            "tags": tags, "options": {"allowDuplicate": True}})
    return {"note": note, "existing": False}


def spelling(field: str) -> str:
    """A Word field's word as written (kumapie's CardIndex.japaneseKeys): 日本[にほん] 語[ご]:0- -> 日本語."""
    s = html.unescape(re.sub(r"<[^>]*>", "", field))
    s = re.sub(r"[/*\\-]", "", re.sub(r":[0-9A-Za-z\-]*", "", s))
    return re.sub(r"\s", "", re.sub(r"([^\s\[\]]+)\[[^\]]*\]", r"\1", s))


async def word_states(url: str, deck: str) -> dict[str, str]:
    """Each mined word -> "d" a card due today, "b" mined and never reviewed, "k" studied and not due (kumapie's
    CardIndex.state; a word with no note here is red in the panel)."""
    # ponytail: "due" is Anki's is:due (the card's own due date), not kuma3's RWKV-Instant queue as on the phone
    q = f'"deck:{deck}"'
    notes = await call(url, "findNotes", query=q)
    if not notes:
        return {}
    info = await call(url, "notesInfo", notes=notes)
    due = set(await call(url, "findNotes", query=q + " is:due"))
    studied = set(await call(url, "findNotes", query=q + " -is:new"))
    rank = {"b": 0, "k": 1, "d": 2}  # any due card wins; blue only when every note of the word is new
    states: dict[str, str] = {}
    for n in info:
        w = spelling(n["fields"].get("Word", {}).get("value", ""))
        s = "d" if n["noteId"] in due else "k" if n["noteId"] in studied else "b"
        if w and rank[s] > rank.get(states.get(w, ""), -1):
            states[w] = s
    return states


async def delete_note(url: str, note: int) -> None:
    await call(url, "deleteNotes", notes=[note])


async def sync(url: str) -> None:
    """AnkiWeb sync after a card; a long one is fine to miss (kuma3 syncs again later)."""
    await call(url, "sync", timeout=120)
