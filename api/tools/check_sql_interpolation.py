"""CI gate: fail on string interpolation anywhere near SQL.

BACKEND-PLAN.md cross-cutting rule: "Parameterized queries only. No f-strings, `%`, `+`,
or `.format()` anywhere near SQL. CI greps for it."

This is an AST check rather than a grep, because a grep for `f"` produces enough noise
that people learn to ignore it, and a gate people ignore is not a gate.

Two checks:

  1. An interpolated expression passed as the query argument to an executing call
     (`execute`, `executemany`, `scalar`, `text`, ...).
  2. Any interpolated string whose literal parts look like SQL, wherever it appears. This
     catches the common shape where a query is assembled into a variable first and passed
     somewhere innocuous-looking later.

Deliberately allowed: interpolating a value that came from a hardcoded whitelist, which is
the only correct way to vary an `ORDER BY`. Mark those with `# sql-safe:` on the same line
and the reason becomes part of the diff instead of an argument in review.

Usage:
    python tools/check_sql_interpolation.py app tools
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

QUERY_CALLS = frozenset(
    {
        "execute",
        "executemany",
        "executescript",
        "scalar",
        "scalars",
        "fetch",
        "fetchrow",
        "fetchval",
        "text",
        "raw",
    }
)

# Markers that only appear in SQL. Any one of these is enough.
SQL_STRONG = (
    "insert into",
    "delete from",
    "create table",
    "alter table",
    "drop table",
    "truncate table",
    "on conflict",
)

# Markers that also occur in ordinary English — "update the value", "order by popularity"
# in a comment, "set the flag". One is not evidence; two together is a query.
#
# This distinction was not academic: the first version of this gate flagged its own help
# text and csp_hash.py's error messages. A gate that cries wolf gets ignored, which is
# worse than not having it.
SQL_WEAK = (
    "select ",
    " from ",
    " where ",
    "order by",
    "group by",
    " join ",
    "update ",
    " set ",
    "returning ",
    "values (",
    " limit ",
    " offset ",
)

ESCAPE_HATCH = "# sql-safe:"

# Calls whose arguments are prose for a human, not a query. Skipping their contents is what
# lets the weak-marker heuristic stay sensitive: help text and error messages are exactly
# where English sentences containing "update" and "from" legitimately live.
MESSAGE_CALLS = frozenset(
    {"print", "warn", "debug", "info", "warning", "error", "exception", "critical", "log"}
)


class Finding(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.problems: list[tuple[int, str]] = []
        self._in_message: set[int] = set()

    def _mark_message_subtree(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            self._in_message.add(id(child))

    @staticmethod
    def _call_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
        return None

    @classmethod
    def _is_message_call(cls, node: ast.Call) -> bool:
        name = cls._call_name(node)
        if name is None:
            return False
        if name in MESSAGE_CALLS:
            return True
        # Exception constructors: raise ValueError(f"could not update ... from ...")
        return name.endswith(("Error", "Exception"))

    # -- helpers ---------------------------------------------------------------------

    def _exempt(self, node: ast.AST) -> bool:
        """Is this node annotated as reviewed-and-safe?

        The annotation is accepted on the node's own line *or* the line directly above it.
        The line above matters for multi-line f-strings: there is nowhere to put a trailing
        comment on the opening `f\"\"\"` line, because anything after the quotes lands *inside*
        the string and would be sent to the database as part of the query. That mistake was
        made once here. Allowing the preceding line is the fix, and it reads better anyway.
        """
        line = getattr(node, "lineno", 0)
        for candidate in (line, line - 1):
            if 1 <= candidate <= len(self.lines) and ESCAPE_HATCH in self.lines[candidate - 1]:
                return True
        return False

    @staticmethod
    def _is_interpolated(node: ast.AST) -> str | None:
        """Return a description if `node` builds a string dynamically."""
        if isinstance(node, ast.JoinedStr):
            return "f-string"
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                return "%-formatting"
            if isinstance(node.op, ast.Add):
                return "concatenation"
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"format", "join"}:
                return f".{func.attr}()"
        return None

    @staticmethod
    def _literal_parts(node: ast.AST) -> str:
        """The static text of a dynamically built string, lower-cased."""
        parts: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                parts.append(child.value)
        return " ".join(parts).lower()

    def _looks_like_sql(self, node: ast.AST) -> bool:
        text = self._literal_parts(node)
        if any(word in text for word in SQL_STRONG):
            return True
        return sum(word in text for word in SQL_WEAK) >= 2

    # -- visitors --------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node)

        # A query argument is still checked even inside a message call, because
        # log.info(conn.execute(f"...")) is not something we want to wave through.
        if self._is_message_call(node):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if not (isinstance(arg, ast.Call) and self._call_name(arg) in QUERY_CALLS):
                    self._mark_message_subtree(arg)

        if name in QUERY_CALLS and node.args:
            first = node.args[0]
            how = self._is_interpolated(first)
            if how and not self._exempt(first) and not self._exempt(node):
                self.problems.append(
                    (
                        node.lineno,
                        f"{how} passed as the query argument to {name}(). "
                        "Use a bound parameter.",
                    )
                )

        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        if (
            id(node) not in self._in_message
            and self._looks_like_sql(node)
            and not self._exempt(node)
        ):
            self.problems.append((node.lineno, "f-string containing SQL keywords"))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if (
            isinstance(node.op, (ast.Mod, ast.Add))
            and id(node) not in self._in_message
            and self._looks_like_sql(node)
            and not self._exempt(node)
        ):
            how = "%-formatting" if isinstance(node.op, ast.Mod) else "concatenation"
            self.problems.append((node.lineno, f"{how} building SQL"))
        self.generic_visit(node)


def scan(path: Path) -> list[tuple[Path, int, str]]:
    out: list[tuple[Path, int, str]] = []
    for file in sorted(path.rglob("*.py")):
        source = file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(file))
        except SyntaxError as exc:
            out.append((file, exc.lineno or 0, f"could not parse: {exc.msg}"))
            continue
        visitor = Finding(file, source)
        visitor.visit(tree)
        # Nested BinOps ("a" + x + "b" is two of them) report the same line twice.
        out.extend((file, line, msg) for line, msg in dict.fromkeys(visitor.problems))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args()

    findings: list[tuple[Path, int, str]] = []
    for path in args.paths:
        if path.is_file():
            source = path.read_text(encoding="utf-8")
            visitor = Finding(path, source)
            visitor.visit(ast.parse(source, filename=str(path)))
            findings.extend((path, line, msg) for line, msg in dict.fromkeys(visitor.problems))
        elif path.is_dir():
            findings.extend(scan(path))
        else:
            print(f"check_sql_interpolation: no such path: {path}", file=sys.stderr)
            return 2

    if not findings:
        print("check_sql_interpolation: ok")
        return 0

    print(f"check_sql_interpolation: {len(findings)} problem(s)\n", file=sys.stderr)
    for file, line, msg in findings:
        print(f"  {file}:{line}: {msg}", file=sys.stderr)
    print(
        f"\nIf an interpolation is genuinely safe — a value from a hardcoded whitelist, "
        f"which is the only correct way to vary an ORDER BY — annotate that line with "
        f"'{ESCAPE_HATCH} <reason>'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
