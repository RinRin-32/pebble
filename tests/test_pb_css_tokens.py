"""The process / frame / Trajectory stylesheets read only ``--pb-*`` tokens.

``pb-tokens.css`` is the single reskin point for that UI: it maps every
``--pb-*`` variable onto the existing base.css palette.  The structural
sheets must not reach past it — no other custom properties and no raw
colours (``mask-image`` gradients excepted: their black is an alpha mask,
not a colour anyone sees) — or a reskin would have to hunt through them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parent.parent / "pebble/shared_static"
_STRUCTURAL = ["process.css", "frame.css", "timeline.css"]
_PAGES = [
    "pebble/ui/static/index.html",
    "pebble/console/static/index.html",
    "pebble/console/static/coordinator/index.html",
]

# Layout variables frame.js sets at runtime from drag handles — widths, not
# theme tokens, so pb-tokens.css does not (and must not) define them.
_RUNTIME_VARS = {"--pb-right-w", "--pb-rail-w"}

_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(")


def _declarations(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [d.strip() for d in re.split(r"[;{}]", css) if ":" in d]


@pytest.mark.parametrize("name", _STRUCTURAL)
def test_structural_sheet_uses_only_pb_tokens(name: str) -> None:
    path = _STATIC / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    css = path.read_text(encoding="utf-8")
    foreign = sorted(
        set(re.findall(r"var\(\s*(--[\w-]+)", css)) - set(re.findall(r"var\(\s*(--pb-[\w-]+)", css))
    )
    assert not foreign, f"{name} reads non --pb-* tokens: {foreign}"
    raw = [
        d
        for d in _declarations(css)
        if _COLOUR.search(d) and not d.startswith(("-webkit-mask-image", "mask-image"))
    ]
    assert not raw, f"{name} has raw colours (move them to pb-tokens.css): {raw}"


def test_every_pb_token_used_is_defined() -> None:
    defined = set(re.findall(r"(--pb-[\w-]+)\s*:", (_STATIC / "pb-tokens.css").read_text()))
    used: set[str] = set()
    for name in _STRUCTURAL:
        path = _STATIC / name
        if path.exists():
            used |= set(re.findall(r"var\(\s*(--pb-[\w-]+)", path.read_text()))
    missing = used - defined - _RUNTIME_VARS
    assert not missing, f"undefined --pb-* tokens: {sorted(missing)}"


def test_token_file_defines_only_pb_aliases() -> None:
    css = (_STATIC / "pb-tokens.css").read_text(encoding="utf-8")
    names = re.findall(r"^\s*(--[\w-]+)\s*:", css, flags=re.M)
    assert names and all(n.startswith("--pb-") for n in names)


@pytest.mark.parametrize("page", _PAGES)
def test_pages_link_tokens_before_structural_sheets(page: str) -> None:
    html = (Path(__file__).resolve().parent.parent / page).read_text(encoding="utf-8")
    assert "/shared/pb-tokens.css" in html and "/shared/process.css" in html
    assert html.index("/shared/pb-tokens.css") < html.index("/shared/process.css")
