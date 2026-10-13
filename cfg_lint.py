#!/usr/bin/env python3
"""cfg-lint: validate JSON, TOML, and .env config files from the CLI.

Zero dependencies, stdlib only. Exit codes: 0 = no errors, 1 = issues
found or a file could not be read, 2 = bad arguments.
"""

import argparse
import json
import os
import re
import sys

__version__ = "1.0.0"

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


# ---------------------------------------------------------------------------
# Issue model
# ---------------------------------------------------------------------------

class Issue:
    __slots__ = ("file", "line", "col", "code", "severity", "message")

    def __init__(self, file, line, col, code, severity, message):
        self.file = file
        self.line = line
        self.col = col
        self.code = code
        self.severity = severity  # "error" | "warning"
        self.message = message

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# JSON checks
# ---------------------------------------------------------------------------

def _tokenize_json(text):
    """Split JSON text into structural tokens. Assumes text parses as JSON."""
    toks = []
    i, n = 0, len(text)
    line, col = 1, 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            col = 1
            i += 1
        elif c in " \t\r":
            col += 1
            i += 1
        elif c in "{}[],:":
            toks.append((c, line, col))
            col += 1
            i += 1
        elif c == '"':
            j = i + 1
            raw_chars = []
            while j < n:
                d = text[j]
                if d == "\\":
                    raw_chars.append(text[j:j + 2])
                    j += 2
                elif d == '"':
                    break
                else:
                    raw_chars.append(d)
                    j += 1
            raw = "".join(raw_chars)
            try:
                value = json.loads('"' + raw + '"')
            except Exception:
                value = raw
            toks.append(("str", line, col, value))
            span = text[i:j + 1]
            newlines = span.count("\n")
            if newlines:
                line += newlines
                col = len(span.rsplit("\n", 1)[1]) + 1
            else:
                col += len(span)
            i = j + 1
        else:
            # numbers, true/false/null, BOM: irrelevant for key tracking
            col += 1
            i += 1
    return toks


def _json_key_positions(text):
    """Return [(line, col, path_tuple, key)] for duplicate object keys."""
    toks = _tokenize_json(text)
    dups = []
    # frame: [type, path, keys, expect_key, pending_key, arr_index]
    stack = []
    for tok in toks:
        kind = tok[0]
        if kind in ("{", "["):
            if not stack:
                path = ()
            else:
                top = stack[-1]
                if top[0] == "arr":
                    top[5] += 1
                    path = top[1] + (top[5],)
                elif top[4] is not None:
                    path = top[1] + (top[4][0],)
                    top[4] = None
                    top[3] = False
                else:
                    path = top[1]
            stack.append(["obj" if kind == "{" else "arr", path, {}, True, None, -1])
        elif kind in ("}", "]"):
            if stack:
                stack.pop()
        elif kind == "str":
            _, sline, scol, s = tok
            if stack and stack[-1][0] == "obj" and stack[-1][3]:
                top = stack[-1]
                if s in top[2]:
                    dups.append((sline, scol, top[1] + (s,), s))
                else:
                    top[2][s] = (sline, scol)
                top[4] = (s, sline, scol)
                top[3] = False
            elif stack and stack[-1][0] == "obj":
                stack[-1][4] = None  # a string value; key consumed
        elif kind == ",":
            if stack and stack[-1][0] == "obj":
                stack[-1][3] = True
                stack[-1][4] = None
    return dups


def _fmt_path(path):
    out = "$"
    for part in path:
        if isinstance(part, int):
            out += "[%d]" % part
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part or ""):
            out += "." + part
        else:
            out += "[" + json.dumps(part) + "]"
    return out


def check_json_text(text, label):
    """Validate JSON: syntax, duplicate keys, BOM."""
    issues = []
    if text.startswith("﻿"):
        issues.append(Issue(label, 1, 1, "J1003", "warning",
                            "file starts with a UTF-8 BOM"))
        text = text[1:]
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        issues.append(Issue(label, exc.lineno, exc.colno, "J1001", "error",
                            "invalid JSON: %s" % exc.msg))
        return issues
    try:
        dups = _json_key_positions(text)
    except Exception:
        dups = []
    for sline, scol, path, key in dups:
        issues.append(Issue(label, sline, scol, "J1002", "error",
                            "duplicate key %r at %s (earlier value is shadowed)"
                            % (key, _fmt_path(path))))
    return issues


# ---------------------------------------------------------------------------
# TOML checks
# ---------------------------------------------------------------------------

_TOML_POS_RE = re.compile(r"\(at line (\d+), column (\d+)\)")


def check_toml_text(text, label):
    """Validate TOML syntax (Python 3.11+ tomllib)."""
    if tomllib is None:
        return [Issue(label, None, None, "T1002", "error",
                       "TOML validation needs Python 3.11+ (tomllib is missing)")]
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = str(exc)
        match = _TOML_POS_RE.search(msg)
        line = int(match.group(1)) if match else None
        col = int(match.group(2)) if match else None
        return [Issue(label, line, col, "T1001", "error",
                       "invalid TOML: %s" % msg)]
    return []


# ---------------------------------------------------------------------------
# .env checks
# ---------------------------------------------------------------------------

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unbalanced_quotes(value):
    if len(value) < 1:
        return False
    if value[0] in "\"'":
        return not (len(value) > 1 and value.endswith(value[0]))
    return False


def check_env_text(text, label):
    """Validate dotenv files: names, duplicates, quoting, formatting."""
    issues = []
    if text and not text.endswith("\n"):
        last_line = text.count("\n") + 1
        issues.append(Issue(label, last_line, None, "W2007", "warning",
                            "file does not end with a newline"))
    seen = {}
    for lineno, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = stripped
        if body.startswith("export ") or body.startswith("export\t"):
            body = body[6:].lstrip()
        if "=" not in body:
            issues.append(Issue(label, lineno, None, "E2004", "error",
                                "not a KEY=VALUE line (missing '=')"))
            continue
        key_raw, value = body.split("=", 1)
        if key_raw != key_raw.strip() or (value and value[0] in " \t"):
            issues.append(Issue(label, lineno, None, "W2003", "warning",
                                "whitespace around '=' is not portable across parsers"))
        key = key_raw.strip()
        if not _ENV_KEY_RE.match(key):
            issues.append(Issue(label, lineno, None, "E2002", "error",
                                "invalid key name %r (use A-Z, 0-9, _; must not start with a digit)"
                                % key))
            continue
        if key in seen:
            issues.append(Issue(label, lineno, None, "E2001", "error",
                                "duplicate key %r (first defined on line %d)"
                                % (key, seen[key])))
        else:
            seen[key] = lineno
        if _unbalanced_quotes(value.strip()):
            issues.append(Issue(label, lineno, None, "E2005", "error",
                                "unbalanced quotes in value of %r" % key))
        elif value.strip() == "":
            issues.append(Issue(label, lineno, None, "W2006", "warning",
                                "empty value for %r" % key))
        if key != key.upper():
            issues.append(Issue(label, lineno, None, "W2008", "warning",
                                "key %r is not uppercase (convention)" % key))
    return issues


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def _read_input(path):
    """Return (text, error_message). path == '-' reads stdin."""
    if path == "-":
        try:
            return sys.stdin.read(), None
        except Exception as exc:
            return None, "cannot read stdin: %s" % exc
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return None, "cannot read file: %s" % (exc.strerror or exc)
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "file is not valid UTF-8"


def _detect_format(path):
    """Return 'json', 'toml', 'env', or None for unknown types."""
    base = os.path.basename(path)
    low = base.lower()
    if low.startswith(".env") or low.endswith(".env") or ".env." in low:
        return "env"
    ext = os.path.splitext(low)[1]
    if ext == ".json":
        return "json"
    if ext == ".toml":
        return "toml"
    return None


def _expand_all(paths, recursive):
    """Expand files/dirs for the 'all' subcommand. Returns (jobs, issues)."""
    jobs = []
    issues = []

    def add_file(full):
        fmt = _detect_format(full)
        if fmt:
            jobs.append((fmt, full))
        else:
            issues.append(Issue(full, None, None, "A1001", "warning",
                                "unknown file type, skipped "
                                "(use the json, toml, or env subcommand)"))

    for path in paths:
        if os.path.isdir(path):
            if recursive:
                for root, _dirs, files in os.walk(path):
                    for name in sorted(files):
                        full = os.path.join(root, name)
                        if os.path.isfile(full):
                            add_file(full)
            else:
                for name in sorted(os.listdir(path)):
                    full = os.path.join(path, name)
                    if os.path.isfile(full):
                        add_file(full)
        elif os.path.isfile(path):
            add_file(path)
        else:
            issues.append(Issue(path, None, None, "C1001", "error",
                                "no such file or directory"))
    return jobs, issues


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _color_enabled(args):
    if args.no_color:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def _format_plain(issue, color):
    loc = issue.file
    if issue.line is not None:
        loc += ":%d" % issue.line
        if issue.col is not None:
            loc += ":%d" % issue.col
    code = "[%s]" % issue.code
    if color:
        paint = _RED if issue.severity == "error" else _YELLOW
        code = paint + code + _RESET
    return "%s: %s: %s %s" % (loc, issue.severity, code, issue.message)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="cfg-lint",
        description="Validate JSON, TOML, and .env config files. "
                    "Exit 0 = no errors, 1 = issues found, 2 = bad arguments.")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON output instead of plain text")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output (also honors NO_COLOR)")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures (exit 1)")
    parser.add_argument("-V", "--version", action="version",
                        version="cfg-lint %s" % __version__)
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p_json = sub.add_parser("json", help="validate JSON files")
    p_json.add_argument("files", nargs="*", metavar="FILE",
                        help="files to check; reads stdin when omitted "
                             "('-' also means stdin)")

    p_toml = sub.add_parser("toml", help="validate TOML files")
    p_toml.add_argument("files", nargs="*", metavar="FILE",
                        help="files to check; reads stdin when omitted "
                             "('-' also means stdin)")

    p_env = sub.add_parser("env", help="validate .env files")
    p_env.add_argument("files", nargs="*", metavar="FILE",
                       help="files to check; reads stdin when omitted "
                            "('-' also means stdin)")

    p_all = sub.add_parser("all", help="check files, auto-detecting format "
                                       "by file name (.json, .toml, .env)")
    p_all.add_argument("paths", nargs="+", metavar="PATH",
                       help="files or directories to check")
    p_all.add_argument("-r", "--recursive", action="store_true",
                       help="recurse into directories")
    return parser


_CHECKERS = {
    "json": check_json_text,
    "toml": check_toml_text,
    "env": check_env_text,
}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    color = _color_enabled(args)

    if args.cmd in ("json", "toml", "env"):
        jobs = [(args.cmd, f) for f in (args.files or ["-"])]
        pending = []
    else:
        jobs, pending = _expand_all(args.paths, args.recursive)

    issues = list(pending)
    files_checked = 0
    for fmt, path in jobs:
        label = "<stdin>" if path == "-" else path
        text, err = _read_input(path)
        if err is not None:
            issues.append(Issue(label, None, None, "C1001", "error", err))
            continue
        files_checked += 1
        issues.extend(_CHECKERS[fmt](text, label))

    errors = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")

    if args.json:
        print(json.dumps({
            "tool": "cfg-lint",
            "version": __version__,
            "files_checked": files_checked,
            "issues": [i.to_dict() for i in issues],
            "summary": {"errors": errors, "warnings": warnings},
        }, indent=2))
    else:
        for issue in issues:
            print(_format_plain(issue, color))

    if errors or warnings:
        print("cfg-lint: %d error(s), %d warning(s) in %d file(s)"
              % (errors, warnings, files_checked), file=sys.stderr)
    else:
        print("cfg-lint: no issues in %d file(s)" % files_checked,
              file=sys.stderr)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
