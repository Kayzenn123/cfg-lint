"""Tests for cfg_lint (stdlib unittest, no dependencies)."""
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cfg_lint

HERE = os.path.dirname(__file__)
FIX = os.path.join(HERE, "fixtures")


def read(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read().decode("utf-8")


def codes(issues):
    return sorted(i.code for i in issues)


class JsonChecks(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cfg_lint.check_json_text(read("valid.json"), "f"), [])

    def test_syntax_error_has_line_col(self):
        issues = cfg_lint.check_json_text(read("bad.json"), "f")
        self.assertEqual(len(issues), 1)
        i = issues[0]
        self.assertEqual(i.code, "J1001")
        self.assertEqual(i.severity, "error")
        self.assertIsNotNone(i.line)

    def test_duplicate_keys_found_with_paths(self):
        issues = cfg_lint.check_json_text(read("dup.json"), "f")
        dups = [i for i in issues if i.code == "J1002"]
        self.assertEqual(len(dups), 2)
        paths = sorted(i.message for i in dups)
        self.assertTrue(any("$.server.port" in m for m in paths))
        self.assertTrue(any(m.endswith("at $.port (earlier value is shadowed)") for m in paths))

    def test_duplicate_key_line_numbers(self):
        issues = cfg_lint.check_json_text(read("dup.json"), "f")
        by_path = {}
        for i in issues:
            if i.code == "J1002":
                by_path["server" if "server.port" in i.message else "top"] = i.line
        self.assertEqual(by_path["server"], 5)
        self.assertEqual(by_path["top"], 7)

    def test_duplicate_inside_array(self):
        text = '{"items": [{"id": 1, "id": 2}]}'
        issues = cfg_lint.check_json_text(text, "f")
        dups = [i for i in issues if i.code == "J1002"]
        self.assertEqual(len(dups), 1)
        self.assertIn("$.items[0].id", dups[0].message)

    def test_bom_is_warning_only(self):
        issues = cfg_lint.check_json_text("﻿{\"a\": 1}", "f")
        self.assertEqual(codes(issues), ["J1003"])
        self.assertEqual(issues[0].severity, "warning")

    def test_empty_document_is_error(self):
        issues = cfg_lint.check_json_text("", "f")
        self.assertEqual(codes(issues), ["J1001"])

    def test_escaped_keys(self):
        text = '{"a\\"b": 1, "a\\"b": 2}'
        dups = [i for i in cfg_lint.check_json_text(text, "f") if i.code == "J1002"]
        self.assertEqual(len(dups), 1)


class TomlChecks(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cfg_lint.check_toml_text(read("valid.toml"), "f"), [])

    def test_syntax_error(self):
        issues = cfg_lint.check_toml_text(read("bad.toml"), "f")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "T1001")
        self.assertEqual(issues[0].severity, "error")
        self.assertEqual(issues[0].line, 1)

    def test_duplicate_key(self):
        issues = cfg_lint.check_toml_text("[s]\na = 1\na = 2\n", "f")
        self.assertTrue(any(i.code == "T1001" for i in issues))


class EnvChecks(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cfg_lint.check_env_text(read("valid.env"), "f"), [])

    def test_bad_file_codes(self):
        got = codes(cfg_lint.check_env_text(read("bad.env"), "f"))
        for want in ("E2001", "E2002", "E2004", "E2005", "W2003",
                     "W2006", "W2007", "W2008"):
            self.assertIn(want, got, "missing %s in %s" % (want, got))

    def test_duplicate_reports_first_line(self):
        issues = cfg_lint.check_env_text("A=1\nA=2\n", "f")
        dups = [i for i in issues if i.code == "E2001"]
        self.assertEqual(len(dups), 1)
        self.assertIn("line 1", dups[0].message)
        self.assertEqual(dups[0].line, 2)

    def test_export_prefix_ok(self):
        self.assertEqual(cfg_lint.check_env_text("export A=1\n", "f"), [])

    def test_comments_and_blanks_ignored(self):
        text = "# comment\n\nA=1\n"
        self.assertEqual(cfg_lint.check_env_text(text, "f"), [])

    def test_quoted_value_ok(self):
        text = 'A="hello world"\nB=\'it works\'\n'
        self.assertEqual(cfg_lint.check_env_text(text, "f"), [])


class CliBehavior(unittest.TestCase):
    def run_cli(self, *argv, stdin=None):
        return cfg_lint.main(list(argv))

    def test_main_valid_json_exit_0(self):
        rc = self.run_cli("json", os.path.join(FIX, "valid.json"))
        self.assertEqual(rc, 0)

    def test_main_invalid_json_exit_1(self):
        rc = self.run_cli("json", os.path.join(FIX, "bad.json"))
        self.assertEqual(rc, 1)

    def test_main_warnings_only_exit_0(self):
        rc = self.run_cli("env", os.path.join(FIX, "valid.env"))
        self.assertEqual(rc, 0)

    def test_main_strict_warnings_exit_1(self):
        with open(os.path.join(FIX, "warn.env"), "w") as fh:
            fh.write("lowercase=x\n")
        try:
            self.assertEqual(self.run_cli("env", os.path.join(FIX, "warn.env")), 0)
            self.assertEqual(self.run_cli("--strict", "env",
                                          os.path.join(FIX, "warn.env")), 1)
        finally:
            os.remove(os.path.join(FIX, "warn.env"))

    def test_main_missing_file_exit_1(self):
        rc = self.run_cli("json", os.path.join(FIX, "nope.json"))
        self.assertEqual(rc, 1)

    def test_all_dispatch(self):
        rc = self.run_cli("all", FIX)
        self.assertEqual(rc, 1)  # bad fixtures present

    def test_no_color_flag(self):
        issue = cfg_lint.Issue("f", 1, 2, "E2001", "error", "msg")
        plain = cfg_lint._format_plain(issue, color=False)
        self.assertEqual(plain, "f:1:2: error: [E2001] msg")
        colored = cfg_lint._format_plain(issue, color=True)
        self.assertIn("\033[", colored)

    def test_json_output_is_parseable(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = self.run_cli("--json", "json", os.path.join(FIX, "dup.json"))
        finally:
            sys.stdout = old
        self.assertEqual(rc, 1)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["tool"], "cfg-lint")
        self.assertEqual(data["summary"]["errors"], 2)
        self.assertTrue(all("code" in i for i in data["issues"]))


class EndToEnd(unittest.TestCase):
    """The CLI as a real subprocess: output text and exit codes."""

    def cli(self, *argv, stdin_data=None):
        exe = os.path.join(os.path.dirname(HERE), "cfg_lint.py")
        proc = subprocess.run(
            [sys.executable, exe] + list(argv),
            input=stdin_data, capture_output=True, text=True, cwd=HERE)
        return proc

    def test_e2e_valid(self):
        p = self.cli("json", "fixtures/valid.json")
        self.assertEqual(p.returncode, 0)
        self.assertIn("no issues", p.stderr)

    def test_e2e_invalid_reports_location(self):
        p = self.cli("json", "fixtures/dup.json")
        self.assertEqual(p.returncode, 1)
        self.assertIn("[J1002]", p.stdout)
        self.assertIn("$.server.port", p.stdout)

    def test_e2e_stdin(self):
        p = self.cli("env", stdin_data="A=1\nA=2\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("<stdin>", p.stdout)
        self.assertIn("[E2001]", p.stdout)

    def test_e2e_help_per_subcommand(self):
        for cmd in ("json", "toml", "env", "all"):
            p = self.cli(cmd, "--help")
            self.assertEqual(p.returncode, 0, cmd)
            self.assertIn("usage:", p.stdout)

    def test_e2e_version(self):
        p = self.cli("--version")
        self.assertEqual(p.returncode, 0)
        self.assertIn(cfg_lint.__version__, p.stdout)

    def test_e2e_no_subcommand_exit_2(self):
        p = self.cli()
        self.assertEqual(p.returncode, 2)

    def test_e2e_no_color_env(self):
        env = dict(os.environ, NO_COLOR="1")
        exe = os.path.join(os.path.dirname(HERE), "cfg_lint.py")
        p = subprocess.run([sys.executable, exe, "json", "fixtures/dup.json"],
                           capture_output=True, text=True, cwd=HERE, env=env)
        self.assertNotIn("\033[", p.stdout)


if __name__ == "__main__":
    unittest.main()
