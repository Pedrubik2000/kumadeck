"""The words Pedro's phones show as known (kumapie), so the Deck colours them the same.

kumapie on the phone/tablet sends its known list to the PC (dojo-feed); kumadeck reads it back from
GET <server>/api/tv/known_words?person=&lang=ja once an hour when the PC answers, and keeps the last
copy on the Deck, so it works away from home too.
"""

import json
import os
import time
import urllib.parse
import urllib.request

REFRESH = 3600  # seconds between reads from the PC


class KnownWords:
    def __init__(self, settings_dir: str):
        self.path = os.path.join(settings_dir, "known-ja.json")
        self.words: frozenset[str] = frozenset()
        self.read_at = 0.0
        try:
            with open(self.path, encoding="utf-8") as f:
                self.words = frozenset(json.load(f))
            self.read_at = os.path.getmtime(self.path)
        except (OSError, ValueError):
            pass

    def refresh(self, server: str, person: str) -> None:
        """Blocking: run in a thread. Keeps the old copy when the PC doesn't answer."""
        if not server or time.time() - self.read_at < REFRESH:
            return
        q = urllib.parse.urlencode({"person": person, "lang": "ja"})
        try:
            with urllib.request.urlopen(f"{server.rstrip('/')}/api/tv/known_words?{q}", timeout=10) as r:
                words = json.loads(r.read())
        except (OSError, ValueError):
            return
        if not isinstance(words, list):
            return
        self.words, self.read_at = frozenset(w for w in words if isinstance(w, str)), time.time()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(self.words), f, ensure_ascii=False)
        os.replace(tmp, self.path)
