#!/usr/bin/env python3
"""Backfill per-client daily history (www/clients/history/YYYY-MM-DD.json) from `relay-scan <db> <since> 0 clients`.

usage: client-backfill.py <clients.tsv> <out-dir> [banlist] [--profiles scan.tsv]

Same counting as analyze.write_clients: per day and client name (["client", name] tag; no tag = one row), distinct
pubkeys and events, all kinds; banned pubkeys and bot accounts (NIP-05 suffix, from the P lines of a window scan) are
excluded. Days already in <out-dir>/clients/history are left alone, so the hourly run's own days always win.
"""
import collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402


def main():
    args = sys.argv[1:]
    prof = None
    if "--profiles" in args:
        i = args.index("--profiles"); prof = args[i + 1]; del args[i:i + 2]
    tsv, outdir = args[:2]
    banned = set()
    if len(args) > 2 and os.path.exists(args[2]):
        banned = {l.strip() for l in open(args[2]) if len(l.strip()) == 64}
    bots = set()
    if prof:
        with open(prof, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("P\t"):
                    rec = line.rstrip("\n").split("\t")
                    try:
                        p = json.loads(json.loads(rec[2]))
                        n = p.get("nip05") if isinstance(p, dict) else None
                        if isinstance(n, str) and n.lower().endswith(analyze.BOT_NIP05_SUFFIXES):
                            bots.add(rec[1])
                    except (ValueError, IndexError):
                        pass
    skip = banned | bots
    today = analyze.day_of(int(__import__("time").time()))
    users = collections.defaultdict(lambda: collections.defaultdict(set))   # day -> name -> pubkeys
    events = collections.defaultdict(collections.Counter)                   # day -> name -> events
    names, ids = {}, {}   # client literal -> name; pubkey -> small int (keeps 38M rows in memory cheaply)
    n = 0
    with open(tsv, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("C\t"):
                continue
            rec = line.rstrip("\n").split("\t")
            if len(rec) < 5:
                continue
            pk, day, lit = rec[1], int(rec[2]), rec[4]
            if pk in skip or day >= today:
                continue
            name = names.get(lit)
            if name is None:
                name = names[lit] = analyze.client_name(lit) or analyze.NO_CLIENT
            pid = ids.get(pk)
            if pid is None:
                pid = ids[pk] = len(ids)
            users[day][name].add(pid)
            events[day][name] += 1
            n += 1
    hdir = os.path.join(outdir, "clients", "history")
    os.makedirs(hdir, exist_ok=True)
    wrote = kept = 0
    for d in sorted(users):
        path = os.path.join(hdir, f"{analyze.day_name(d)}.json")
        if os.path.exists(path):
            kept += 1
            continue
        day = users[d]
        snap = {nm: [len(pks), events[d][nm]] for nm, pks in sorted(day.items(), key=lambda x: -len(x[1]))}
        total = [len(set().union(*day.values())), sum(events[d].values())]
        with open(path + ".tmp", "w") as f:
            json.dump({"date": analyze.day_name(d), "partial": False, "total": total, "clients": snap, "backfill": True},
                      f, ensure_ascii=False, separators=(",", ":"))
        os.replace(path + ".tmp", path)
        wrote += 1
    print(f"client backfill: {n} events, {len(users)} days, wrote {wrote}, kept {kept} existing, "
          f"excluded banned={len(banned)} bots={len(bots)}")


if __name__ == "__main__":
    main()
