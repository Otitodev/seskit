"""The documentation site's own invariants.

Documentation fails quietly. A page that stops being listed, a link that points
at a file somebody renamed, an agent index that silently drops half the corpus
- none of it raises, and none of it is noticed by the person who caused it.

`mkdocs build --strict` in CI covers broken internal links. These cover the
things it does not: that the agent-readable index still describes every page,
and that generated files are still generated rather than hand-edited.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"


def _config() -> dict[str, Any]:
    """mkdocs.yml, parsed.

    `yaml.safe_load` is enough here: the file uses no custom tags, and adding
    one would break this loudly rather than silently.
    """
    yaml = pytest.importorskip("yaml", reason="pyyaml arrives with the docs group")
    loaded: dict[str, Any] = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))
    return loaded


def _nav_pages(node: Any, found: set[str] | None = None) -> set[str]:
    """Every markdown file the nav points at, at any nesting depth."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for value in node.values():
            _nav_pages(value, found)
    elif isinstance(node, list):
        for value in node:
            _nav_pages(value, found)
    elif isinstance(node, str) and node.endswith(".md"):
        found.add(node)
    return found


def _llms_sections(config: dict[str, Any]) -> dict[str, list[Any]]:
    for plugin in config.get("plugins", []):
        if isinstance(plugin, dict) and "llmstxt" in plugin:
            sections: dict[str, list[Any]] = plugin["llmstxt"].get("sections", {})
            return sections
    return {}


def _llms_pages(config: dict[str, Any]) -> set[str]:
    """Every page named in the llmstxt sections.

    Entries are either a bare path or a single-key mapping of path to
    description, so both shapes have to be handled.
    """
    pages: set[str] = set()
    for entries in _llms_sections(config).values():
        for entry in entries:
            pages.add(next(iter(entry)) if isinstance(entry, dict) else entry)
    return pages


def test_the_agent_index_describes_every_page() -> None:
    """A page missing from llms.txt is invisible to anything reading it.

    Nothing else catches this: the site builds, the page is reachable, and the
    index simply does not mention it. The failure mode is an agent confidently
    telling someone a feature is undocumented.
    """
    config = _config()
    nav = _nav_pages(config.get("nav", []))
    listed = _llms_pages(config)

    missing = nav - listed
    assert not missing, f"nav pages absent from the llmstxt sections: {sorted(missing)}"


def test_the_agent_index_does_not_describe_pages_that_are_gone() -> None:
    """The other direction. A stale entry points an agent at a 404, which is
    worse than an omission because it looks like the page should exist.
    """
    config = _config()
    listed = _llms_pages(config)

    absent = {page for page in listed if not (ROOT / "docs" / page).exists()}
    assert not absent, f"llmstxt lists files that do not exist: {sorted(absent)}"


def test_every_listed_page_carries_a_description() -> None:
    """The whole value of the index is letting something decide *not* to open a
    page. A bare path gives it nothing to decide on.
    """
    config = _config()

    bare = [
        entry
        for entries in _llms_sections(config).values()
        for entry in entries
        if not isinstance(entry, dict)
    ]
    assert not bare, f"listed without a description: {bare}"


def test_both_probes_are_documented_where_operators_look() -> None:
    """`/readyz` was missing from the prose entirely, and `/healthz` was
    described as the readiness probe in its place.

    That is the worst shape this mistake can take. `/healthz` deliberately
    checks nothing, so an orchestrator pointed at it keeps an instance in
    rotation with its database unreachable - verified against a running stack:
    Postgres stopped, `/healthz` still answered 200 while `/readyz` answered
    503 with `database: false`.

    Naming both here because the failure was an endpoint nobody wrote down,
    not a sentence somebody phrased badly.
    """
    deploying = (ROOT / "docs" / "operating" / "deploying.md").read_text(encoding="utf-8")

    for probe in ("/healthz", "/readyz"):
        assert probe in deploying, f"{probe} is not documented in operating/deploying.md"


def _front_matter(page: str) -> dict[str, Any]:
    """A page's YAML front matter, or an empty mapping if it has none."""
    yaml = pytest.importorskip("yaml", reason="pyyaml arrives with the docs group")
    text = (ROOT / "docs" / page).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    _, _, rest = text.partition("---\n")
    block, _, _ = rest.partition("\n---")
    parsed: dict[str, Any] | None = yaml.safe_load(block)
    return parsed or {}


def test_pages_kept_out_of_the_nav_are_kept_out_of_search() -> None:
    """Unlisting a page from the nav is only half of hiding it.

    Search indexes the whole site regardless of the nav, so a page removed from
    the navigation still surfaces in results - and does it with no breadcrumb
    to explain what it is or why it is there. `commit-conventions.md` came back
    for "idempotency", ahead of neither of the two pages that answer it but
    among them, which is the shape of the problem: contributor documentation
    interleaved with user documentation in the one place a user is asking a
    question.

    Excluded from the index, not from the site. The page still builds and its
    URL still works for anyone sent the link.
    """
    # not_in_nav takes gitignore-style patterns, so a line is not necessarily a
    # filename. Expanded here rather than assumed literal, because the day
    # somebody writes "internal/*.md" this should keep checking rather than
    # start erroring on a path that was never meant to be one.
    unlisted: list[str] = []
    for line in (_config().get("not_in_nav") or "").splitlines():
        pattern = line.strip()
        if not pattern:
            continue
        if any(char in pattern for char in "*?["):
            unlisted += [
                match.relative_to(ROOT / "docs").as_posix()
                for match in sorted((ROOT / "docs").glob(pattern))
            ]
        else:
            unlisted.append(pattern)
    assert unlisted, "nothing is unlisted; has not_in_nav been removed?"

    missing = [
        page for page in unlisted if not (_front_matter(page).get("search") or {}).get("exclude")
    ]
    assert not missing, f"absent from the nav but still in the search index: {missing}"


# ---------------------------------------------------------- code samples ---

#: Put immediately before a fence to say the block is not meant to work as
#: written. Used where a sample describes an interface that does not exist yet,
#: which is honest rather than broken - and saying so in the file is better
#: than a reader discovering it by pasting the code.
ILLUSTRATIVE = "<!-- docs-test: illustrative -->"

_FENCE = re.compile(
    r"(?:(" + re.escape(ILLUSTRATIVE) + r")\s*\n)?^```(\w*)\n(.*?)^```", re.S | re.M
)


class Sample(NamedTuple):
    where: str
    language: str
    body: str
    illustrative: bool


def _samples(language: str | None = None) -> list[Sample]:
    found: list[Sample] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for index, (marker, lang, body) in enumerate(_FENCE.findall(text)):
            lang = lang or "text"
            if language and lang != language:
                continue
            rel = path.relative_to(ROOT).as_posix()
            found.append(Sample(f"{rel} block {index}", lang, body, bool(marker)))
    return found


def test_there_are_samples_to_check() -> None:
    """A parser that silently matches nothing would make every check below
    pass, which is the failure this whole file exists to avoid.
    """
    assert len(_samples()) > 40


def test_the_illustrative_marker_is_recognised() -> None:
    """The marker only means something if the parser sees it.

    A typo in the comment would silently produce an unmarked block that the
    checks then treat as real - the marker failing open rather than closed.
    Asserted against a known user of it, so renaming the marker without
    updating the pages that carry it fails here.
    """
    marked = [s for s in _samples() if s.illustrative]

    assert marked, f"nothing carries {ILLUSTRATIVE!r}; has the marker been renamed?"
    assert any("reference/sdk.md" in s.where for s in marked), (
        "the SDK sample describes a client that does not exist and should be marked"
    )


@pytest.mark.parametrize("sample", _samples("python"), ids=lambda s: s.where)
def test_every_python_sample_parses(sample: Sample) -> None:
    """Not executed - most of these need a running server - but a sample that
    does not even parse has never been read by anyone, let alone run.
    """
    compile(sample.body, sample.where, "exec")


@pytest.mark.parametrize("sample", _samples("json"), ids=lambda s: s.where)
def test_every_json_sample_parses(sample: Sample) -> None:
    """A malformed payload example is copied into somebody's test fixture and
    debugged for an hour before they suspect the documentation.
    """
    json.loads(sample.body)


@pytest.mark.parametrize("sample", _samples("bash"), ids=lambda s: s.where)
def test_every_shell_sample_parses(sample: Sample) -> None:
    """Syntax-checked, never run: these create databases and start containers.

    Passed on stdin rather than written to a temporary file, because Git Bash
    on Windows cannot open a Windows path handed to it that way - the check
    then fails for every block regardless of what it contains, which looks like
    a real failure and is not.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")

    # The resolved path, not the bare name: which() has already found it, and
    # running whatever "bash" happens to resolve to at the time is the habit
    # this project should not be teaching in its own test suite.
    # S603 is suppressed below because the argv is fixed and the sample goes in
    # on stdin, where -n parses it and stops. Nothing here is executed, which is
    # the entire reason for choosing -n over running the block.
    result = subprocess.run(  # noqa: S603
        [bash, "-n"], input=sample.body, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{sample.where}: {result.stderr.strip()}"


def test_the_documented_verifier_actually_verifies() -> None:
    """The snippet a customer will paste, run against the real signing code.

    This is the sample that matters. Every other one fails visibly when it is
    wrong - this one fails by accepting a forged event, silently, in somebody
    else's production system.

    The dashboard shows its own copy of this snippet and has its own test.
    Which is the point: there are two copies, they had already drifted before
    this existed, and only one of them was being checked.
    """
    from seskit_core.security.webhooks import sign

    sample = next(s for s in _samples("python") if "guides/webhooks.md" in s.where)
    namespace: dict[str, Any] = {}
    exec(sample.body, namespace)  # noqa: S102 - running the docs is the point
    verify = namespace["verify"]

    secret = "whsec_documented"
    body = b'{"id":"evt_1","type":"email.delivered"}'
    signature, timestamp = sign(secret, body)

    assert verify(secret, body, signature, str(timestamp)) is True, "a real signature was refused"
    assert verify(secret, b"{}", signature, str(timestamp)) is False, "a tampered body was accepted"
    assert verify("whsec_other", body, signature, str(timestamp)) is False, (
        "any secret was accepted"
    )
    assert verify(secret, body, signature, str(timestamp - 3600)) is False, "a replay was accepted"


def test_the_documented_verifier_agrees_with_the_dashboards() -> None:
    """Two copies of the same instructions, held to the same answer.

    They differ in formatting and always will - the page can breathe where a
    dashboard panel cannot. What they must not differ in is what they accept.
    """
    from seskit_api.routes.webhooks import VERIFY_SNIPPET
    from seskit_core.security.webhooks import sign

    sample = next(s for s in _samples("python") if "guides/webhooks.md" in s.where)

    verifiers = []
    for source in (sample.body, VERIFY_SNIPPET):
        namespace: dict[str, Any] = {}
        exec(source, namespace)  # noqa: S102
        verifiers.append(namespace["verify"])

    secret = "whsec_documented"
    body = b'{"id":"evt_1","type":"email.bounced"}'
    signature, timestamp = sign(secret, body)

    cases = [
        (secret, body, signature, str(timestamp)),
        (secret, b"{}", signature, str(timestamp)),
        ("whsec_other", body, signature, str(timestamp)),
        (secret, body, signature, str(timestamp - 3600)),
    ]
    for case in cases:
        page, dashboard = (verify(*case) for verify in verifiers)
        assert page == dashboard, f"the two copies disagree on {case[1]!r}"


# ---------------------------------------------------------------- README ---

README = ROOT / "README.md"

#: Generated by the llmstxt plugin at build time rather than committed, so
#: there is no file under docs/ to check them against. Their existence is
#: covered by the plugin being configured, which the tests above assert.
_GENERATED = {"llms.txt", "llms-full.txt"}


def _readme_site_links() -> list[str]:
    """Every README link that points into the published documentation site.

    The base comes from mkdocs.yml rather than being written out again here,
    so moving the site to a custom domain fails this file until the README
    follows - which is the moment the links would otherwise start rotting.
    """
    base = _config()["site_url"]
    text = README.read_text(encoding="utf-8")
    return [link for link in re.findall(r"\]\((https?://[^)\s]+)\)", text) if link.startswith(base)]


def test_the_readme_links_to_the_published_site() -> None:
    """The README is the front door for people who have not cloned anything.

    It used to link to `docs/*.md`, which was right while Pages was serving a
    404 and is wrong now: it lands a reader on raw markdown with no search and
    no navigation, one directory away from the rendered page they wanted.
    """
    assert len(_readme_site_links()) > 20, "the docs index no longer points at the site"


def test_every_readme_site_link_is_a_real_page() -> None:
    """A renamed page breaks these silently.

    `mkdocs build --strict` will not catch it - the site's own links are fine,
    and the README is not part of the site. Nothing else looks at these at all,
    which is how a front page ends up advertising four dead links.
    """
    base = _config()["site_url"]
    nav = _nav_pages(_config().get("nav", []))

    broken = []
    for link in _readme_site_links():
        tail = link[len(base) :]
        if tail in _GENERATED:
            continue
        page = "index.md" if tail == "" else f"{tail.rstrip('/')}.md"
        if not (ROOT / "docs" / page).exists():
            broken.append(f"{link} -> docs/{page} does not exist")
        elif page not in nav:
            broken.append(f"{link} -> docs/{page} is not in the nav")

    assert not broken, "README links into the site that will 404:\n  " + "\n  ".join(broken)


def test_the_readme_does_not_link_to_markdown_that_is_published() -> None:
    """The two link styles are not interchangeable and mixing them is a bug.

    A relative `docs/guides/webhooks.md` renders on GitHub and is dead on the
    site's own rendering of the README; the absolute URL works in both. Links
    to files that are *not* published - AGENTS.md, CONTRIBUTING.md, the spec -
    stay relative, which is why this only objects to ones under docs/.
    """
    text = README.read_text(encoding="utf-8")

    relative = re.findall(r"\]\((docs/[^)\s]+\.md)\)", text)
    assert not relative, f"published pages linked as files instead of URLs: {relative}"


# ------------------------------------------------------------- generated ---

#: FastAPI generates these from its own request-validation machinery. We do not
#: declare them, so there is no `Field` to hang a description on without
#: subclassing framework internals. Named rather than pattern-matched, so a
#: model of ours can never quietly join the exemption.
_FRAMEWORK_MODELS = {"ValidationError", "HTTPValidationError"}


def test_every_field_we_own_is_described() -> None:
    """An undescribed field renders as an empty cell in the API reference.

    That was the state of the whole request body - `from`, `to` and `subject`
    all arrived at the reader with nothing beside them - which reads as an
    unfinished page rather than a simple field, and sends them to the source.

    Reading the committed schema rather than building the app, so this runs
    without a database and fails in the same place CI regenerates the file.
    """
    schema = json.loads((ROOT / "docs" / "reference" / "openapi.json").read_text(encoding="utf-8"))

    missing = [
        f"{model}.{field}"
        for model, definition in sorted(schema["components"]["schemas"].items())
        if model not in _FRAMEWORK_MODELS
        for field, spec in (definition.get("properties") or {}).items()
        if not spec.get("description")
    ]
    assert not missing, f"fields with no description: {missing}"


def test_every_parameter_we_own_is_described() -> None:
    """Headers and path parameters render in the same table and were missed.

    The first version of the check above looked only at component schemas, so
    it passed while `Idempotency-Key` - the header that decides whether a retry
    sends a second message - sat in the reference with an empty cell beside it.
    A parameter is not a field, and the reader cannot tell the difference.
    """
    schema = json.loads((ROOT / "docs" / "reference" / "openapi.json").read_text(encoding="utf-8"))

    missing = [
        f"{method.upper()} {path} ({parameter.get('in')} {parameter.get('name')})"
        for path, operations in sorted(schema["paths"].items())
        for method, operation in operations.items()
        for parameter in operation.get("parameters") or []
        if not parameter.get("description")
    ]
    assert not missing, f"parameters with no description: {missing}"


def test_the_api_reference_is_not_hand_edited() -> None:
    """It carries a header saying so, and CI regenerates it to check.

    This asserts the warning is still there: someone who removes it is the
    someone about to edit the file by hand.
    """
    api = (ROOT / "docs" / "reference" / "api.md").read_text(encoding="utf-8")

    assert "Generated by scripts/export_openapi.py" in api
    assert "Do not edit" in api


def test_the_api_reference_links_resolve_to_its_own_headings() -> None:
    """The endpoint index links to anchors on the same page.

    Markdown removes a slash rather than turning it into a hyphen, so these are
    easy to get wrong by hand - and they were, before the generator computed
    them. `mkdocs --strict` catches it too, but only when the docs group is
    installed, and this runs anywhere.
    """
    api = (ROOT / "docs" / "reference" / "api.md").read_text(encoding="utf-8")

    headings = {
        re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", line[3:]).strip().lower())
        for line in api.splitlines()
        if line.startswith("## ")
    }
    links = set(re.findall(r"\]\(#([a-z0-9_-]+)\)", api))

    assert links, "the endpoint index has no anchor links at all"
    assert links <= headings, f"links with no matching heading: {sorted(links - headings)}"
