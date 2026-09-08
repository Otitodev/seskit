"""The two Dockerfiles.

`docker/Dockerfile` and `docker/Dockerfile.worker` are the same build with a
different last line. Nothing enforced that, and nothing checked that either
would build on the builder a deployment platform actually uses — which is how
both of these went wrong at once:

- A `RUN --mount=type=cache` is BuildKit-only. A builder without BuildKit does
  not ignore it; it stops with "the --mount option requires BuildKit". CI
  builds with buildx, so CI was never going to notice, and the failure arrived
  during a deployment instead.
- CI built the API image and not the worker's, so the worker's copy could have
  drifted arbitrarily far without anything saying so.

Both checks are text over the files rather than real builds. That is the point:
they run in milliseconds inside the ordinary suite, and the thing being checked
is a property of the source, not of the daemon that happens to be installed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "docker" / "Dockerfile"
WORKER = ROOT / "docker" / "Dockerfile.worker"

#: Flags the classic builder rejects outright. `COPY --from=` is deliberately
#: absent - multi-stage copies, including from an external image, work on both.
BUILDKIT_ONLY = re.compile(
    r"^\s*(?:RUN|COPY|ADD)\s+.*?--(?P<flag>mount|link|network|security)[=\s]",
    re.MULTILINE,
)

#: How the image starts, which is the one thing the two files may disagree on.
ENTRYPOINT_INSTRUCTIONS = {"CMD", "ENTRYPOINT", "EXPOSE"}


def _instructions(path: Path) -> list[str]:
    """The Dockerfile with comments, blank lines and line continuations gone.

    Comparing raw text would fail on the header comment, which is *supposed* to
    differ - each file explains why it exists. What has to match is the build.
    """
    text = path.read_text(encoding="utf-8")
    # Join continuations first, so a wrapped RUN is one instruction.
    text = re.sub(r"\\\r?\n\s*", " ", text)

    instructions = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instructions.append(" ".join(stripped.split()))
    return instructions


def _build_only(path: Path) -> list[str]:
    """Everything except the trailing instructions that say how to start."""
    instructions = _instructions(path)
    while instructions and instructions[-1].split(maxsplit=1)[0] in ENTRYPOINT_INSTRUCTIONS:
        instructions.pop()
    return instructions


# --------------------------------------------------------------- buildkit ---


@pytest.mark.parametrize("path", [API, WORKER], ids=lambda p: p.name)
def test_no_instruction_needs_buildkit(path: Path) -> None:
    """The bug that reached a deployment.

    A cache mount saves re-downloading wheels when the dependency layer is
    invalidated, which is worth very little here - the manifests are copied on
    their own, so an ordinary source edit never reaches that layer - and it
    costs the ability to build at all on a classic builder. Some platforms
    build that way and offer no switch.
    """
    found = [match.group("flag") for match in BUILDKIT_ONLY.finditer(path.read_text("utf-8"))]

    assert not found, (
        f"{path.name} uses BuildKit-only flags {sorted(set(found))}. "
        "A builder without BuildKit fails rather than ignoring them."
    )


def test_the_guard_would_catch_a_cache_mount() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    assert BUILDKIT_ONLY.search("RUN --mount=type=cache,target=/x uv sync")
    assert BUILDKIT_ONLY.search("COPY --link a b")
    # The one that must keep working: it is not BuildKit-only.
    assert not BUILDKIT_ONLY.search("COPY --from=builder /app /app")
    assert not BUILDKIT_ONLY.search("RUN uv sync --frozen --no-dev")


# ---------------------------------------------------------------- mirrored ---


def test_the_two_dockerfiles_build_the_same_image() -> None:
    """They differ by how the image starts and by nothing else.

    Stated as a test because the alternative was a comment asking people to
    remember. A dependency added to one and not the other produces a worker
    that builds cleanly and dies on import, in production, at the first job.
    """
    api, worker = _build_only(API), _build_only(WORKER)

    assert api == worker, (
        "docker/Dockerfile and docker/Dockerfile.worker have diverged. "
        "They may differ only in CMD/ENTRYPOINT/EXPOSE.\n"
        f"  only in Dockerfile:        {[i for i in api if i not in worker]}\n"
        f"  only in Dockerfile.worker: {[i for i in worker if i not in api]}"
    )


def test_each_starts_the_process_it_is_for() -> None:
    """The one difference, asserted rather than assumed - a copy-paste that
    left the worker running uvicorn would pass every other test here.
    """
    assert "uvicorn" in _instructions(API)[-1]
    assert "arq" in _instructions(WORKER)[-1]


def test_only_the_api_publishes_a_port() -> None:
    """Nothing connects to the worker; it only makes outbound connections. An
    EXPOSE on it would suggest otherwise to whoever deploys it.
    """
    assert any(line.startswith("EXPOSE") for line in _instructions(API))
    assert not any(line.startswith("EXPOSE") for line in _instructions(WORKER))


# ------------------------------------------------------------------ compose ---


def _compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml", reason="pyyaml arrives with the docs group")
    loaded: dict[str, Any] = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_stack_applies_its_own_migrations() -> None:
    """`docker compose up` on a clean machine has to produce a working
    instance.

    Before this existed it produced one with no schema: every documented way to
    migrate was `uv run alembic upgrade head`, which needs uv, a checkout and
    the dev dependency group - none of which a server following the quickstart
    has, and alembic was not in the image either.
    """
    services = _compose()["services"]

    assert "migrate" in services
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]


@pytest.mark.parametrize("service", ["api", "worker"])
def test_nothing_starts_before_the_schema_exists(service: str) -> None:
    """Waiting for it to *complete*, not merely to start. A migration that
    fails must stop the stack rather than leaving an application to discover
    the missing column on its first query.
    """
    depends = _compose()["services"][service]["depends_on"]

    assert depends["migrate"]["condition"] == "service_completed_successfully"


def test_the_image_can_run_alembic() -> None:
    """The migrate service uses the shipped image, so alembic has to be a
    runtime dependency rather than a development one.
    """
    # Parsed rather than sliced: `uvicorn[standard]` contains a bracket, and a
    # string split on "]" reads the dependency list as ending there.
    manifest = tomllib.loads((ROOT / "apps" / "api" / "pyproject.toml").read_text("utf-8"))
    dependencies = manifest["project"]["dependencies"]

    assert any(name.startswith("alembic") for name in dependencies), dependencies
