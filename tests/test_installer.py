"""The one-line installer.

It runs on somebody else's server, as a shell they piped a URL into, and its
failures land in a place nobody is watching. Nothing here executes it - these
are the properties that can be checked by reading, which is more than was being
checked before.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "docs" / "install.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_it_is_published_where_the_documentation_says() -> None:
    """MkDocs copies static files out of docs/, so its location in the tree is
    its URL. Moving it silently breaks the command in the README.
    """
    assert INSTALLER.is_file()
    assert "install.sh" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="no POSIX sh on the runner")
def test_it_parses_as_posix_sh() -> None:
    """`curl | sh` runs it under whatever /bin/sh the host has. A bashism is a
    syntax error on Debian, where /bin/sh is dash.
    """
    shell = shutil.which("sh")
    assert shell is not None, "no POSIX sh to check against"

    result = subprocess.run(  # noqa: S603 - fixed argv, resolved path
        [shell, "-n", str(INSTALLER)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_it_stops_on_an_error(script: str) -> None:
    """Without `set -e` a failed clone carries on to `docker compose up` in a
    directory that is not there, and reports success.
    """
    assert re.search(r"^set -eu$", script, re.MULTILINE)


def test_it_never_overwrites_an_env_file(script: str) -> None:
    """The one that matters most on a rerun. Regenerating SECRET_KEY would lock
    every project out of its stored AWS credentials while looking like it
    worked.
    """
    assert "if [ -f .env ]; then" in script
    assert "keeping the existing .env" in script


def test_the_env_file_is_not_world_readable(script: str) -> None:
    assert "chmod 600 .env" in script


def test_it_installs_nothing_on_its_own_initiative(script: str) -> None:
    """A script piped into a shell must not install packages, open ports or
    edit a system on a whim. Missing prerequisites are named, and it stops.
    """
    for forbidden in ("apt-get install", "yum install", "ufw ", "systemctl enable"):
        assert forbidden not in script, forbidden


def test_it_can_be_pinned_to_a_version(script: str) -> None:
    """The same URL serves everybody, so without pinning a bad commit on main
    reaches every new install within minutes.
    """
    assert "SESKIT_VERSION" in script
    assert "--branch" in script


def test_an_unreachable_release_api_is_not_fatal(script: str) -> None:
    """No release published yet, or no route to the API. Refusing to install
    over a failed version lookup would be worse than saying what it installed.
    """
    assert 'VERSION="main"' in script
    assert "no published release found" in script


def test_it_reads_the_tag_without_a_json_parser(script: str) -> None:
    """jq is not on a fresh box, and requiring it would mean installing
    something - which this script does not do.
    """
    assert "jq" not in script
    assert "tag_name" in script


def test_the_public_url_is_set_when_it_can_be_guessed(script: str) -> None:
    """Unset, every message goes out with no unsubscribe header at all - a
    silent omission that costs sender reputation and that nothing reports.
    """
    assert "PUBLIC_BASE_URL" in script
    assert "SESKIT_PUBLIC_URL" in script


def test_it_points_at_the_access_key_guide(script: str) -> None:
    """Installing is not the end. The next thing anybody has to do is get an
    AWS key, and the script is the last place they are looking.
    """
    assert "get-an-access-key" in script
