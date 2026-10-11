#!/usr/bin/env python3
"""Event traffic stats (received EVENTs and distinct sources) from the strfry-ratelimit audit trail.

usage: traffic.py <audit-dir> <www-dir> [state-dir]

Reads <audit-dir>/audit-YYYYMMDD.tsv[.gz] (UTC days, columns: arrival, source, id, pubkey, kind,
created_at, bytes, action, msg) and writes <www-dir>/traffic.json:
  {"generated_at", "timezone", "classes": {"ephemeral" | "regular" | "all": {"hourly", "daily", "monthly"}}}
  ephemeral = kinds 20000-29999, regular = every other kind, all = both. Each class has
  hourly : last 31 days, [hour_start_epoch, events, accepted, sources]
  daily  : every day (TIMEZONE of relay-stats.conf), [date, events, accepted, sources, hours_covered]
  monthly: every month, [YYYY-MM, events, accepted, sources, hours_covered]
hours_covered < 24 (or < the month's hours) means the audit trail did not cover the whole period
(the first day, or today / this month in progress).
"events" counts every EVENT of the class received (accepted or rejected); "sources" counts distinct
connecting addresses (IP / forwarder) that sent at least one in that hour / day / month.

Only files whose size/mtime changed since the last run are read again, so an hourly run reads just
the current audit file. Per-hour source sets are kept in the state dir as short hashes (never
published) for the current and previous month so that day / month distinct counts are exact; older
days and months are frozen into daily.json / monthly.json. Delete the state dir to rebuild.
"""
import datetime, glob, gzip, hashlib, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOUR, DAY = 3600, 86400
HOURLY_DAYS = 31
CLASSES = ("ephemeral", "regular", "all")


def load_tz():
    cfg = {"TIMEZONE": "Asia/Tokyo"}
    path = os.environ.get("RELAY_STATS_CONF", os.path.join(HERE, "relay-stats.conf"))
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg["TIMEZONE"])
    # same convention as analyze.py: the zone's current UTC offset, DST ignored
    return cfg["TIMEZONE"], int(datetime.datetime.now(tz).utcoffset().total_seconds())


TZ_LABEL, TZ_OFFSET = load_tz()


def day_name(ts):
    return (datetime.date(1970, 1, 1) + datetime.timedelta(days=(ts + TZ_OFFSET) // DAY)).isoformat()


def src_id(s):
    return hashlib.blake2b(s.encode(), digest_size=6).hexdigest()


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def scan_file(path, now):
    """-> {class: {hour: [events, accepted, set(src_ids)]}} for the hours this UTC-day file covers."""
    out = {c: {} for c in CLASSES}
    first = None
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split("\t", 8)
            if len(p) < 8:
                continue
            try:
                ts = float(p[0])
                kind = int(p[4])
            except ValueError:
                continue
            h = int(ts) // HOUR * HOUR
            if first is None or h < first:
                first = h
            sid = src_id(p[1])
            acc = p[7] == "accept"
            for c in ("ephemeral" if 20000 <= kind < 30000 else "regular", "all"):
                hours = out[c]
                r = hours.get(h)
                if r is None:
                    r = hours[h] = [0, 0, set()]
                r[0] += 1
                if acc:
                    r[1] += 1
                r[2].add(sid)
    if first is None:
        return out
    # hours with no traffic of a class are real zeros: from the first line of the file
    # (or the start of its UTC day) to the end of the day, but not into the future
    base = os.path.basename(path)[6:14]
    d0 = int(datetime.datetime.strptime(base, "%Y%m%d").replace(tzinfo=datetime.timezone.utc).timestamp())
    start = max(d0, first)
    for h in range(start, min(d0 + DAY, now // HOUR * HOUR + HOUR), HOUR):
        for hours in out.values():
            hours.setdefault(h, [0, 0, set()])
    return out


def month_of(date):
    return date[:7]


def main():
    audit, www = sys.argv[1], sys.argv[2]
    state = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "state", "traffic")
    os.makedirs(state, exist_ok=True)
    now = int(time.time())

    seen = read_json(os.path.join(state, "files.json"), {})              # day -> [size, mtime]
    st_ = {}
    for c in CLASSES:
        hours_raw = read_json(os.path.join(state, f"hours-{c}.json"), {})   # hour -> [e, a, [ids]]
        st_[c] = {
            "hours": {int(h): [v[0], v[1], set(v[2])] for h, v in hours_raw.items()},
            "daily": read_json(os.path.join(state, f"daily-{c}.json"), {}),       # date -> [e, a, s, n]
            "monthly": read_json(os.path.join(state, f"monthly-{c}.json"), {}),   # YYYY-MM -> [e, a, s, n]
        }

    files = {}
    for p in glob.glob(os.path.join(audit, "audit-*.tsv*")):
        b = os.path.basename(p)
        day = b[6:14]
        if not day.isdigit() or not (b.endswith(".tsv") or b.endswith(".tsv.gz")):
            continue
        if day in files and files[day].endswith(".tsv"):   # mid-compression: prefer the plain file
            continue
        files[day] = p
    for day in sorted(files):
        st = os.stat(files[day])
        sig = [st.st_size, int(st.st_mtime)]
        if seen.get(day) == sig:
            continue
        for c, hrs in scan_file(files[day], now).items():
            st_[c]["hours"].update(hrs)
        seen[day] = sig

    if not st_["all"]["hours"]:
        return
    this_month = month_of(day_name(now))
    # hour sets are kept for the previous and current month, so every day / month from the start of
    # the previous month is recomputed exactly from them
    keep_from = month_start_ts(this_month, -1)
    floor = month_of(day_name(keep_from))
    cut = (now // HOUR * HOUR) - HOURLY_DAYS * DAY
    out = {"generated_at": now, "timezone": TZ_LABEL, "classes": {}}
    for c in CLASSES:
        hours, daily, monthly = st_[c]["hours"], st_[c]["daily"], st_[c]["monthly"]
        by_day, by_month = {}, {}
        for h, (e, a, s) in hours.items():
            d = day_name(h)
            for key, tbl in ((d, by_day), (month_of(d), by_month)):
                r = tbl.setdefault(key, [0, 0, set(), 0])
                r[0] += e; r[1] += a; r[2] |= s; r[3] += 1
        # older hours only appear when old audit files are read for the first time; a day / month
        # before the kept range is written then and never overwritten by a partial re-read
        for d, (e, a, s, n) in by_day.items():
            if month_of(d) >= floor or d not in daily:
                daily[d] = [e, a, len(s), n]
        for m, (e, a, s, n) in by_month.items():
            if m >= floor or m not in monthly:
                monthly[m] = [e, a, len(s), n]
        hours = {h: v for h, v in hours.items() if h >= keep_from}
        write_json(os.path.join(state, f"hours-{c}.json"), {str(h): [v[0], v[1], sorted(v[2])] for h, v in hours.items()})
        write_json(os.path.join(state, f"daily-{c}.json"), daily)
        write_json(os.path.join(state, f"monthly-{c}.json"), monthly)
        out["classes"][c] = {
            "hourly": [[h, v[0], v[1], len(v[2])] for h, v in sorted(hours.items()) if h >= cut],
            "daily": [[d, *v] for d, v in sorted(daily.items())],
            "monthly": [[m, *v] for m, v in sorted(monthly.items())],
        }
    write_json(os.path.join(state, "files.json"), seen)
    write_json(os.path.join(www, "traffic.json"), out)


def month_start_ts(ym, delta=0):
    y, m = int(ym[:4]), int(ym[5:7]) + delta
    while m < 1:
        y, m = y - 1, m + 12
    return int(datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc).timestamp()) - TZ_OFFSET


if __name__ == "__main__":
    main()
