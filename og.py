#!/usr/bin/env python3
"""Render www/assets/og.png (1200x630) from og-template.html + stats via headless Chrome.
usage: og.py <outdir>   (outdir = www; reads stats.json + history/)"""
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def main(outdir):
    outdir = os.path.abspath(outdir)
    stats = json.load(open(os.path.join(outdir, "stats.json")))
    hist = []
    hdir = os.path.join(outdir, "history")
    for name in sorted(n for n in os.listdir(hdir) if n.endswith(".json"))[-120:]:
        try:
            h = json.load(open(os.path.join(hdir, name)))
        except ValueError:
            continue
        if h.get("partial"):
            continue
        hist.append({"date": name[:10], "active": {l: [h["active"][l][w]["total"] for w in ("dau", "wau", "mau")] for l in ("_all", "ja") if l in h["active"]}})
    tpl = open(os.path.join(HERE, "og-template.html"), encoding="utf-8").read()
    html = tpl.replace("__RELAY_STATS_DATA__", json.dumps({"stats": stats, "history": hist}, ensure_ascii=False).replace("<", "\\u003c"))
    tmp_html = os.path.join(outdir, ".og.html")
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(html)
    os.makedirs(os.path.join(outdir, "assets"), exist_ok=True)
    tmp_png = os.path.join(outdir, "assets", ".og-tmp.png")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", 
                    "--force-device-scale-factor=1", "--window-size=1200,630",
                    f"--screenshot={tmp_png}", "file://" + tmp_html],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    os.replace(tmp_png, os.path.join(outdir, "assets", "og.png"))
    os.remove(tmp_html)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "www"))
