# cfg-lint

> Validate JSON, TOML, and .env config files from the CLI. Daily Tools #7.

## Why

Misconfigured files are a classic "works on my machine" bug: a duplicated JSON
key silently shadows the first value, a TOML table typo breaks a deploy, a
stray space in `.env` behaves differently across parsers. The closest tools are
single-format — `jsonlinter` only handles JSON, and `dotenv-linter` only
handles `.env` and needs a compiled Rust binary. `cfg-lint` covers JSON, TOML,
and `.env` in one zero-dependency Python script with git-style subcommands,
CI-friendly exit codes, and machine-readable `--json` output.

## Install

```bash
git clone https://github.com/Kayzenn123/cfg-lint.git
cd cfg-lint
# no dependencies — just run it
python3 cfg_lint.py --help
# optional: install the `cfg-lint` command
pip install .
```

Requires: Python >= 3.11 (uses stdlib `tomllib`).

## Usage

```bash
cfg-lint json [FILE ...]              # validate JSON files (stdin if omitted)
cfg-lint toml [FILE ...]              # validate TOML files
cfg-lint env [FILE ...]               # validate .env files
cfg-lint all [-r] PATH ...            # auto-detect format by file name
```

Each subcommand has its own help: `cfg-lint json --help`.

| Flag | Function | Default |
| --- | --- | --- |
| `--json` | machine-readable JSON report on stdout | plain text |
| `--strict` | warnings also fail (exit 1) | warnings pass |
| `--no-color` | disable colors (also honors `NO_COLOR`) | color when TTY |
| `-V, --version` | print version | — |

Exit codes: `0` no errors · `1` issues found or unreadable file · `2` bad arguments.

## Example output

Command:

```bash
python3 cfg_lint.py all examples/
```

Output (real, copied from an actual run):

```text
examples/deploy.env:5: error: [E2001] duplicate key 'DATABASE_URL' (first defined on line 4)
examples/deploy.env:7: warning: [W2003] whitespace around '=' is not portable across parsers
examples/service.toml:5:16: error: [T1001] invalid TOML: Expected ']' at the end of a table declaration (at line 5, column 16)
examples/settings.json:6:5: error: [J1002] duplicate key 'port' at $.server.port (earlier value is shadowed)
```

Machine output for CI:

```bash
python3 cfg_lint.py --json all examples/ | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['summary'])"
```

```text
{'errors': 3, 'warnings': 1}
```

Piping through stdin also works:

```bash
printf 'PORT=8080\nport=9090\n' | python3 cfg_lint.py env
```

```text
<stdin>:2: warning: [W2008] key 'port' is not uppercase (convention)
```

## How it works

- **JSON**: parsed with stdlib `json`; duplicate keys at any nesting level are
  found by a small tokenizer that records exact line/column and a JSONPath-like
  location (`$.server.port`, `$.items[0].id`). A UTF-8 BOM is a warning, not an error.
- **TOML**: parsed with stdlib `tomllib`; syntax errors (including duplicate
  keys, which TOML forbids) are reported with the line/column from the parser.
- **.env**: a rule engine checks duplicate keys, invalid key names, lines that
  are not `KEY=VALUE`, unbalanced quotes (errors), plus whitespace around `=`,
  empty values, missing trailing newline, and lowercase keys (warnings).
- `all` dispatches by file name (`.json`, `.toml`, `.env*`) with `-r` for recursion.
- Issues go to stdout, the summary to stderr; `--json` emits one JSON report.

## Limitations

- JSON duplicate detection reports the second and later occurrences; the exact
  column of a key inside a multi-line string key is approximate.
- TOML validation needs Python 3.11+ (`tomllib`); on older Pythons the `toml`
  subcommand reports an error instead of validating.
- `.env` inline comments (`KEY=value # comment`) are treated as part of the
  value — strip them yourself if your parser ignores them.
- No schema validation (e.g. "port must be an integer"); this tool checks
  syntax and common mistakes only.

## Development

```bash
python3 -m unittest discover -s test -q
```

32 tests, all green on two consecutive runs.

## License

MIT

---
Ringkasan (Bahasa Indonesia): cfg-lint memvalidasi file konfigurasi JSON, TOML, dan .env langsung dari terminal — menemukan duplikat key JSON, syntax error TOML, dan 8 aturan umum file .env. Cocok dipakai sebagai gate di CI berkat exit code yang bermakna (0/1/2) dan output `--json` yang bisa di-parse mesin. Tanpa dependensi, cukup Python 3.11+. Batasan utama: tidak ada validasi skema (tipe data/nilai), dan validasi TOML butuh Python 3.11+.
