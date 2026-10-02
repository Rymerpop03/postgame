"""The lock files, checked as they will be read on Linux.

The development machine is Windows and everything that installs from these files — CI and the
Docker image — is Linux. The first CI run failed on exactly that gap: the lock had been written
by evaluating platform markers against Windows, so it carried a bare `colorama==…` line and
could never match what a Linux walk of the same dependency graph produces. These tests read
the committed files the way pip on Linux will, without needing a Linux machine.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

API = Path(__file__).resolve().parent.parent
RUNTIME = API / "requirements.lock"
DEV = API / "requirements-dev.lock"

LINUX = {
    **{key: str(value) for key, value in default_environment().items()},
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "extra": "",
}


def _requirements(path: Path) -> list[Requirement]:
    return [
        Requirement(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _installed_on_linux(path: Path) -> dict[str, str]:
    """What `pip install --no-deps -r <path>` installs on the Linux image."""
    return {
        r.name: str(r.specifier).removeprefix("==")
        for r in _requirements(path)
        if r.marker is None or r.marker.evaluate(LINUX)
    }


class TestWhatShipsOnLinux:
    def test_windows_only_packages_do_not_reach_the_image(self) -> None:
        shipped = _installed_on_linux(RUNTIME)
        assert "colorama" not in shipped
        assert "tzdata" not in shipped

    def test_the_database_driver_does(self) -> None:
        """`psycopg-binary` is pulled in by the `[binary]` extra, and its marker includes
        `extra == "binary"`. Written out verbatim, that clause evaluates false in a
        requirements file and pip skips the line — an image that builds and then cannot open a
        database connection."""
        shipped = _installed_on_linux(RUNTIME)
        assert "psycopg-binary" in shipped
        assert "psycopg" in shipped

    def test_no_line_carries_an_extra_clause(self) -> None:
        for path in (RUNTIME, DEV):
            for requirement in _requirements(path):
                marker = str(requirement.marker or "")
                assert "extra" not in marker, (
                    f"{path.name}: {requirement} would be skipped by pip"
                )

    @pytest.mark.parametrize(
        "package", ["httptools", "uvloop", "watchfiles", "websockets", "pyyaml"]
    )
    def test_uvicorns_standard_extra_is_gone(self, package: str) -> None:
        """Dropped after the first CI run. Nothing used them, and httptools in particular is
        the parser that ignores the 16 KB header cap app/server.py relies on."""
        assert package not in _installed_on_linux(RUNTIME)

    def test_pyproject_does_not_ask_for_it_back(self) -> None:
        data = tomllib.loads((API / "pyproject.toml").read_text(encoding="utf-8"))
        uvicorn = [d for d in data["project"]["dependencies"] if d.startswith("uvicorn")]
        assert uvicorn and "[" not in uvicorn[0], uvicorn

    def test_every_direct_dependency_is_pinned(self) -> None:
        data = tomllib.loads((API / "pyproject.toml").read_text(encoding="utf-8"))
        shipped = {name.lower() for name in _installed_on_linux(RUNTIME)}
        for line in data["project"]["dependencies"]:
            assert Requirement(line).name.lower() in shipped, line

    def test_runtime_and_dev_do_not_overlap(self) -> None:
        """The image installs requirements.lock alone. A package in both files would be
        pinned twice, and the two pins could disagree."""
        runtime = {r.name for r in _requirements(RUNTIME)}
        dev = {r.name for r in _requirements(DEV)}
        assert not runtime & dev, runtime & dev


class TestKnownBadReleases:
    def test_the_yanked_mako_is_not_pinned(self) -> None:
        """Mako 1.4.0 was yanked for installing a stray top-level `tools` package. It was in
        the first lock, and it is why `from tools import check_login_timing` once imported
        somebody else's code — tests/test_auth_timing.py loads the gate by path because of
        it. CI now refuses any yanked pin at install time; this names the one we had."""
        pins = {r.name.lower(): str(r.specifier) for r in _requirements(RUNTIME)}
        assert pins.get("mako") != "==1.4.0"

    def test_urllib3_is_past_the_2026_advisories(self) -> None:
        """PYSEC-2026-4175, -4176 and -4177, all fixed in 2.8.0. Found by the first audit
        that actually reached the dependencies."""
        from packaging.version import Version

        pins = {r.name.lower(): str(r.specifier) for r in _requirements(DEV)}
        assert Version(pins["urllib3"].removeprefix("==")) >= Version("2.8.0")
