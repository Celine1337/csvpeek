#!/usr/bin/env python3
"""csvpeek - quick CSV profiling and conversion.

`peek` profiles a CSV (row/column counts, heuristic per-column types, nulls,
numeric stats, top values, sample rows); `tojson` converts CSV to a JSON array
of row objects with coerced types; `tocsv` converts a JSON array of objects
back to CSV. Single file, standard library only, no network.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

__version__ = "1.0.0"

# Values treated as empty/null (case-insensitive, after stripping whitespace).
NULL_TOKENS = {"", "null", "none", "na", "n/a", "nan"}
BOOL_TRUE = {"true", "yes"}
BOOL_FALSE = {"false", "no"}
INT_RE = re.compile(r"[+-]?[0-9]+")
FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
)
# A str column is "low cardinality" when it has at most this many distinct values.
LOW_CARD_MAX = 10
TYPE_COLORS = {"int": "green", "float": "green", "bool": "magenta",
               "date": "yellow", "str": "cyan"}


class CsvPeekError(Exception):
    """Bad input or path; the CLI turns this into exit code 2."""


def is_null(value):
    return value.strip().lower() in NULL_TOKENS


def is_date(value):
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def infer_type(values):
    """Heuristic: try int, float, bool, date parsers over non-null values.

    `values` must be stripped, non-null strings. An empty list falls back to
    str. A column of 0/1 is int, not bool; dates are recognized only in the
    formats listed in DATE_FORMATS.
    """
    if not values:
        return "str"
    if all(INT_RE.fullmatch(v) for v in values):
        return "int"
    if all(FLOAT_RE.fullmatch(v) for v in values):
        return "float"
    if all(v.lower() in BOOL_TRUE or v.lower() in BOOL_FALSE for v in values):
        return "bool"
    if all(is_date(v) for v in values):
        return "date"
    return "str"


def read_csv(path, delimiter):
    """Return (header, rows). Raises CsvPeekError on unreadable/malformed input.

    Blank lines are skipped; any other row whose field count differs from the
    header is treated as malformed.
    """
    if os.path.isdir(path):
        raise CsvPeekError("not a file: %s" % path)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh, delimiter=delimiter, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise CsvPeekError("empty file: no header row: %s" % path)
            rows = []
            for i, row in enumerate(reader, start=1):
                if not row:
                    continue  # blank line
                if len(row) != len(header):
                    raise CsvPeekError(
                        "%s: row %d has %d fields, header has %d"
                        % (path, i, len(row), len(header)))
                rows.append(row)
    except FileNotFoundError:
        raise CsvPeekError("path not found: %s" % path)
    except IsADirectoryError:
        raise CsvPeekError("not a file: %s" % path)
    except UnicodeDecodeError:
        raise CsvPeekError("cannot decode %s as UTF-8" % path)
    except csv.Error as exc:
        raise CsvPeekError("malformed CSV in %s: %s" % (path, exc))
    except OSError as exc:
        raise CsvPeekError("cannot read %s: %s" % (path, exc))
    return header, rows


def build_profile(path, header, rows, delimiter, sample_rows):
    """Build the profile struct (also what `peek --json` emits)."""
    profile = []
    for i, name in enumerate(header):
        values = [r[i] for r in rows]
        non_null = [v.strip() for v in values if not is_null(v)]
        kind = infer_type(non_null)
        col = {"name": name, "type": kind, "empty": len(values) - len(non_null)}
        if kind in ("int", "float"):
            nums = [int(v) if kind == "int" else float(v) for v in non_null]
            col["min"] = min(nums)
            col["max"] = max(nums)
            col["mean"] = round(sum(nums) / len(nums), 6)
        elif kind == "bool":
            col["true"] = sum(1 for v in non_null if v.lower() in BOOL_TRUE)
            col["false"] = len(non_null) - col["true"]
        elif kind == "str" and non_null:
            counts = Counter(v for v in values if not is_null(v))
            if len(counts) <= LOW_CARD_MAX:
                top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
                col["top"] = [[v, n] for v, n in top]
        profile.append(col)
    return {"file": path, "delimiter": delimiter, "rows": len(rows),
            "columns": len(header), "profile": profile,
            "sample": [list(r) for r in rows[:sample_rows]]}


def coerce_rows(header, rows):
    """CSV rows -> list of dicts with numbers/bools coerced per inferred type.

    Nulls become None; date and str values stay strings (dates are not
    reformatted). Duplicate header names cannot map to JSON objects.
    """
    if len(set(header)) != len(header):
        raise CsvPeekError("duplicate column names; cannot map to JSON objects")
    types = []
    for i in range(len(header)):
        types.append(infer_type([r[i].strip() for r in rows if not is_null(r[i])]))
    out = []
    for row in rows:
        obj = {}
        for name, value, kind in zip(header, row, types):
            if is_null(value):
                obj[name] = None
            elif kind == "int":
                obj[name] = int(value.strip())
            elif kind == "float":
                obj[name] = float(value.strip())
            elif kind == "bool":
                obj[name] = value.strip().lower() in BOOL_TRUE
            else:
                obj[name] = value
        out.append(obj)
    return out


def read_json(path):
    """Load a JSON array of objects. Raises CsvPeekError otherwise."""
    if os.path.isdir(path):
        raise CsvPeekError("not a file: %s" % path)
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise CsvPeekError("path not found: %s" % path)
    except IsADirectoryError:
        raise CsvPeekError("not a file: %s" % path)
    except UnicodeDecodeError:
        raise CsvPeekError("cannot decode %s as UTF-8" % path)
    except json.JSONDecodeError as exc:
        raise CsvPeekError("invalid JSON in %s: %s" % (path, exc))
    except OSError as exc:
        raise CsvPeekError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, list):
        raise CsvPeekError("%s: expected a JSON array of objects" % path)
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise CsvPeekError("%s: item %d is not an object" % (path, i))
    return data


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def json_to_csv(data, delimiter):
    """JSON array of objects -> CSV text; header is the union of keys in
    first-seen order, missing keys become empty cells."""
    keys, seen = [], set()
    for obj in data:
        for key in obj:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    if keys:
        writer.writerow(keys)
    for obj in data:
        writer.writerow([csv_cell(obj.get(k)) for k in keys])
    return buf.getvalue()


def colorize(text, color, enabled):
    if not enabled or color is None:
        return text
    codes = {"red": "31", "green": "32", "yellow": "33", "blue": "34",
             "magenta": "35", "cyan": "36", "bold": "1", "dim": "2"}
    return "\x1b[%sm%s\x1b[0m" % (codes[color], text)


def fmt_num(x):
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def render_profile(prof, color):
    lines = [colorize("PEEK %s" % prof["file"], "bold", color)]
    lines.append("  rows      %d" % prof["rows"])
    lines.append("  columns   %d" % prof["columns"])
    delim = "\\t" if prof["delimiter"] == "\t" else prof["delimiter"]
    lines.append("  delimiter '%s'" % delim)
    lines.append("")
    lines.append(colorize("COLUMNS", "bold", color))
    table = [["name", "type", "empty", "details"]]
    for col in prof["profile"]:
        details = ""
        if col["type"] in ("int", "float"):
            details = "min %s  max %s  mean %s" % (
                fmt_num(col["min"]), fmt_num(col["max"]), fmt_num(col["mean"]))
        elif col["type"] == "bool":
            details = "true=%d false=%d" % (col["true"], col["false"])
        elif col.get("top"):
            details = "top: " + ", ".join("%s (%d)" % (v, n) for v, n in col["top"])
        table.append([col["name"], col["type"], str(col["empty"]), details])
    widths = [max(len(r[i]) for r in table) for i in range(4)]
    head = "  ".join(h.ljust(w) for h, w in zip(table[0], widths)).rstrip()
    lines.append("  " + colorize(head, "bold", color))
    for row in table[1:]:
        cells = [row[0].ljust(widths[0]),
                 colorize(row[1].ljust(widths[1]), TYPE_COLORS[row[1]], color),
                 row[2].ljust(widths[2]), row[3]]
        lines.append("  " + "  ".join(cells).rstrip())
    if prof["sample"]:
        lines.append("")
        lines.append(colorize("SAMPLE (first %d of %d rows)"
                              % (len(prof["sample"]), prof["rows"]), "bold", color))
        names = [c["name"] for c in prof["profile"]]
        sample = [names] + prof["sample"]
        swidths = [max(len(r[i]) for r in sample) for i in range(len(names))]
        head = "  ".join(h.ljust(w) for h, w in zip(names, swidths)).rstrip()
        lines.append("  " + colorize(head, "cyan", color))
        for row in prof["sample"]:
            lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(row, swidths)).rstrip())
    return "\n".join(lines)


def use_color(no_color=False, tty=None):
    if tty is None:
        tty = sys.stdout.isatty()
    return not no_color and not os.environ.get("NO_COLOR") and tty


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="disable ANSI colors (also honors NO_COLOR)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output (peek only)")
    parser = argparse.ArgumentParser(
        prog="csvpeek", parents=[common],
        description="Quick CSV profiling and CSV/JSON conversion.")
    parser.add_argument("--version", action="version", version="csvpeek " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_peek = sub.add_parser("peek", parents=[common], help="profile a CSV file")
    p_peek.add_argument("file", help="CSV file to profile")
    p_peek.add_argument("--delimiter", default=",", help="field delimiter (or \\t)")
    p_peek.add_argument("--rows", type=int, default=5, help="sample rows to show (default 5)")

    p_tojson = sub.add_parser("tojson", parents=[common], help="CSV -> JSON array of row objects")
    p_tojson.add_argument("file", help="CSV file to convert")
    p_tojson.add_argument("--delimiter", default=",", help="field delimiter (or \\t)")

    p_tocsv = sub.add_parser("tocsv", parents=[common], help="JSON array of objects -> CSV")
    p_tocsv.add_argument("file", help="JSON file to convert")
    p_tocsv.add_argument("--delimiter", default=",", help="output field delimiter (or \\t)")
    return parser


def main(argv=None):
    parser = build_parser()
    opts = parser.parse_args(argv)
    no_color = getattr(opts, "no_color", False)
    as_json = getattr(opts, "json", False)
    color = use_color(no_color)
    delimiter = opts.delimiter
    if delimiter == "\\t":
        delimiter = "\t"
    if len(delimiter) != 1:
        parser.error("--delimiter must be a single character (or \\t)")
    try:
        if opts.command == "peek":
            if opts.rows < 0:
                parser.error("--rows must be >= 0")
            header, rows = read_csv(opts.file, delimiter)
            prof = build_profile(opts.file, header, rows, delimiter, opts.rows)
            if as_json:
                print(json.dumps(prof, indent=2, ensure_ascii=False))
            else:
                print(render_profile(prof, color))
        elif opts.command == "tojson":
            header, rows = read_csv(opts.file, delimiter)
            print(json.dumps(coerce_rows(header, rows), indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(json_to_csv(read_json(opts.file), delimiter))
    except CsvPeekError as exc:
        sys.stderr.write("csvpeek: %s\n" % exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())