"""Guard the `cmd /c "..."` quoting in run.bat.

cmd.exe's /C and /K switches mangle a command line that contains more than two
quote characters. The documented rule:

    1. quote characters are preserved only when there is no /S switch, there
       are exactly two quotes, no special characters between them, ...
    2. otherwise: if the first character is a quote, strip it and remove the
       LAST quote character on the line, keeping any text after it.

Rule 2 is what bit us. The old numpy check was written as

    start /b "" cmd /c "\"%VENVPY%\" -c \"import numpy\" >nul 2>&1 && echo ok> \"venv\\.np_ok\""

which expands to a command line that *ends* with a quote. Rule 2 then stripped
the leading quote and the trailing one, leaving

    C:\\...\\python.exe" -c "import numpy" >nul 2>&1 && echo ok> "venv\\.np_ok

so the interpreter never ran, the marker file was never written, and the
launcher reported "numpy hangs on import" on every single PC — healthy ones
included. The fix is to wrap the whole command in an extra pair of quotes
(``cmd /d /s /c ""...""``) so the stripping removes only the wrapper.

These tests simulate rule 2 so the mistake cannot come back silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUN_BAT = Path(__file__).resolve().parents[2] / "run.bat"

if not RUN_BAT.exists():
    pytest.skip("run.bat is not part of this checkout", allow_module_level=True)

TEXT = RUN_BAT.read_text(encoding="utf-8", errors="replace")

# Batch comments are prose: the examples inside them are not code.
CODE_LINES = [
    line
    for line in TEXT.splitlines()
    if not line.strip().lower().startswith(("rem", "::"))
]

# `cmd ... /c "<payload>"` — payload runs to the last quote on the line.
CMD_PAYLOAD_RE = re.compile(r'cmd\s+(?:/[a-z]\s+)*/([ck])\s+(".*")\s*$')

# Batch variables we can substitute with a plausible Windows value.
SUBSTITUTIONS = {
    "%VENVPY%": r"C:\psd.ai\venv\Scripts\python.exe",
    "%APP_DIR%": r"C:\psd.ai",
    "%ROOT%": "C:\\",
    "%DESKTOP_DIR%": r"C:\psd.ai-desktop",
    "%TOOLS_DIR%": r"C:\.tools",
    "%USERPROFILE%": r"C:\Users\owner",
    "%PSD_AI_RUNTIME_DIR%": r"C:\Users\owner\AppData\Local\psd.ai\runtime",
    "%LLAMA_PORT%": "8080",
    "%MODEL_WAIT_SECONDS%": "2400",
    "%NUMPY_IMPORT_BUDGET%": "90",
    "%~1": "90",
}


def expand(text: str) -> str:
    for needle, value in SUBSTITUTIONS.items():
        text = text.replace(needle, value)
    return text


def cmd_c_strip(command_line: str) -> str:
    """Apply cmd.exe rule 2 (the /C and /K fallback) to a command line."""

    if not command_line.startswith('"'):
        return command_line
    rest = command_line[1:]
    last = rest.rfind('"')
    if last < 0:
        return rest
    return rest[:last] + rest[last + 1 :]


def payloads() -> list[tuple[str, str]]:
    found = []
    for line in CODE_LINES:
        m = CMD_PAYLOAD_RE.search(line.strip())
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def test_we_found_the_subprocess_launches():
    """Sanity check: the patterns we protect are still in the file."""

    assert payloads(), "no `cmd /c` launches found in run.bat"


@pytest.mark.parametrize("switch,payload", payloads())
def test_cmd_payload_survives_cmd_quote_stripping(switch, payload):
    """After cmd's mangling the command must still be correctly quoted."""

    stripped = cmd_c_strip(expand(payload))
    assert stripped.count('"') % 2 == 0, f"unbalanced quotes: {stripped}"
    # Every one of these launches runs an interpreter, so it must start with a
    # fully quoted executable path.
    assert stripped.startswith('"'), f"executable path lost its quoting: {stripped}"
    exe = stripped.split('"')[1]
    assert exe.endswith(".exe"), f"not an executable: {exe}"


def test_numpy_check_runs_the_python_we_expect():
    """The regression itself: the numpy probe must invoke the venv python."""

    hits = [
        cmd_c_strip(expand(p))
        for _switch, p in payloads()
        if "import numpy" in p
    ]
    assert hits, "the numpy import probe is gone from run.bat"
    cmd = hits[0]
    assert cmd.startswith('"C:\\psd.ai\\venv\\Scripts\\python.exe"')
    assert '-c "import numpy"' in cmd


def test_numpy_marker_files_are_still_quoted():
    cmd = next(
        cmd_c_strip(expand(p)) for _s, p in payloads() if "import numpy" in p
    )
    # `.np_ok` / `.np_fail` / `.np_err` are written by the child shell, so
    # their paths must survive intact or the wait loop never sees them.
    for marker in (".np_ok", ".np_fail", ".np_err"):
        assert f'"{marker}"'.replace(".np", "venv\\.np") in cmd, marker


def test_the_broken_single_wrapped_form_is_gone():
    """`cmd /c "` where the payload does NOT start with a doubled quote."""

    offenders = [
        line.strip()
        for line in CODE_LINES
        if re.search(r'cmd\s+/[ck]\s+"(?!")', line)
    ]
    assert not offenders, (
        "cmd /c with a single-wrapped payload mangles the command; "
        f"use cmd /d /s /c \"\"...\"\" instead: {offenders}"
    )


def test_numpy_budget_is_generous_and_configurable():
    """A cold venv can take tens of seconds; 20 was never enough."""

    assert "NUMPY_IMPORT_BUDGET" in TEXT
    assert "90" in TEXT
    # The retry is what distinguishes "slow" from "stuck".
    assert "verify_numpy_again" in TEXT


def test_a_failed_numpy_check_does_not_hard_stop_the_launcher():
    """The old behaviour was exit /b 1 with no way forward."""

    assert "numpy_troubleshoot" in TEXT
    assert "Continue anyway" in TEXT
    assert "--skip-numpy-check" in TEXT
