"""Windows portability guards for shipped code.

A Windows machine gives Python two legacy defaults that a Linux or macOS
runner never has:

* the console defaults to a code page (cp1252 on most installs, cp932 on a
  Japanese one) — handled centrally by ``app.runtime.ensure_utf8_output``;
* **file** I/O defaults to that same code page, and nothing handles it unless
  the call site names an encoding.

The second one is the dangerous half, because it fails on the user's machine
and nowhere else. Two failures of exactly this shape reached CI:

* ``live-digest --out`` wrote a digest containing ``→`` with no encoding, so
  on Windows the command exited 1 and left a zero-byte file;
* the daily ``upstox-login`` refresh read the operator's ``.env`` with no
  encoding, so one non-ASCII comment in it aborted the token save.

Both are covered below. The source scan keeps the whole class from coming
back; the subprocess tests prove the real commands work under a legacy code
page, which is what a Windows user actually has. They are cheap: no network,
no credentials, and a throwaway ARENA_HOME.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Everything that ends up inside the packaged executable or is run from a
# source checkout by a user. tools/ and tests/ are excluded deliberately: they
# never run on the operator's machine, and a scan that covers them would be
# noise rather than a guard.
SHIPPED_ROOTS = ("app", "run.py", "arena.py")

TEXT_MODE_MARKERS = ("w", "wt", "a", "at", "r", "rt", "r+", "w+", "x")
BINARY_MODE_MARKERS = ("b", "rb", "wb", "ab", "r+b", "w+b", "xb")

# `<something>.open(...)` is not always a file. These receivers are the ones
# that exist in this tree and mean something else entirely; a new one has to
# be added here consciously rather than silently weakening the scan.
NON_FILE_OPEN_RECEIVERS = {"webbrowser"}


def _shipped_python_files() -> list[Path]:
    files: list[Path] = []
    for entry in SHIPPED_ROOTS:
        path = ROOT / entry
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py")))
        else:
            files.append(path)
    return files


def _literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_text_write_or_read(node: ast.Call) -> str | None:
    """Describe a text-mode file call that must name an encoding, or None.

    Only calls whose default encoding follows the operating system are
    returned: ``open``/``Path.open``/``gzip.open`` in a text mode,
    ``Path.read_text``/``Path.write_text``. Binary calls are ignored — they
    have no encoding to get wrong.
    """
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name in ("read_text", "write_text"):
        return name
    if name != "open":
        return None
    if isinstance(func, ast.Attribute):
        receiver = func.value
        receiver_name = (receiver.id if isinstance(receiver, ast.Name)
                         else getattr(receiver, "attr", ""))
        if receiver_name in NON_FILE_OPEN_RECEIVERS:
            return None
    mode = None
    if len(node.args) >= 2:
        mode = _literal_string(node.args[1])
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = _literal_string(keyword.value)
    mode = mode or "r"
    if any(marker in mode for marker in BINARY_MODE_MARKERS):
        return None
    if any(marker in mode for marker in TEXT_MODE_MARKERS):
        return f"open({mode!r})"
    return None


def test_shipped_text_io_always_names_an_encoding():
    """No shipped file read or write may rely on the OS default encoding.

    A missing encoding is invisible on Linux (UTF-8 everywhere) and fatal on
    Windows the first time a character outside the code page appears — a
    dashboard arrow, an em dash in a report, an operator's own comment.
    """
    findings: list[str] = []
    for path in _shipped_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            mode = _is_text_write_or_read(node)
            if mode is None:
                continue
            if any(keyword.arg == "encoding" for keyword in node.keywords):
                continue
            relative = path.relative_to(ROOT)
            findings.append(f"{relative}:{node.lineno} {mode} without encoding=")
    assert not findings, (
        "these calls would use the Windows code page for file I/O; pass "
        'encoding="utf-8" explicitly:\n  ' + "\n  ".join(findings)
    )


def _legacy_code_page_env(home: Path, **extra: str) -> dict[str, str]:
    """An environment whose default file encoding is NOT UTF-8.

    ``PYTHONCOERCECLOCALE``/``PYTHONUTF8`` are switched off so the C locale
    stays the C locale instead of being coerced to UTF-8, which is what makes
    this a faithful stand-in for a Windows code page.
    """
    env = dict(os.environ)
    env.update({
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "",
        "ARENA_HOME": str(home),
        "PYTHONPATH": str(ROOT),
    })
    env.pop("DATABASE_URL", None)
    env.update(extra)
    return env


def _run(argv: list[str], home: Path, **extra: str) -> subprocess.CompletedProcess:
    # errors="replace": the child's console output is UTF-8 (ensure_utf8_output)
    # while the parent decodes with its own locale code page, and a byte that
    # is undefined in that code page must not turn into a decode crash inside
    # the test itself.
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(ROOT),
        env=_legacy_code_page_env(home, **extra),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=180,
    )


def _assert_legacy_code_page_is_in_force(home: Path) -> None:
    probe = _run(["-c", "import locale; print(locale.getpreferredencoding(False))"], home)
    encoding = probe.stdout.strip()
    if encoding.lower().replace("-", "") in ("utf8", "utf_8"):
        pytest.skip(f"this interpreter cannot be forced off UTF-8 (got {encoding!r})")


def test_the_live_digest_file_is_written_under_a_legacy_code_page(tmp_path):
    """`live-digest --out` must produce a readable file on a code-page system.

    Before the fix this raised UnicodeEncodeError on the ``→`` in the window
    line: exit 1, an empty file, and no digest — which is precisely what the
    Windows CI run recorded.
    """
    home = tmp_path / "home"
    home.mkdir()
    _assert_legacy_code_page_is_in_force(home)

    target = tmp_path / "digest.md"
    result = _run(
        ["-m", "app", "live-digest", "--markdown", "--out", str(target)], home)
    assert result.returncode == 0, result.stderr[-2000:]

    content = target.read_text(encoding="utf-8")
    assert "LIVE OPS DIGEST" in content
    # The character that used to break it is still there, and is still one
    # character: the digest is written as UTF-8, not transliterated.
    assert "→" in content


def test_the_operator_env_survives_a_legacy_code_page(tmp_path):
    """The daily token refresh must not die on a non-ASCII .env.

    The operator owns this file and may have written anything in it. Reading
    or rewriting it with the code page raised UnicodeDecodeError on Windows
    (and would have corrupted the file on the write path).
    """
    home = tmp_path / "home"
    home.mkdir()
    _assert_legacy_code_page_is_in_force(home)

    env_file = tmp_path / ".env"
    original = "# Notes — rupees ₹1000 limit\nAPI_TOKEN=local\n"
    env_file.write_text(original, encoding="utf-8")

    script = (
        "import sys;"
        "from pathlib import Path;"
        "from app.data.upstox_auth import store_token_in_env;"
        "print(store_token_in_env('issued-token', Path(sys.argv[1])))"
    )
    result = _run(["-c", script, str(env_file)], home)
    assert result.returncode == 0, result.stderr[-2000:]

    content = env_file.read_text(encoding="utf-8")
    # The operator's own line survives byte-for-byte, the token is added, and
    # the secret is not echoed to the terminal.
    assert "# Notes — rupees ₹1000 limit" in content
    assert "API_TOKEN=local" in content
    assert "UPSTOX_ACCESS_TOKEN=issued-token" in content
    assert "issued-token" not in result.stdout


def test_a_frozen_startup_failure_waits_instead_of_vanishing():
    """A double-clicked .exe must not disappear before it can say why.

    Windows closes the console the instant the process exits, so without this
    a startup failure is a window that flashes and is gone — the most common
    way a user concludes the executable is broken, with no message to report.
    """
    import arena

    should_hold = arena._should_hold_console

    # The case this exists for: a frozen build, a real terminal, no subcommand.
    assert should_hold([], frozen=True, attached=True) is True
    assert should_hold(["--port", "9000"], frozen=True, attached=True) is True

    # A piped run (CI, the packaged smoke test) must never wait for input...
    assert should_hold([], frozen=True, attached=False) is False
    # ...a source checkout is a developer who has the traceback anyway...
    assert should_hold([], frozen=False, attached=True) is False
    # ...and these are scriptable commands whose exit code callers read.
    for argv in (["--cli", "live-status"], ["--run", "paper"],
                 ["--diagnostics"]):
        assert should_hold(argv, frozen=True, attached=True) is False


def test_a_saved_setup_file_reloads_under_a_legacy_code_page(tmp_path):
    """The credential screen's .env writer is UTF-8 on both sides.

    The setup page is the first thing a Windows user touches, so a code-page
    default here would break onboarding rather than a later feature.
    """
    home = tmp_path / "home"
    home.mkdir()
    _assert_legacy_code_page_is_in_force(home)

    script = (
        "import json, sys;"
        "from pathlib import Path;"
        "from app.config.envfile import read_values, update_values;"
        "path = Path(sys.argv[1]);"
        # The escape keeps this argv ASCII; the value it writes is not.
        "update_values(path, {'API_TOKEN': 't-\\u2014-token',"
        " 'BROKER': 'LOCAL_PAPER'});"
        "print(json.dumps(read_values(path)))"
    )
    env_file = tmp_path / ".env"
    result = _run(["-c", script, str(env_file)], home)
    assert result.returncode == 0, result.stderr[-2000:]
    values = json.loads(result.stdout.strip().splitlines()[-1])
    assert values["API_TOKEN"] == "t-\u2014-token"
    assert values["BROKER"] == "LOCAL_PAPER"
