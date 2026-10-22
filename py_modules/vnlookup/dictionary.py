"""Local dictionary: imports Yomitan-format dictionary zips into SQLite.

Yomitan dictionaries (Jitendex, JMnedict, pitch-accent and frequency dicts)
are zips of JSON "banks":
  index.json                    {title, revision, format, ...}
  term_bank_N.json              [[expression, reading, defTags, rules,
                                  score, glosses, sequence, termTags], ...]
  term_meta_bank_N.json         [[expression, mode, data], ...]
                                mode "freq" or "pitch"

Glosses may be Yomitan "structured content" (nested HTML-ish JSON); we
flatten to plain text for the panel and keep it readable with bullet/line
separators. Everything here is stdlib — it runs in the Decky process.
"""

import json
import logging
import os
import sqlite3
import threading
import urllib.request
import zipfile

from .net import ssl_context

logger = logging.getLogger(__name__)

# official distribution point per jitendex.org/pages/downloads.html
JITENDEX_URL = ("https://github.com/stephenmk/stephenmk.github.io/releases/"
                "latest/download/jitendex-yomitan.zip")

_BLOCK_TAGS = {"div", "li", "ul", "ol", "br", "tr", "details", "summary"}

# Bump whenever already-imported data needs reprocessing to pick up a
# change in how term banks are flattened. On startup, a database with
# PRAGMA user_version below this is silently re-imported from its source
# zips (see Dictionary.__init__).
#
# 2: word_type split out of glosses into its own column.
# 3: no nested "sense" bullets, Tatoeba example sentences dropped.
_SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS dictionaries (
    id INTEGER PRIMARY KEY,
    title TEXT UNIQUE,
    revision TEXT,
    kind TEXT
);
CREATE TABLE IF NOT EXISTS terms (
    dict_id INTEGER,
    expression TEXT,
    reading TEXT,
    glosses TEXT,
    word_type TEXT,
    tags TEXT,
    score INTEGER
);
CREATE INDEX IF NOT EXISTS idx_terms_expr ON terms(expression);
CREATE INDEX IF NOT EXISTS idx_terms_reading ON terms(reading);
CREATE TABLE IF NOT EXISTS term_meta (
    dict_id INTEGER,
    expression TEXT,
    mode TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_meta_expr ON term_meta(expression);
"""


def flatten_content(node, pos_out: list[str] | None = None) -> str:
    """Flatten Yomitan structured content to plain text.

    When `pos_out` is given, part-of-speech-info spans (Jitendex's "noun",
    "transitive verb", etc.) are collected into it, in order, and excluded
    from the returned text — rather than left inline as a bare text line
    for something downstream to guess is a header and not a real gloss.
    This must happen here, while the tree still carries that semantic
    marker (data-content == "part-of-speech-info"); by the time glosses
    are flattened to plain text there is no reliable way to tell "a POS
    header" apart from "a real gloss that happens to sit on its own line"
    (e.g. right at the boundary between two dictionary rows joined by
    _entries). Omit pos_out (the default) to keep the old inline behavior.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_content(x, pos_out) for x in node)
    if isinstance(node, dict):
        if "text" in node and "content" not in node:
            return str(node.get("text") or "")
        tag = node.get("tag", "")
        if tag in ("rt", "rp"):
            return ""  # furigana annotations read as duplicated kana inline
        semantic = (node.get("data") or {}).get("content", "")
        if semantic == "attribution":
            # per-gloss JMdict/Tatoeba credit links; dropped here since every
            # entry already carries the dict title, and full acknowledgement
            # lives in the plugin's "About / sources" settings section
            return ""
        if semantic == "example-sentence":
            # a Tatoeba usage example nested inside the sense — several
            # sentences' worth per entry once every sense has one; too
            # verbose for a compact lookup/card view, so drop the whole
            # subtree (Japanese + translation + footnote) rather than
            # just the attribution part of it
            return ""
        if semantic == "part-of-speech-info" and pos_out is not None:
            label = flatten_content(node.get("content")).strip()
            if label:
                pos_out.append(label)
            return ""
        inner = flatten_content(node.get("content"), pos_out)
        if tag == "li" and semantic not in ("sense", "sense-group"):
            # a real gloss line. "sense"/"sense-group" <li>s (falling through
            # to the plain _BLOCK_TAGS handling below, no bullet) are just
            # numbering/grouping wrappers around one or more of these —
            # nested <li>s at every level would otherwise each add their
            # own bullet to the same line ("• • • to eat")
            inner = "• " + inner.strip() + "\n"
        elif tag in ("ul", "ol"):
            # glossary lists must not start on the part-of-speech line
            inner = "\n" + inner
            inner = inner if inner.endswith("\n") else inner + "\n"
        elif tag in _BLOCK_TAGS:
            inner = inner if inner.endswith("\n") else inner + "\n"
        elif tag == "span" and inner and not inner.endswith((" ", "\n")):
            inner += " "  # part-of-speech header spans run together otherwise
        return inner
    return str(node)


def flatten_glosses(glosses, pos_out: list[str] | None = None) -> str:
    parts = []
    for g in glosses or []:
        if isinstance(g, str):
            parts.append(g)
        elif isinstance(g, dict):
            gtype = g.get("type")
            if gtype == "text":
                parts.append(g.get("text", ""))
            elif gtype == "structured-content":
                parts.append(flatten_content(g.get("content"), pos_out))
            elif gtype == "image":
                continue
            else:
                parts.append(flatten_content(g, pos_out))
        else:
            parts.append(flatten_content(g, pos_out))
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    # collapse blank lines and per-line leftover whitespace
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _term_row(dict_id: int, r: list) -> tuple:
    """One term_bank_N.json row -> a terms table row, splitting the
    part-of-speech header out of the glosses text (see flatten_content's
    pos_out) into its own word_type column."""
    pos: list[str] = []
    glosses = flatten_glosses(r[5], pos)
    tags = " ".join(filter(None, [r[2], r[7] if len(r) > 7 else ""]))
    return (dict_id, r[0], r[1] or "", glosses, ", ".join(pos),
            tags, int(r[4]) if r[4] else 0)


_MAX_GLOSS_SENSES = 3


def _cap_glosses(text: str, max_senses: int = _MAX_GLOSS_SENSES) -> str:
    """Cap a flattened glosses string to its first few senses.

    A common JMdict entry runs to 15+ senses; joined across every matching
    dictionary row (see _entries), that's the entire dictionary entry
    pasted into the word-lookup panel and onto every Anki card. Each
    non-blank line is one sense — POS-group headers ("noun", "transitive
    verb") are extracted separately at flatten time (flatten_content's
    pos_out) rather than left inline in this text, so unlike an earlier
    version of this function there's no ambiguity here to resolve: every
    line reaching this point already is a real gloss. Silently drops
    anything past the cap — no "…" marker on the card.
    """
    lines = text.splitlines()
    return "\n".join(lines[:max_senses])


def _freq_value(data):
    """Normalize the many shapes of frequency data to (sort_key, display)."""
    if isinstance(data, (int, float)):
        return data, str(int(data))
    if isinstance(data, str):
        return None, data
    if isinstance(data, dict):
        if "frequency" in data:  # {reading, frequency: ...}
            return _freq_value(data["frequency"])
        value = data.get("value")
        display = data.get("displayValue") or (str(value) if value is not None else "")
        return value, display
    return None, ""


class Dictionary:
    def __init__(self, db_path: str, dicts_dir: str):
        self.db_path = db_path
        self.dicts_dir = dicts_dir
        self._importing = False
        self._progress = 0.0
        self._step = ""
        self._error = None
        self._lock = threading.Lock()
        self._cached_dicts = []
        self._cached_terms = 0
        # written to PRAGMA user_version only once a migration reimport has
        # *completed* (see _import_all): if the plugin reloads mid-import,
        # SQLite rolls the import back and the next launch must retry
        self._pending_schema_version: int | None = None
        os.makedirs(dicts_dir, exist_ok=True)
        needs_reimport = False
        with self._connect() as db:
            # WAL lets the panel's status polls read while an import is
            # mid-transaction; sticky once set on the file
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            # terms tables created before word_type existed (inserts
            # address columns by name, so appending it is fine)
            cols = {row[1] for row in db.execute("PRAGMA table_info(terms)")}
            if "word_type" not in cols:
                db.execute("ALTER TABLE terms ADD COLUMN word_type TEXT")
            if db.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                # a brand-new database has nothing to reprocess
                has_data = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM dictionaries)").fetchone()[0]
                needs_reimport = bool(has_data)
        if needs_reimport:
            # source zips are kept in dicts_dir after import, so this needs
            # no network
            self._pending_schema_version = _SCHEMA_VERSION
            self.start_import()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    # ---- status ----------------------------------------------------------

    def get_status(self) -> dict:
        try:
            with self._connect() as db:
                dicts = [r[0] for r in
                         db.execute("SELECT title FROM dictionaries ORDER BY id")]
                terms = db.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            self._cached_dicts, self._cached_terms = dicts, terms
        except sqlite3.OperationalError:
            # import mid-transaction; serve the last known snapshot
            dicts, terms = self._cached_dicts, self._cached_terms
        pending = [f for f in sorted(os.listdir(self.dicts_dir))
                   if f.endswith(".zip")]
        with self._lock:
            return {
                "dictionaries": dicts,
                "term_count": terms,
                "pending_zips": pending,
                "importing": self._importing,
                "progress": self._progress,
                "step": self._step,
                "error": self._error,
                "ready": terms > 0,
            }

    # ---- import ----------------------------------------------------------

    def start_import(self, download_jitendex: bool = False) -> bool:
        with self._lock:
            if self._importing:
                return False
            self._importing = True
            self._error = None
            self._progress = 0.0
            self._step = "starting"
        threading.Thread(target=self._import_all,
                         args=(download_jitendex,), daemon=True).start()
        return True

    def _set(self, step=None, progress=None):
        with self._lock:
            if step is not None:
                self._step = step
            if progress is not None:
                self._progress = progress

    def _import_all(self, download_jitendex: bool):
        try:
            if download_jitendex:
                dest = os.path.join(self.dicts_dir, "jitendex-yomitan.zip")
                if not os.path.exists(dest):
                    self._download(JITENDEX_URL, dest)
            zips = [os.path.join(self.dicts_dir, f)
                    for f in sorted(os.listdir(self.dicts_dir))
                    if f.endswith(".zip")]
            if not zips:
                raise RuntimeError(
                    f"no dictionary zips found in {self.dicts_dir}")
            for i, path in enumerate(zips):
                self._import_zip(path, base=i / len(zips),
                                 span=1 / len(zips))
            self._set(step="done", progress=1.0)
            if self._pending_schema_version is not None:
                with self._connect() as db:
                    db.execute(f"PRAGMA user_version = {self._pending_schema_version}")
                self._pending_schema_version = None
        except Exception as e:
            logger.error(f"dictionary import failed: {e}")
            with self._lock:
                self._error = str(e)
        finally:
            with self._lock:
                self._importing = False

    def _download(self, url: str, dest: str):
        self._set(step="downloading Jitendex")
        req = urllib.request.Request(url, headers={"User-Agent": "kumadeck"})
        tmp = dest + ".part"
        with urllib.request.urlopen(req, timeout=120,
                                    context=ssl_context()) as resp, \
                open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    # download counts as the first 60% of overall progress
                    self._set(progress=0.6 * done / total)
        os.replace(tmp, dest)

    def _import_zip(self, path: str, base: float, span: float):
        name = os.path.basename(path)
        self._set(step=f"importing {name}")
        db = self._connect()
        try:
            with zipfile.ZipFile(path) as zf:
                try:
                    index = json.loads(zf.read("index.json"))
                except KeyError:
                    raise RuntimeError(f"{name}: not a Yomitan dictionary "
                                       "(no index.json)") from None
                title = index.get("title") or name
                revision = index.get("revision", "")

                existing = db.execute(
                    "SELECT id FROM dictionaries WHERE title = ?",
                    (title,)).fetchone()
                if existing:
                    logger.info(f"re-importing {title}: clearing old rows")
                    db.execute("DELETE FROM terms WHERE dict_id = ?", existing)
                    db.execute("DELETE FROM term_meta WHERE dict_id = ?", existing)
                    db.execute("DELETE FROM dictionaries WHERE id = ?", existing)
                cur = db.execute(
                    "INSERT INTO dictionaries (title, revision, kind) "
                    "VALUES (?, ?, ?)", (title, revision, "term"))
                dict_id = cur.lastrowid

                banks = [n for n in zf.namelist()
                         if n.startswith(("term_bank_", "term_meta_bank_"))
                         and n.endswith(".json")]
                for j, bank_name in enumerate(sorted(banks)):
                    rows = json.loads(zf.read(bank_name))
                    if bank_name.startswith("term_meta_bank_"):
                        db.executemany(
                            "INSERT INTO term_meta VALUES (?, ?, ?, ?)",
                            ((dict_id, r[0], r[1],
                              json.dumps(r[2], ensure_ascii=False))
                             for r in rows))
                    else:
                        db.executemany(
                            "INSERT INTO terms "
                            "(dict_id, expression, reading, glosses, word_type, "
                            " tags, score) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_term_row(dict_id, r) for r in rows))
                    # bound the write transaction so status reads stay fresh
                    db.commit()
                    self._set(progress=base + span * (0.6 + 0.4 * (j + 1) / len(banks)))
                db.commit()
                logger.info(f"imported {title} ({revision})")
        finally:
            db.close()

    # ---- lookup ----------------------------------------------------------

    def lookup(self, queries, limit: int = 24) -> list:
        """Try each query string in order; return entries for the first hit.

        Entry: {expression, reading, glosses, word_type, tags, dict, score,
                frequency, pitch}
        """
        db = self._connect()
        try:
            for q in queries:
                if not q:
                    continue
                rows = db.execute(
                    "SELECT t.expression, t.reading, t.glosses, t.word_type, "
                    "       t.tags, t.score, d.title "
                    "FROM terms t JOIN dictionaries d ON d.id = t.dict_id "
                    "WHERE t.expression = ? OR "
                    "      (t.reading = ? AND t.reading != '') "
                    "ORDER BY t.score DESC LIMIT ?",
                    (q, q, limit)).fetchall()
                if rows:
                    return self._entries(db, rows, matched=q)
            return []
        finally:
            db.close()

    def _entries(self, db, rows, matched: str) -> list:
        # group by (expression, reading); merge glosses across dictionaries
        grouped = {}
        order = []
        for expr, reading, glosses, word_type, tags, score, dtitle in rows:
            key = (expr, reading)
            if key not in grouped:
                grouped[key] = {
                    "expression": expr,
                    "reading": reading,
                    "matched": matched,
                    "glosses": [],
                    "word_types": [],
                    "tags": tags.strip(),
                    "dicts": [],
                    "score": score,
                }
                order.append(key)
            grouped[key]["glosses"].append(glosses)
            if word_type:
                for wt in word_type.split(", "):
                    if wt not in grouped[key]["word_types"]:
                        grouped[key]["word_types"].append(wt)
            if dtitle not in grouped[key]["dicts"]:
                grouped[key]["dicts"].append(dtitle)

        entries = [grouped[k] for k in order]
        for e in entries:
            e["glosses"] = _cap_glosses("\n".join(e["glosses"]))
            e["word_type"] = ", ".join(e.pop("word_types"))
            e["frequency"] = self._frequency(db, e["expression"])
            e["pitch"] = self._pitch(db, e["expression"], e["reading"])
        return entries

    def _frequency(self, db, expression: str):
        rows = db.execute(
            "SELECT data FROM term_meta WHERE expression = ? AND mode = 'freq'",
            (expression,)).fetchall()
        best = None
        display = ""
        for (data,) in rows:
            value, disp = _freq_value(json.loads(data))
            if value is not None and (best is None or value < best):
                best, display = value, disp or str(value)
        return display or None

    def _pitch(self, db, expression: str, reading: str):
        rows = db.execute(
            "SELECT data FROM term_meta WHERE expression = ? AND mode = 'pitch'",
            (expression,)).fetchall()
        for (data,) in rows:
            d = json.loads(data)
            if not reading or d.get("reading") == reading:
                positions = [p.get("position") for p in d.get("pitches", [])
                             if isinstance(p, dict) and "position" in p]
                if positions:
                    return positions
        return None
