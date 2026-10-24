import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from vnlookup.known import KnownWords


def fake_pc(words):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps(words).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", seen


def test_reads_the_pc_then_keeps_a_copy_for_offline(tmp_path):
    url, seen = fake_pc(["行く", "すごい"])
    k = KnownWords(str(tmp_path))
    k.refresh(url, "pedro")
    assert k.words == {"行く", "すごい"}
    assert seen == ["/api/tv/known_words?person=pedro&lang=ja"]
    k.refresh(url, "pedro")  # within the hour: no second read
    assert len(seen) == 1
    again = KnownWords(str(tmp_path))  # next start, PC away
    again.read_at = 0
    again.refresh("http://127.0.0.1:9", "pedro")
    assert again.words == {"行く", "すごい"}


def test_no_server_no_words(tmp_path):
    k = KnownWords(str(tmp_path))
    k.refresh("", "pedro")
    assert k.words == frozenset()
