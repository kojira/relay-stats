#!/usr/bin/env python3
"""Full-text search index (SQLite FTS5, trigram) of kind 1 notes, kind 30023 articles and kind 0 profiles.

usage: search-index.py <db-path> [--full]
       (reads events from `$SEARCH_STRFRY scan <filter>`, e.g.
        SEARCH_STRFRY="/path/strfry --config /path/strfry.conf";
        for nostrfy: SEARCH_NOSTRFY="/path/nostrfy-scan /path/nostrfy-data", which reads the LMDB directly)

--full   index every stored event (first build; tens of minutes on a large relay)
default  incremental: events with created_at since the last run minus 2 days, plus kind 5 deletions

Tables
  notes(rowid = created_at << 20 | low bits of the id, so FTS rowid order is time order and a time range is a
        rowid range;  id BLOB, pubkey BLOB, kind, created_at, d, nsfw, content)  + notes_fts (contentless trigram)
  profiles(rowid, pubkey BLOB, created_at, name, display_name, nip05, picture, about) + profiles_fts
  meta(k, v)
  short_terms(term, built_at, hits) + short_hits(term, rowid)
        index for 1-2 character words, which trigram FTS cannot match. Terms come from short-terms.txt
        (always) and from state/search/queries.log (words the web server could not answer; built once
        searched >= SHORT_MIN_SEARCHES times in SHORT_WINDOW). New notes are added to every built term.
Kind 30023 keeps only the newest version per (pubkey, d). Banned pubkeys are filtered by the reader
(web/server.mjs), so a ban / unban takes effect without re-indexing. NSFW = content-warning tag,
"nsfw" t / l tag, or #nsfw in the text; flagged rows stay in the table but are never shown in search.
"""
import json, os, re, shlex, sqlite3, subprocess, sys, time

KINDS_TEXT = (1, 30023)
SLACK = 2 * 86400
MAX_CONTENT = 100_000
SHORT_MIN_SEARCHES = 3            # searches within SHORT_WINDOW before a short word gets its own index
SHORT_WINDOW = 7 * 86400
SHORT_NEW_PER_RUN = 5             # at most this many new short words per run (each is one pass over the notes)
SHORT_MAX_TERMS = 300
SHORT_MAX_HITS = 1_000_000        # a word in more notes than this is not indexed (too common to be useful)
HERE = os.path.dirname(os.path.abspath(__file__))
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")   # same folding as SQL LIKE
SHORT_TERMS = []                  # built short words (lower-cased), filled by main()
NSFW_RE = re.compile(r"(?:^|[\s#＃])#nsfw\b|＃nsfw\b", re.I)


def is_nsfw(e):
    for t in e.get("tags", []):
        if not t:
            continue
        if t[0] == "content-warning":
            return 1
        if t[0] in ("t", "l") and len(t) > 1 and str(t[1]).lower() == "nsfw":
            return 1
    return 1 if NSFW_RE.search(e.get("content", "")) else 0


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS notes(rowid INTEGER PRIMARY KEY, id BLOB UNIQUE NOT NULL, pubkey BLOB NOT NULL,
        kind INTEGER NOT NULL, created_at INTEGER NOT NULL, d TEXT, nsfw INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS notes_created ON notes(created_at);
    CREATE INDEX IF NOT EXISTS notes_author ON notes(pubkey, created_at);
    CREATE INDEX IF NOT EXISTS notes_kind ON notes(kind) WHERE kind = 30023;
    CREATE UNIQUE INDEX IF NOT EXISTS notes_addr ON notes(pubkey, d) WHERE kind = 30023;
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content, content='', contentless_delete=1, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS profiles(rowid INTEGER PRIMARY KEY, pubkey BLOB UNIQUE NOT NULL, created_at INTEGER NOT NULL,
        name TEXT, display_name TEXT, nip05 TEXT, picture TEXT, about TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS profiles_fts USING fts5(names, content='', contentless_delete=1, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS short_terms(term TEXT PRIMARY KEY, built_at INTEGER NOT NULL, hits INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS short_hits(term TEXT NOT NULL, rowid_ INTEGER NOT NULL, PRIMARY KEY(term, rowid_)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS short_hits_row ON short_hits(rowid_);
    """)
    return db


def scan(flt):
    if os.environ.get("SEARCH_NOSTRFY"):           # "<nostrfy-scan> <dbdir>": nostrfy LMDB, event JSON lines
        cmd = shlex.split(os.environ["SEARCH_NOSTRFY"]) + [str(flt.get("since", 0)), "0", "json",
                                                            ",".join(map(str, flt["kinds"]))]
    else:
        cmd = shlex.split(os.environ["SEARCH_STRFRY"]) + ["scan", json.dumps(flt)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        try:
            yield json.loads(line)
        except ValueError:
            continue
    p.wait()
    if p.returncode:
        raise SystemExit(f"search-index: scan failed ({p.returncode})")


def put_note(db, e):
    eid, pk = bytes.fromhex(e["id"]), bytes.fromhex(e["pubkey"])
    content = e.get("content", "")[:MAX_CONTENT]
    d = None
    if e["kind"] == 30023:
        d = next((t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "d"), "")
        old = db.execute("SELECT rowid, created_at FROM notes WHERE kind=30023 AND pubkey=? AND d=?", (pk, d)).fetchone()
        if old:
            if old[1] >= e["created_at"]:
                return 0
            delete_rowid(db, old[0])
        title = next((t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "title"), "")
        if title:
            content = title + "\n" + content
    if db.execute("SELECT 1 FROM notes WHERE id=?", (eid,)).fetchone():
        return 0
    rowid = time_rowid(e["created_at"], eid)
    while db.execute("SELECT 1 FROM notes WHERE rowid=?", (rowid,)).fetchone():
        rowid += 1
    db.execute("INSERT INTO notes(rowid, id, pubkey, kind, created_at, d, nsfw, content) VALUES(?,?,?,?,?,?,?,?)",
               (rowid, eid, pk, e["kind"], e["created_at"], d, is_nsfw(e), content))
    db.execute("INSERT INTO notes_fts(rowid, content) VALUES(?,?)", (rowid, content))
    if SHORT_TERMS:
        low = content.translate(ASCII_LOWER)
        for t in SHORT_TERMS:
            if t in low:
                db.execute("INSERT OR IGNORE INTO short_hits(term, rowid_) VALUES(?,?)", (t, rowid))
    return 1


def time_rowid(created_at, eid):
    ca = max(0, min(int(created_at), (1 << 42) - 1))      # clamp absurd timestamps
    return (ca << 20) | (int.from_bytes(eid[:3], "big") & 0xFFFF0)


def delete_rowid(db, rowid):
    db.execute("DELETE FROM short_hits WHERE rowid_=?", (rowid,))
    db.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
    db.execute("DELETE FROM notes WHERE rowid=?", (rowid,))


def put_profile(db, e):
    try:
        p = json.loads(e.get("content") or "{}")
        if not isinstance(p, dict):
            return 0
    except ValueError:
        return 0
    s = lambda k, n=300: (p.get(k) if isinstance(p.get(k), str) else "")[:n]
    pk = bytes.fromhex(e["pubkey"])
    old = db.execute("SELECT rowid, created_at FROM profiles WHERE pubkey=?", (pk,)).fetchone()
    if old and old[1] >= e["created_at"]:
        return 0
    row = (e["created_at"], s("name", 100), s("display_name", 100) or s("displayName", 100), s("nip05", 200), s("picture", 1000), s("about", 2000))
    names = " ".join(x for x in row[1:4] if x)
    if old:
        db.execute("UPDATE profiles SET created_at=?, name=?, display_name=?, nip05=?, picture=?, about=? WHERE rowid=?", row + (old[0],))
        db.execute("DELETE FROM profiles_fts WHERE rowid=?", (old[0],))
        db.execute("INSERT INTO profiles_fts(rowid, names) VALUES(?,?)", (old[0], names))
    else:
        cur = db.execute("INSERT INTO profiles(pubkey, created_at, name, display_name, nip05, picture, about) VALUES(?,?,?,?,?,?,?)", (pk,) + row)
        db.execute("INSERT INTO profiles_fts(rowid, names) VALUES(?,?)", (cur.lastrowid, names))
    return 1


def apply_deletion(db, e):
    pk = bytes.fromhex(e["pubkey"])
    n = 0
    for t in e.get("tags", []):
        if len(t) < 2:
            continue
        if t[0] == "e" and re.fullmatch(r"[0-9a-f]{64}", t[1]):
            r = db.execute("SELECT rowid FROM notes WHERE id=? AND pubkey=?", (bytes.fromhex(t[1]), pk)).fetchone()
        elif t[0] == "a" and t[1].startswith("30023:" + e["pubkey"] + ":"):
            r = db.execute("SELECT rowid FROM notes WHERE kind=30023 AND pubkey=? AND d=? AND created_at<=?",
                           (pk, t[1].split(":", 2)[2], e["created_at"])).fetchone()
        else:
            continue
        if r:
            delete_rowid(db, r[0]); n += 1
    return n


def short_wanted(db):
    """short words to build: short-terms.txt + words searched often enough (queries.log), not built yet"""
    want = []
    try:
        with open(os.path.join(HERE, "short-terms.txt"), encoding="utf-8") as f:
            want += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        pass
    counts, cut = {}, time.time() - SHORT_WINDOW
    try:
        with open(os.path.join(HERE, "state", "search", "queries.log"), encoding="utf-8") as f:
            for line in f:                       # "<unix time>\t<word>"
                ts, _, w = line.rstrip("\n").partition("\t")
                if w and ts.isdigit() and int(ts) >= cut:
                    counts[w] = counts.get(w, 0) + 1
    except OSError:
        pass
    want += [w for w, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= SHORT_MIN_SEARCHES]
    built = {r[0] for r in db.execute("SELECT term FROM short_terms")}
    out = []
    for w in want:
        w = w.translate(ASCII_LOWER)
        if len(w) == 1 and w.isascii():          # single ASCII letters / digits / symbols: in nearly every note
            continue
        if 1 <= len(w) <= 2 and not w.isspace() and w not in built and w not in out:
            out.append(w)
    return out[:max(0, min(SHORT_NEW_PER_RUN, SHORT_MAX_TERMS - len(built)))]


def build_short(db, term):
    t0 = time.time()
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    db.execute("BEGIN")
    db.execute("INSERT OR IGNORE INTO short_hits(term, rowid_) SELECT ?, rowid FROM notes WHERE content LIKE ? ESCAPE '\\'", (term, like))
    n = db.execute("SELECT count(*) FROM short_hits WHERE term=?", (term,)).fetchone()[0]
    if n > SHORT_MAX_HITS:                     # too common: drop the list, remember the word so it is not retried
        db.execute("DELETE FROM short_hits WHERE term=?", (term,))
        n = -n
    db.execute("INSERT OR REPLACE INTO short_terms(term, built_at, hits) VALUES(?,?,?)", (term, int(time.time()), n))
    db.execute("COMMIT")
    print(f"{time.strftime('%F %T')} search-index: short word {term!r}: {n} notes ({time.time() - t0:.0f}s)", flush=True)


def main():
    path = sys.argv[1]
    full = "--full" in sys.argv[2:]
    db = open_db(path)
    started = int(time.time())
    last = db.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    if not full and not last:
        raise SystemExit("search-index: no index yet; run with --full first")
    since = 0 if full else int(last[0]) - SLACK
    flt = lambda kinds: {"kinds": list(kinds), **({"since": since} if since else {})}
    stats = {"notes": 0, "profiles": 0, "deleted": 0}
    SHORT_TERMS[:] = [r[0] for r in db.execute("SELECT term FROM short_terms WHERE hits >= 0")]
    t0 = time.time()
    for kinds, fn, key in ((KINDS_TEXT, put_note, "notes"), ((0,), put_profile, "profiles")):
        db.execute("BEGIN")
        n = 0
        for e in scan(flt(kinds)):
            try:
                stats[key] += fn(db, e)
            except (KeyError, ValueError, TypeError):
                continue
            n += 1
            if n % 20000 == 0:
                db.execute("COMMIT"); db.execute("BEGIN")
                if full:
                    print(f"{time.strftime('%F %T')} {key} {n} read, {stats[key]} added", flush=True)
        db.execute("COMMIT")
    if not full:
        db.execute("BEGIN")
        for e in scan({"kinds": [5], "since": since}):
            try:
                stats["deleted"] += apply_deletion(db, e)
            except (KeyError, ValueError):
                continue
        db.execute("COMMIT")
    db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('last_run', ?)", (str(started),))
    for term in short_wanted(db):                 # after the notes are in, so a new word covers everything
        build_short(db, term)
    if full:
        db.execute("INSERT INTO notes_fts(notes_fts) VALUES('optimize')")
        db.execute("INSERT INTO profiles_fts(profiles_fts) VALUES('optimize')")
    print(f"{time.strftime('%F %T')} search-index {'full' if full else 'incremental'}: +{stats['notes']} notes, "
          f"+{stats['profiles']} profiles, -{stats['deleted']} deleted ({time.time() - t0:.0f}s)", flush=True)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # the file is complete on its own (it may be renamed into place)
    db.close()


if __name__ == "__main__":
    main()
