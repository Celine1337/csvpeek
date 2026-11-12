import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import csvpeek as cp

DEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")


def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cp.main(args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        return path

    def profile(self, text, delimiter=",", sample_rows=5, name="t.csv"):
        path = self.write(name, text)
        header, rows = cp.read_csv(path, delimiter)
        return cp.build_profile(path, header, rows, delimiter, sample_rows)


class Inference(unittest.TestCase):
    def test_int(self):
        self.assertEqual(cp.infer_type(["1", "-2", "+3", "007"]), "int")

    def test_float_mixed(self):
        self.assertEqual(cp.infer_type(["1", "2.5", "-0.5", "1e3", ".5"]), "float")

    def test_bool(self):
        self.assertEqual(cp.infer_type(["true", "FALSE", "Yes", "no"]), "bool")

    def test_date_iso(self):
        self.assertEqual(cp.infer_type(["2024-01-15", "2024-12-06"]), "date")

    def test_date_alt_formats(self):
        self.assertEqual(cp.infer_type(["15.01.2024", "01/15/2024", "2024-01-15T10:30:00"]), "date")

    def test_str_fallback(self):
        self.assertEqual(cp.infer_type(["1", "abc"]), "str")
        self.assertEqual(cp.infer_type(["2024-01-15", "yesterday"]), "str")

    def test_all_empty_is_str(self):
        self.assertEqual(cp.infer_type([]), "str")

    def test_zero_one_is_int_not_bool(self):
        self.assertEqual(cp.infer_type(["0", "1", "1"]), "int")

    def test_no_python_parser_quirks(self):
        # int("1_000") and float("inf") would parse in Python; the regexes must not.
        self.assertEqual(cp.infer_type(["1_000"]), "str")
        self.assertEqual(cp.infer_type(["inf", "1"]), "str")

    def test_null_tokens(self):
        for token in ["", "  ", "NULL", "None", " N/A ", "nan", "NA"]:
            self.assertTrue(cp.is_null(token), token)
        for token in ["0", "x", "false", "none-ish"]:
            self.assertFalse(cp.is_null(token), token)


class Profile(TempFiles):
    def test_shape_and_counts(self):
        prof = self.profile("a,b\n1,x\n2,y\n3,z\n")
        self.assertEqual(prof["rows"], 3)
        self.assertEqual(prof["columns"], 2)
        self.assertEqual(prof["delimiter"], ",")
        self.assertEqual([c["name"] for c in prof["profile"]], ["a", "b"])
        self.assertEqual(prof["sample"], [["1", "x"], ["2", "y"], ["3", "z"]])

    def test_numeric_stats(self):
        prof = self.profile("i,f\n1,1.5\n2,2.5\n3,\n4,0.5\n")
        ci, cf = prof["profile"]
        self.assertEqual(ci["type"], "int")
        self.assertEqual((ci["min"], ci["max"], ci["mean"]), (1, 4, 2.5))
        self.assertEqual(cf["type"], "float")
        self.assertEqual((cf["min"], cf["max"], cf["mean"]), (0.5, 2.5, 1.5))
        self.assertEqual(cf["empty"], 1)

    def test_empty_counts_all_null_tokens(self):
        prof = self.profile("a,b\n1,x\n,y\nnull,z\nN/A,w\n2,v\n")
        self.assertEqual(prof["profile"][0]["empty"], 3)
        self.assertEqual(prof["profile"][0]["type"], "int")

    def test_blank_lines_skipped(self):
        prof = self.profile("a,b\n1,x\n\n2,y\n")
        self.assertEqual(prof["rows"], 2)

    def test_top_values_order_and_cap(self):
        prof = self.profile("c\nb\nb\na\na\na\nc\nd\n")
        self.assertEqual(prof["profile"][0]["top"], [["a", 3], ["b", 2], ["c", 1]])

    def test_no_top_for_high_cardinality(self):
        text = "c\n" + "".join("v%d\n" % i for i in range(cp.LOW_CARD_MAX + 1))
        prof = self.profile(text)
        self.assertNotIn("top", prof["profile"][0])

    def test_bool_counts(self):
        prof = self.profile("b\ntrue\nno\nTRUE\nfalse\n")
        col = prof["profile"][0]
        self.assertEqual(col["type"], "bool")
        self.assertEqual((col["true"], col["false"]), (2, 2))

    def test_sample_limit(self):
        text = "a\n" + "".join("%d\n" % i for i in range(12))
        self.assertEqual(len(self.profile(text)["sample"]), 5)
        self.assertEqual(len(self.profile(text, sample_rows=2)["sample"]), 2)
        self.assertEqual(self.profile(text, sample_rows=0)["sample"], [])

    def test_quoted_fields(self):
        prof = self.profile('a,b\n"x,1","say ""hi"""\n"multi\nline",2\n')
        self.assertEqual(prof["rows"], 2)
        self.assertEqual(prof["sample"][0], ["x,1", 'say "hi"'])
        self.assertEqual(prof["sample"][1], ["multi\nline", "2"])

    def test_custom_delimiter(self):
        prof = self.profile("a;b\n1;x\n2;y\n", delimiter=";")
        self.assertEqual(prof["columns"], 2)
        self.assertEqual(prof["profile"][0]["type"], "int")

    def test_bom_header(self):
        path = self.write("bom.csv", "\ufeffa,b\n1,2\n", encoding="utf-8")
        header, rows = cp.read_csv(path, ",")
        self.assertEqual(header, ["a", "b"])

    def test_header_only(self):
        prof = self.profile("a,b\n")
        self.assertEqual(prof["rows"], 0)
        self.assertEqual(prof["columns"], 2)
        self.assertEqual([c["type"] for c in prof["profile"]], ["str", "str"])
        self.assertEqual(prof["sample"], [])

    def test_demo_people(self):
        header, rows = cp.read_csv(os.path.join(DEMO, "people.csv"), ",")
        prof = cp.build_profile("people.csv", header, rows, ",", 5)
        kinds = {c["name"]: c["type"] for c in prof["profile"]}
        self.assertEqual(kinds, {"id": "int", "name": "str", "city": "str",
                                 "score": "float", "active": "bool", "joined": "date"})
        score = next(c for c in prof["profile"] if c["name"] == "score")
        self.assertEqual((score["empty"], score["min"], score["max"], score["mean"]),
                         (2, 5.5, 9.5, 7.75))
        city = next(c for c in prof["profile"] if c["name"] == "city")
        self.assertEqual(city["top"], [["Berlin", 5], ["Paris", 4], ["Rome", 3]])


class ToJson(TempFiles):
    def test_coercion(self):
        path = self.write("t.csv", "i,f,b,d,s\n1,1.5,true,2024-01-15,hello\n2,,yes,2024-02-20,\n")
        header, rows = cp.read_csv(path, ",")
        data = cp.coerce_rows(header, rows)
        self.assertEqual(data[0], {"i": 1, "f": 1.5, "b": True, "d": "2024-01-15", "s": "hello"})
        self.assertEqual(data[1], {"i": 2, "f": None, "b": True, "d": "2024-02-20", "s": None})
        self.assertIsInstance(data[0]["i"], int)
        self.assertIsInstance(data[0]["f"], float)

    def test_str_values_preserved(self):
        path = self.write("t.csv", "s\n  padded  \n")
        header, rows = cp.read_csv(path, ",")
        self.assertEqual(cp.coerce_rows(header, rows), [{"s": "  padded  "}])

    def test_duplicate_header_rejected(self):
        path = self.write("t.csv", "a,a\n1,2\n")
        header, rows = cp.read_csv(path, ",")
        with self.assertRaises(cp.CsvPeekError):
            cp.coerce_rows(header, rows)


class ToCsv(unittest.TestCase):
    def test_union_of_keys_first_seen(self):
        data = [{"b": 1, "a": 2}, {"c": 3, "b": 4}]
        self.assertEqual(cp.json_to_csv(data, ","), "b,a,c\n1,2,\n4,,3\n")

    def test_value_encoding(self):
        data = [{"n": None, "b": True, "f": False, "x": 1.5, "o": {"k": 1}, "l": [1, 2]}]
        out = cp.json_to_csv(data, ",")
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[1], ["", "true", "false", "1.5", '{"k": 1}', "[1, 2]"])

    def test_empty_array(self):
        self.assertEqual(cp.json_to_csv([], ","), "")

    def test_quoting_roundtrip(self):
        data = [{"a": "x,y", "b": 'say "hi"', "c": "line\nbreak"}]
        out = cp.json_to_csv(data, ",")
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[1], ["x,y", 'say "hi"', "line\nbreak"])

    def test_custom_delimiter(self):
        self.assertEqual(cp.json_to_csv([{"a": 1, "b": 2}], ";"), "a;b\n1;2\n")

    def test_roundtrip_from_csv(self):
        text = "id,score,active\n1,9.5,true\n2,,false\n"
        reader = list(csv.reader(io.StringIO(text)))
        data = cp.coerce_rows(reader[0], reader[1:])
        out = cp.json_to_csv(data, ",")
        self.assertEqual(out, "id,score,active\n1,9.5,true\n2,,false\n")


class Cli(TempFiles):
    def test_version(self):
        code, out, _ = run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("csvpeek " + cp.__version__, out)

    def test_peek_text(self):
        path = self.write("t.csv", "a,b\n1,x\n2,y\n")
        code, out, err = run_cli(["peek", path, "--no-color"])
        self.assertEqual(code, 0, err)
        self.assertIn("PEEK", out)
        self.assertIn("COLUMNS", out)
        self.assertIn("SAMPLE (first 2 of 2 rows)", out)
        self.assertNotIn("\x1b", out)

    def test_peek_json_struct(self):
        path = self.write("t.csv", "a,b\n1,x\n2,y\n")
        code, out, err = run_cli(["peek", path, "--json"])
        self.assertEqual(code, 0, err)
        prof = json.loads(out)
        self.assertEqual(prof["rows"], 2)
        self.assertEqual(prof["columns"], 2)
        self.assertEqual(len(prof["profile"]), 2)
        self.assertEqual(prof["sample"], [["1", "x"], ["2", "y"]])

    def test_peek_rows_zero_no_sample(self):
        path = self.write("t.csv", "a\n1\n2\n")
        code, out, _ = run_cli(["peek", path, "--rows", "0", "--no-color"])
        self.assertEqual(code, 0)
        self.assertNotIn("SAMPLE", out)

    def test_tojson_cli(self):
        path = self.write("t.csv", "i,b\n1,true\n2,false\n")
        code, out, err = run_cli(["tojson", path])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), [{"i": 1, "b": True}, {"i": 2, "b": False}])

    def test_tocsv_cli(self):
        path = self.write("t.json", json.dumps([{"a": 1}, {"a": 2, "b": "x"}]))
        code, out, err = run_cli(["tocsv", path])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "a,b\n1,\n2,x\n")

    def test_tab_delimiter_escape(self):
        path = self.write("t.tsv", "a\tb\n1\t2\n")
        code, out, err = run_cli(["peek", path, "--delimiter", "\\t", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["columns"], 2)

    def test_missing_file_exit2(self):
        code, _, err = run_cli(["peek", os.path.join(self.root, "nope.csv")])
        self.assertEqual(code, 2)
        self.assertIn("path not found", err)

    def test_directory_exit2(self):
        code, _, err = run_cli(["tojson", self.root])
        self.assertEqual(code, 2)
        self.assertIn("not a file", err)

    def test_ragged_row_exit2(self):
        path = self.write("t.csv", "a,b\n1,2\n3\n")
        code, _, err = run_cli(["peek", path])
        self.assertEqual(code, 2)
        self.assertIn("row 2 has 1 fields", err)

    def test_empty_file_exit2(self):
        path = self.write("t.csv", "")
        code, _, err = run_cli(["peek", path])
        self.assertEqual(code, 2)
        self.assertIn("no header row", err)

    def test_malformed_quote_exit2(self):
        path = self.write("t.csv", 'a,b\n1,"2\n')
        code, _, err = run_cli(["peek", path])
        self.assertEqual(code, 2)
        self.assertIn("malformed CSV", err)

    def test_bad_encoding_exit2(self):
        path = os.path.join(self.root, "t.csv")
        with open(path, "wb") as fh:
            fh.write(b"a,b\n\xff\xfe,2\n")
        code, _, err = run_cli(["peek", path])
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", err)

    def test_bad_delimiter_exit2(self):
        path = self.write("t.csv", "a\n1\n")
        code, _, err = run_cli(["peek", path, "--delimiter", "ab"])
        self.assertEqual(code, 2)

    def test_negative_rows_exit2(self):
        path = self.write("t.csv", "a\n1\n")
        code, _, err = run_cli(["peek", path, "--rows", "-1"])
        self.assertEqual(code, 2)

    def test_no_command_exit2(self):
        code, _, err = run_cli([])
        self.assertEqual(code, 2)

    def test_tocsv_bad_json_exit2(self):
        path = self.write("t.json", "{ nope")
        code, _, err = run_cli(["tocsv", path])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_tocsv_not_array_exit2(self):
        path = self.write("t.json", '{"a": 1}')
        code, _, err = run_cli(["tocsv", path])
        self.assertEqual(code, 2)
        self.assertIn("array of objects", err)

    def test_tocsv_non_object_item_exit2(self):
        path = self.write("t.json", "[1, 2]")
        code, _, err = run_cli(["tocsv", path])
        self.assertEqual(code, 2)
        self.assertIn("not an object", err)

    def test_demo_smoke(self):
        code, out, err = run_cli(["peek", os.path.join(DEMO, "people.csv"), "--no-color"])
        self.assertEqual(code, 0, err)
        self.assertIn("rows      12", out)
        self.assertIn("Berlin (5)", out)


class Colors(unittest.TestCase):
    def test_use_color_matrix(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cp.use_color(False, tty=True))
            self.assertFalse(cp.use_color(True, tty=True))
            self.assertFalse(cp.use_color(False, tty=False))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(cp.use_color(False, tty=True))

    def test_render_with_color_has_ansi(self):
        prof = {"file": "t.csv", "delimiter": ",", "rows": 1, "columns": 1,
                "profile": [{"name": "a", "type": "int", "empty": 0,
                             "min": 1, "max": 1, "mean": 1.0}],
                "sample": [["1"]]}
        ansi = cp.render_profile(prof, True)
        self.assertIn("\x1b[", ansi)
        self.assertNotIn("\x1b", cp.render_profile(prof, False))

    def test_cli_no_color_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a\n1\n")
            code, out, _ = run_cli(["peek", path, "--no-color"])
            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out)


if __name__ == "__main__":
    unittest.main()