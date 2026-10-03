#!/usr/bin/env python3
"""Rebuild daily history (www/history/YYYY-MM-DD.json) from a full relay-scan ("full" mode) TSV.

usage: backfill.py <relay-name> <full-scan.tsv> <out-dir> [banlist] [--from YYYY-MM-DD]

Each pubkey gets one language for its whole history (from all its sampled text, profile, or the
languages of people it reacted to). For each day D the counts are:
  DAU = distinct pubkeys active on D, WAU = active in (D-6..D), MAU = active in (D-29..D).
Days are calendar days in TIMEZONE from relay-stats.conf (passed to relay-scan by run/backfill scripts).
Profile class (nip05 / profile_only / no_profile) uses the CURRENT kind 0 (history of kind 0 is not used).
"""
import collections, datetime, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402

WIN = {"dau": 1, "wau": 7, "mau": 30}


def main():
    args = [a for a in sys.argv[1:]]
    start = None
    if "--from" in args:
        i = args.index("--from"); start = args[i + 1]; del args[i:i + 2]
    relay, scan, outdir = args[:3]
    banned = set()
    if len(args) > 3 and os.path.exists(args[3]):
        banned = {l.strip() for l in open(args[3]) if len(l.strip()) == 64}

    days = collections.defaultdict(set)          # day -> pubkeys
    votes = collections.defaultdict(collections.Counter)
    target = collections.defaultdict(collections.Counter)
    profiles, summary = {}, {}
    bridge_marked = set()
    with open(scan, encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = line.rstrip("\n").split("\t")
            t = rec[0]
            if t == "D":
                if rec[1] not in banned:
                    days[int(rec[2])].add(rec[1])
            elif t == "E":
                _, pk, ca, kind, tgt, content = rec
                if pk in banned:
                    continue
                if content != "-":
                    try:
                        l = analyze.detect(json.loads(content))
                    except ValueError:
                        l = None
                    if l:
                        votes[pk][l] += 1
                if tgt != "-":
                    target[pk][tgt] += 1
            elif t == "P":
                try:
                    profiles[rec[1]] = json.loads(json.loads(rec[2]))
                except (ValueError, IndexError):
                    profiles[rec[1]] = {}
            elif t == "B":
                bridge_marked.add(rec[1])
            elif t == "S":
                summary[rec[1]] = int(rec[2])

    def nip05(pk):
        p = profiles.get(pk)
        v = p.get("nip05") if isinstance(p, dict) else None
        return v if isinstance(v, str) and v.strip() else None

    bots = {pk for pk in profiles if (nip05(pk) or "").lower().endswith(analyze.BOT_NIP05_SUFFIXES)}
    everyone = set().union(*days.values()) - bots
    bridges = bridge_marked | {pk for pk in profiles if (nip05(pk) or "").lower().endswith(analyze.BRIDGE_NIP05_SUFFIXES)}
    lang = {pk: v.most_common(1)[0][0] for pk, v in votes.items() if v}
    for pk in everyone:
        if pk not in lang and isinstance(profiles.get(pk), dict):
            p = profiles[pk]
            l = analyze.detect(" ".join(str(p.get(x) or "") for x in ("about", "display_name", "name")))
            if l:
                lang[pk] = l
    for pk in everyone:
        if pk in lang or pk not in target:
            continue
        c = collections.Counter()
        for t, n in target[pk].items():
            if t in lang:
                c[lang[t]] += n
        if c:
            lang[pk] = c.most_common(1)[0][0]

    def klass(pk):
        if pk not in profiles:
            return "no_profile"
        return "nip05" if nip05(pk) else "profile_only"

    info = {pk: ((analyze.BRIDGE_KEY,) if pk in bridges else (lang.get(pk, "unknown"), "_all"), klass(pk)) for pk in everyone}
    hdir = os.path.join(outdir, "history")
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(outdir, "lang-cache.json.tmp"), "w") as f:
        json.dump({pk: l for pk, l in lang.items() if pk in everyone}, f)
    os.replace(os.path.join(outdir, "lang-cache.json.tmp"), os.path.join(outdir, "lang-cache.json"))
    first, last = min(days), max(days)
    if start:
        first = max(first, analyze.day_of(int(datetime.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=analyze.TZ).timestamp())))
    today = analyze.day_of(int(datetime.datetime.now().timestamp()))
    written = 0
    window = collections.deque()          # last 30 days of sets
    for d in range(first - 29, last + 1):
        window.append(days.get(d, set()) - bots)
        if len(window) > 30:
            window.popleft()
        if d < first or d >= today:       # today is partial; the live job writes it
            continue
        rows = collections.defaultdict(lambda: {w: {"total": 0, "nip05": 0, "profile_only": 0, "no_profile": 0} for w in WIN})
        for w, n in WIN.items():
            act = set().union(*list(window)[-n:])
            for pk in act:
                keys, c = info.get(pk, (("unknown", "_all"), "no_profile"))
                for key in keys:
                    rows[key][w]["total"] += 1
                    rows[key][w][c] += 1
        out = {
            "relay": relay, "date": analyze.day_name(d), "partial": False, "timezone": analyze.TZ_LABEL,
            "backfilled": True,
            "active": {l: r for l, r in sorted(rows.items(), key=lambda x: -x[1]["mau"]["total"])},
        }
        name = analyze.day_name(d) + ".json"
        path = os.path.join(hdir, name)
        if os.path.exists(path):
            try:
                if not json.load(open(path)).get("backfilled"):
                    continue          # keep live snapshots
            except ValueError:
                pass
        with open(path + ".tmp", "w") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
        written += 1
    print(f"backfilled {written} days ({analyze.day_name(first)} .. {analyze.day_name(last)}), "
          f"pubkeys={len(everyone)} bots_excluded={len(bots)} bridges={len(bridges & everyone)} langs_known={sum(1 for pk in everyone if pk in lang)}")


if __name__ == "__main__":
    main()
