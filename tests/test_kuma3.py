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
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(req)
                p = req["params"]
                result = {"version": 6, "findNotes": fake.mined, "addNote": 1234,
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
    assert fake.calls[0]["params"]["query"] == '"deck:Japanese Mined" "Word:re:^行く(\\[|$)"'
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


def test_reachable_and_delete():
    fake = FakeAnkiConnect()
    assert asyncio.run(kuma3.reachable(fake.url))
    asyncio.run(kuma3.delete_note(fake.url, 1234))
    assert fake.calls[-1] == {"action": "deleteNotes", "version": 6, "params": {"notes": [1234]}}
    assert not asyncio.run(kuma3.reachable("http://127.0.0.1:9"))
