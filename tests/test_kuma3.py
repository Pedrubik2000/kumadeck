import asyncio
import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from vnlookup import kuma3


class FakeAnkiConnect:
    """Records every AnkiConnect call; findNotes answers from `mined`."""

    def __init__(self):
        self.calls = []
        self.mined = []
        self.queries = {}  # findNotes query -> note ids (else `mined`)
        self.notes = []  # notesInfo
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(req)
                p = req["params"]
                if req["action"] == "findNotes" and fake.queries:
                    result = fake.queries[p["query"]]
                else:
                    result = {"version": 6, "findNotes": fake.mined, "addNote": 1234, "notesInfo": fake.notes,
                              "storeMediaFile": p.get("filename"), "deleteNotes": None}[req["action"]]
                body = json.dumps({"result": result, "error": None}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def actions(self):
        return [c["action"] for c in self.calls]


def audio_db(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE words (term, reading, source, file)")
        db.execute("CREATE TABLE audio (source, file, data)")
        db.executemany("INSERT INTO words VALUES (?,?,?,?)", [
            ("行く", "いく", "forvo", "a"), ("行く", "いく", "nhk16", "b"), ("行く", "ゆく", "nhk16", "c")])
        db.executemany("INSERT INTO audio VALUES (?,?,?)", [
            ("forvo", "a", b"forvo"), ("nhk16", "b", b"nhk-iku"), ("nhk16", "c", b"nhk-yuku")])
    return str(path)


CARD = {"expression": "行く", "reading": "いく", "glosses": "to go\nto move", "sentence": "東京に行く。",
        "game": "Yakuza 0"}


def test_add_card_fields_media_tags(tmp_path):
    fake = FakeAnkiConnect()
    png = tmp_path / "capture_1.png"
    png.write_bytes(b"\x89PNG")
    r = asyncio.run(kuma3.add_card(fake.url, CARD, deck="Japanese Mined", note_type="🐻 Japanese",
                                   image_path=str(png), audio_db=audio_db(tmp_path / "a.sqlite")))
    assert r == {"note": 1234, "existing": False}
    assert fake.actions() == ["findNotes", "storeMediaFile", "storeMediaFile", "addNote"]
    assert fake.calls[0]["params"]["query"] == '"deck:Japanese Mined" "Word:re:^行く(\\[|:|$)"'
    audio = fake.calls[2]["params"]
    assert base64.b64decode(audio["data"]) == b"nhk-iku"  # NHK first, by reading
    note = fake.calls[3]["params"]["note"]
    f = note["fields"]
    assert f["Sentence"] == "東京に<b>行く</b>。"
    assert f["Word"] == "行く[いく]"
    assert f["Definition (bilingual)"] == "to go<br>to move"
    assert f["Image"].startswith('<img src="kumadeck_') and f["Image"].endswith('.png">')
    assert f["Word Audio"] == f"[audio:{audio['filename']}]"
    assert f["Context"] == "Mined in kumadeck: Yakuza 0."
    assert note["tags"] == ["yakuza_0", "kumadeck", "_ja::word"]
    assert (note["deckName"], note["modelName"]) == ("Japanese Mined", "🐻 Japanese")


def test_already_mined_adds_nothing(tmp_path):
    fake = FakeAnkiConnect()
    fake.mined = [99]
    r = asyncio.run(kuma3.add_card(fake.url, CARD, deck="Japanese Mined", note_type="🐻 Japanese",
                                   image_path=None, audio_db=""))
    assert r == {"note": 99, "existing": True}
    assert fake.actions() == ["findNotes"]


def test_kana_word_no_reading_no_bold_when_conjugated():
    assert kuma3.word_field("すごい", "すごい") == "すごい"
    assert kuma3.bold_sentence("行こう<よ>", "行く") == "行こう&lt;よ&gt;"
    assert kuma3.bold_sentence("行こう<よ>", "行こう") == "<b>行こう</b>&lt;よ&gt;"


def test_spelling_as_kumapie_reads_word_fields():
    assert kuma3.spelling("行く[いく]:0-") == "行く"
    assert kuma3.spelling("日本[にほん] 語[ご]:0-") == "日本語"
    assert kuma3.spelling(r"拾[ひろ]\う:0") == "拾う"
    assert kuma3.spelling("<b>すごい</b>") == "すごい"


def test_word_states_due_new_studied():
    fake = FakeAnkiConnect()
    deck = '"deck:Japanese Mined"'
    fake.queries = {deck: [1, 2, 3, 4], deck + " is:due": [1], deck + " -is:new": [1, 3, 4]}
    word = lambda nid, w: {"noteId": nid, "fields": {"Word": {"value": w, "order": 2}}}  # noqa: E731
    fake.notes = [word(1, "行く[いく]:0-"), word(2, "爆炎[ばくえん]"), word(3, "すごい"), word(4, "爆炎")]
    states = asyncio.run(kuma3.word_states(fake.url, "Japanese Mined"))
    # 爆炎: one note never reviewed, one studied -> studied (blue only when every note is new)
    assert states == {"行く": "d", "爆炎": "k", "すごい": "k"}
    fake.queries = {deck: [2], deck + " is:due": [], deck + " -is:new": []}
    fake.notes = [word(2, "爆炎[ばくえん]")]
    assert asyncio.run(kuma3.word_states(fake.url, "Japanese Mined")) == {"爆炎": "b"}


def test_sync_trouble_in_words():
    assert "full sync" in kuma3.sync_trouble("Sync status 2 not one of [0, 1] - see SyncCollectionResponse")
    assert kuma3.sync_trouble("sync: auth not configured") == "kuma3 isn't logged in to AnkiWeb"


def test_reachable_and_delete():
    fake = FakeAnkiConnect()
    assert asyncio.run(kuma3.reachable(fake.url))
    asyncio.run(kuma3.delete_note(fake.url, 1234))
    assert fake.calls[-1] == {"action": "deleteNotes", "version": 6, "params": {"notes": [1234]}}
    assert not asyncio.run(kuma3.reachable("http://127.0.0.1:9"))
