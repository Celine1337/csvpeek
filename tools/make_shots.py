#!/usr/bin/env python3
"""Regenerate docs/peek.png and docs/convert.png from real CLI output.

Run: python tools/make_shots.py   (requires Pillow)
Fixtures live in tests/demo; everything is offline and reproducible. The
shots use the exact code paths the CLI uses (render_profile / coerce_rows).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import csvpeek as cp
import render_terminal as rt


def render(name, ansi, cmd, title):
    rt.render(rt.parse_ansi(ansi), title, cmd, os.path.join(ROOT, "docs", name + ".png"))


def main():
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)

    people = os.path.join(ROOT, "tests", "demo", "people.csv")
    header, rows = cp.read_csv(people, ",")
    prof = cp.build_profile("tests/demo/people.csv", header, rows, ",", 5)
    render("peek", cp.render_profile(prof, True),
           "python csvpeek.py peek tests/demo/people.csv", "csvpeek — peek")

    tiny = os.path.join(ROOT, "tests", "demo", "tiny.csv")
    header, rows = cp.read_csv(tiny, ",")
    ansi = json.dumps(cp.coerce_rows(header, rows), indent=2, ensure_ascii=False)
    render("convert", ansi, "python csvpeek.py tojson tests/demo/tiny.csv",
           "csvpeek — tojson")


if __name__ == "__main__":
    main()