"""The CSP hash tool, and the two ways it could silently be wrong.

If this hash does not match what the browser computes, the theme-resolver script is blocked
and every light-theme reader gets a dark flash on every page load — the exact bug the script
exists to prevent, reintroduced invisibly. So the extraction rules are pinned here.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from app.security.csp import inline_script_hashes
from tests.conftest import FRONTEND, REAL_CSP_HASH

TOOL = Path(__file__).resolve().parent.parent / "tools" / "csp_hash.py"


def test_matches_the_real_index_html() -> None:
    assert inline_script_hashes((FRONTEND / "index.html").read_bytes()) == [REAL_CSP_HASH]


def test_exactly_one_inline_script_today() -> None:
    # A second inline script is a change to the page's script surface and needs its own
    # hash. Better to fail here than to have it blocked in production.
    assert len(inline_script_hashes((FRONTEND / "index.html").read_bytes())) == 1


def test_deferred_src_tags_are_not_hashed() -> None:
    # index.html has 29 <script defer src=...> tags. None is inline.
    html = b'<script defer src="js/app.js?v=29"></script><script>var a=1;</script>'
    assert len(inline_script_hashes(html)) == 1


def test_src_detection_tolerates_spacing() -> None:
    assert inline_script_hashes(b'<script src = "x.js"></script>') == []


TOOL_MODULES = {
    "csp_hash",
    "check_sql_interpolation",
    "check_route_policies",
    "serve_with_headers",
}


def test_app_does_not_import_from_tools() -> None:
    """The dependency runs one way: tools/ imports app/, never the reverse.

    An earlier version of main.py inserted tools/ at sys.path[0] to reach the hashing
    function, which put a non-package directory ahead of the standard library for the whole
    process — a module-shadowing footgun for the sake of one function.

    AST-based, not a text search. The first version of this test matched any line containing
    both "tools" and "import", which flagged four of its own explanatory comments. Same lesson
    as the SQL gate: a check that fires on prose is a check people learn to ignore.
    """
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []

    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno} imports {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in TOOL_MODULES
                ]
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in TOOL_MODULES:
                    offenders.append(f"{path.name}:{node.lineno} imports from {node.module}")
            elif isinstance(node, ast.Call):
                # sys.path.insert(...) / sys.path.append(...)
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"insert", "append"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "path"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "sys"
                ):
                    offenders.append(f"{path.name}:{node.lineno} mutates sys.path")

    assert not offenders, offenders


class TestLineEndingNormalisation:
    """The subtle one.

    The HTML parser replaces CRLF and lone CR with LF before the content is ever hashed for
    CSP. A Windows editor "fixing" the line endings in index.html would therefore change the
    bytes on disk without changing what the browser hashes — so if we hashed the raw bytes,
    the page would break with no visible edit.
    """

    def test_crlf_and_lf_agree(self) -> None:
        lf = b"<script>\nvar a = 1;\n</script>"
        crlf = b"<script>\r\nvar a = 1;\r\n</script>"
        assert inline_script_hashes(lf) == inline_script_hashes(crlf)

    def test_lone_cr_agrees_too(self) -> None:
        lf = b"<script>\nvar a = 1;\n</script>"
        cr = b"<script>\rvar a = 1;\r</script>"
        assert inline_script_hashes(lf) == inline_script_hashes(cr)

    def test_index_html_is_currently_lf_only(self) -> None:
        # Not a requirement, just a fact worth noticing if it changes.
        assert b"\r" not in (FRONTEND / "index.html").read_bytes()


class TestCliGate:
    def test_check_passes_on_the_real_hash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(TOOL), str(FRONTEND / "index.html"), "--check", REAL_CSP_HASH],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_check_fails_on_a_stale_hash(self) -> None:
        stale = "sha256-" + "A" * 43 + "="
        result = subprocess.run(
            [sys.executable, str(TOOL), str(FRONTEND / "index.html"), "--check", stale],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "MISMATCH" in result.stderr

    def test_missing_file_is_an_error_not_a_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, str(TOOL), "nope.html", "--check", REAL_CSP_HASH],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2


def test_app_refuses_to_boot_on_a_stale_hash() -> None:
    """The runtime half of the guard, beyond the CI gate."""
    from app.config import ConfigError
    from app.main import create_app
    from tests.conftest import make_settings

    stale = "sha256-" + "B" * 43 + "="
    with pytest.raises(ConfigError, match="does not match"):
        create_app(make_settings(csp_inline_script_sha256=stale))
