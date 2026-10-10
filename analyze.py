#!/usr/bin/env python3
"""Turn relay-scan TSV into language-split DAU/WAU/MAU stats (JSON) and render the landing page.

usage: analyze.py <relay-name> <scan.tsv> <out-dir> [banlist]
       analyze.py --render <out-dir>      # re-render index.html from stats.json after editing template.html
"""
import collections, datetime, json, os, re, sys, time

import fasttext

HERE = os.path.dirname(os.path.abspath(__file__))
fasttext.FastText.eprint = lambda *a, **k: None
MODEL = fasttext.load_model(os.path.join(HERE, "lib", "lid.176.ftz"))

DAY = 86400
WINDOWS = {"dau": 1, "wau": 7, "mau": 30}   # calendar days (JST) ending at the target date
def _load_config():
    """relay-stats.conf (KEY=VALUE). Keys: TIMEZONE (IANA name, e.g. Asia/Tokyo, UTC, America/New_York)."""
    cfg = {"TIMEZONE": "Asia/Tokyo"}
    path = os.environ.get("RELAY_STATS_CONF", os.path.join(HERE, "relay-stats.conf"))
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


CONFIG = _load_config()
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(CONFIG["TIMEZONE"])
except Exception as e:  # python < 3.9 or unknown name: fail loudly instead of silently using another zone
    raise SystemExit(f"relay-stats: invalid TIMEZONE={CONFIG['TIMEZONE']!r} in relay-stats.conf ({e!r})")
# Day boundaries use the zone's current UTC offset (DST shifts are ignored for simplicity).
# Day number d = (ts + TZ_OFFSET) // DAY, i.e. the date is 1970-01-01 + d days (same as relay-scan).
TZ_OFFSET = int(datetime.datetime.now(TZ).utcoffset().total_seconds())
TZ_LABEL = CONFIG["TIMEZONE"]


def day_of(ts):
    return (ts + TZ_OFFSET) // DAY


EPOCH_DATE = datetime.date(1970, 1, 1)


def day_name(d):
    return (EPOCH_DATE + datetime.timedelta(days=d)).isoformat()


def day_from_name(name):
    return (datetime.date.fromisoformat(name) - EPOCH_DATE).days
# Kinds that do not indicate a human being active on this relay.
NOT_ACTIVITY = {1059, 13, 10377}
BOT_NIP05_SUFFIXES = ("nostrverse.eth.link",)
# Fediverse bridges (Mostr / momostr): counted separately as "_bridge", not in languages/_all
BRIDGE_NIP05_SUFFIXES = ("mostr.pub", "momostr.pink")
BRIDGE_KEY = "_bridge"
NOISE = re.compile(r"https?://\S+|nostr:\S+|(?:npub|note|nevent|naddr|nprofile)1\w+|#\[\d+\]|:[A-Za-z0-9_+-]+:|#\S+")
TOP_LANGS = 12


def detect(text):
    t = NOISE.sub(" ", text).replace("\n", " ").strip()
    t = t.encode("utf-8", "replace").decode("utf-8")   # drop lone surrogates
    if len(t) < 4:
        return None
    try:
        (label,), (p,) = MODEL.predict(t)
    except (TypeError, ValueError):
        return None
    return label[9:] if p >= 0.5 else None


def is_ephemeral(kind):
    return 20000 <= kind < 30000


def main():
    relay, scan, outdir = sys.argv[1:4]
    banned = set()
    if len(sys.argv) > 4 and os.path.exists(sys.argv[4]):
        banned = {l.strip() for l in open(sys.argv[4]) if len(l.strip()) == 64}
    now = int(time.time())

    summary, actdays, votes, target = {}, collections.defaultdict(set), collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    profiles = {}
    bridge_marked = set()
    kinds30 = collections.Counter()
    latest_note = {}   # pk -> (created_at, content JSON literal) of the newest kind 1 in the scan window
    with open(scan, encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = line.rstrip("\n").split("\t")
            tag = rec[0]
            if tag == "E":
                _, pk, ca, kind, tgt, content = rec
                ca, kind = int(ca), int(kind)
                if pk in banned or ca > now + 600:
                    continue
                kinds30[kind] += 1
                if not is_ephemeral(kind) and kind not in NOT_ACTIVITY:
                    actdays[pk].add(day_of(ca))
                if kind == 1 and content != "-" and ca > latest_note.get(pk, (0,))[0]:
                    latest_note[pk] = (ca, content)
                if content != "-":
                    try:
                        lang = detect(json.loads(content))
                    except ValueError:
                        lang = None
                    if lang:
                        votes[pk][lang] += 1
                if tgt != "-":
                    target[pk][tgt] += 1
            elif tag == "P":
                try:
                    profiles[rec[1]] = json.loads(json.loads(rec[2]))
                except (ValueError, IndexError):
                    profiles[rec[1]] = {}
            elif tag == "B":
                bridge_marked.add(rec[1])
            elif tag == "S":
                summary[rec[1]] = int(rec[2])

    def nip05(pk):
        p = profiles.get(pk)
        v = p.get("nip05") if isinstance(p, dict) else None
        return v if isinstance(v, str) and v.strip() else None

    bots = {pk for pk in profiles if (nip05(pk) or "").lower().endswith(BOT_NIP05_SUFFIXES)}
    for pk in bots:
        actdays.pop(pk, None)
    bridges = bridge_marked | {pk for pk in profiles if (nip05(pk) or "").lower().endswith(BRIDGE_NIP05_SUFFIXES)}
    last = actdays
    cache_path = os.path.join(outdir, "lang-cache.json")   # written by backfill.py (whole-history evidence)
    lang_cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    # 1) language from own text; 2) profile name/about; 3) majority language of people reacted to
    lang = {pk: v.most_common(1)[0][0] for pk, v in votes.items() if v}
    for pk in last:
        if pk in lang:
            continue
        p = profiles.get(pk)
        if isinstance(p, dict):
            text = " ".join(str(p.get(x) or "") for x in ("about", "display_name", "name"))
            l = detect(text)
            if l:
                lang[pk] = l
    inferred, from_cache = {}, set()
    for pk in last:
        if pk in lang or pk not in target:
            continue
        c = collections.Counter()
        for t, n in target[pk].items():
            if t in lang:
                c[lang[t]] += n
        if c:
            inferred[pk] = c.most_common(1)[0][0]
    for pk in last:
        if pk not in lang and pk not in inferred and pk in lang_cache:
            inferred[pk] = lang_cache[pk]
            from_cache.add(pk)

    def klass(pk):
        if pk not in profiles:
            return "no_profile"
        return "nip05" if nip05(pk) else "profile_only"

    today = day_of(now)

    def count(target_day):
        rows = collections.defaultdict(lambda: {w: {"total": 0, "nip05": 0, "profile_only": 0, "no_profile": 0} for w in WINDOWS})
        for pk, ds in actdays.items():
            l = lang.get(pk) or inferred.get(pk) or "unknown"
            c = klass(pk)
            keys = (BRIDGE_KEY,) if pk in bridges else (l, "_all")
            for w, n in WINDOWS.items():
                if any(target_day - n < d <= target_day for d in ds):
                    for key in keys:
                        rows[key][w]["total"] += 1
                        rows[key][w][c] += 1
        return {l: r for l, r in sorted(rows.items(), key=lambda x: -x[1]["mau"]["total"])}

    base = {
        "relay": relay,
        "generated_at": now,
        "generated_at_iso": datetime.datetime.fromtimestamp(now, TZ).isoformat(),
        "timezone": TZ_LABEL,
        "db": {
            "total_events": summary.get("total_events"),
            "bytes_used": summary.get("db_bytes_used"),
            "events_30d": summary.get("window_events"),
        },
        "excluded": {"bot_nip05_accounts": len(bots), "banned_pubkeys": len(banned),
                     "bridge_accounts": len(bridges & set(actdays))},
        "language_method": {
            "from_own_text": sum(1 for pk in last if pk in votes and votes[pk]),
            "from_profile": sum(1 for pk in last if pk in lang and not votes.get(pk)),
            "inferred_from_reactions": len(inferred),
        },
        "kinds_30d": dict(kinds30.most_common(15)),
    }
    os.makedirs(os.path.join(outdir, "history"), exist_ok=True)

    def write(path, obj):
        with open(path + ".tmp", "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    # yesterday = latest complete day (headline); today = in progress
    for d, partial in ((today - 1, False), (today, True)):
        snap = dict(base, date=day_name(d), partial=partial, active=count(d))
        write(os.path.join(outdir, "history", f"{day_name(d)}.json"), snap)
        if not partial:
            out = snap
    out["today"] = {"date": day_name(today), "active": json.load(open(os.path.join(outdir, "history", f"{day_name(today)}.json")))["active"]}
    write(os.path.join(outdir, "stats.json"), out)
    try:
        write_accounts(outdir, today, actdays, lang, inferred, from_cache, votes, profiles, latest_note, bridges, klass, nip05)
    except Exception as e:   # the account list is a debugging aid; never block the stats update
        print(f"accounts list failed: {e!r}", file=sys.stderr)
    render(out, outdir)


def write_accounts(outdir, today, actdays, lang, inferred, from_cache, votes, profiles, latest_note, bridges, klass, nip05):
    """assets/accounts-<lang>.json: every account active in the last 30 days with its profile, newest note and
    the evidence behind its language, so the language split can be checked by eye (landing page #accounts)."""
    adir = os.path.join(outdir, "assets")
    os.makedirs(adir, exist_ok=True)

    def cut(v, n):
        v = v if isinstance(v, str) else ""
        return v if len(v) <= n else v[:n] + "…"

    groups = collections.defaultdict(list)
    for pk, ds in actdays.items():
        recent = [d for d in ds if today - 30 < d <= today]
        if not recent:
            continue
        if pk in votes and votes[pk]:
            how = "note"
        elif pk in lang:
            how = "profile"
        elif pk in inferred:
            how = "history" if pk in from_cache else "reactions"
        else:
            how = "none"
        l = BRIDGE_KEY if pk in bridges else (lang.get(pk) or inferred.get(pk) or "unknown")
        p = profiles.get(pk) if isinstance(profiles.get(pk), dict) else {}
        note = latest_note.get(pk)
        text = None
        if note:
            try:
                text = json.loads(note[1])
            except ValueError:
                text = None
        groups[l].append({
            "pk": pk, "how": how, "votes": dict(votes[pk].most_common(4)) if pk in votes else {},
            "last": day_name(max(recent)), "days": len(recent), "cls": klass(pk),
            "ago": sorted(today - d for d in recent),   # active days, as days before the list date (0 = today)
            "name": cut(p.get("display_name") or p.get("name"), 60), "nip05": cut(nip05(pk), 80),
            "about": cut(p.get("about"), 240), "pic": cut(p.get("picture"), 300) if str(p.get("picture") or "").startswith("https://") else "",
            "note": cut(text, 300) if text else "", "note_at": note[0] if note else 0,
        })
    index, daily = {}, {}
    for l, rows in groups.items():
        rows.sort(key=lambda r: (r["last"], r["note_at"]), reverse=True)
        tmp = os.path.join(adir, f"accounts-{l}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"lang": l, "date": day_name(today), "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(adir, f"accounts-{l}.json"))
        index[l] = len(rows)
        per_day = [0] * 30
        for r in rows:
            for a in r["ago"]:
                per_day[a] += 1
        daily[l] = per_day
    for n in os.listdir(adir):   # languages that dropped out
        if n.startswith("accounts-") and n.endswith(".json") and n[9:-5] not in index:
            os.remove(os.path.join(adir, n))
    tmp = os.path.join(adir, "accounts.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": day_name(today), "langs": index, "daily": daily}, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(adir, "accounts.json"))


def render(s, outdir):
    """Inject stats + history into template.html (editable without touching code)."""
    hist = []
    hdir = os.path.join(outdir, "history")
    for name in sorted(n for n in os.listdir(hdir) if n.endswith(".json")):
        try:
            h = json.load(open(os.path.join(hdir, name)))
            if h.get("partial"):
                continue
            # compact: [dau, wau, mau] for the languages that matter on the chart
            hist.append({"date": name[:10], "active": {l: [r[w]["total"] for w in WINDOWS] for l, r in h["active"].items()
                                                       if l in ("_all", "ja", "en", "unknown") or r["mau"]["total"] >= 50}})
        except Exception:
            pass
    tpl = open(os.environ.get("RELAY_STATS_TEMPLATE", os.path.join(HERE, "template.html")), encoding="utf-8").read()
    eph = None
    try:
        eph = json.load(open(os.path.join(outdir, "ephemeral.json")))   # written by ephemeral.py (relays with an audit trail)
    except (OSError, ValueError):
        pass
    data = json.dumps({"stats": s, "history": hist, "ephemeral": eph}, ensure_ascii=False).replace("<", "\\u003c")
    html = (tpl.replace("__RELAY_STATS_DATA__", data).replace("__RELAY_NAME__", s["relay"])
               .replace("__OG_VERSION__", str(s.get("date", "")).replace("-", "")))   # changes daily -> SNS caches refresh
    tmp = os.path.join(outdir, "index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, os.path.join(outdir, "index.html"))


def rerender(outdir):
    render(json.load(open(os.path.join(outdir, "stats.json"))), outdir)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render":
        rerender(sys.argv[2])
    else:
        main()
