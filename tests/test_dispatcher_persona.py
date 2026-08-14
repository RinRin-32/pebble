"""The dispatcher persona: a child that can delegate coding work but not do it.

Observed in a real coordinator fan-out — a child told to bind, provision and
dispatch instead edited the file itself. That is reasonable model judgment on a
one-line change and wrong for the architecture, so the constraint is structural
rather than a prompt: the persona simply does not carry editing tools.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from pebble.core.personas import snapshot_from_persona


def _migration() -> Any:
    """Load migration 071 directly.

    The ``backend`` fixture builds the schema from metadata rather than running
    alembic, so the seeded row is not present in tests. Reading the migration's
    own constants tests the thing that actually ships.
    """
    path = (
        Path(__file__).parent.parent
        / "pebble/core/storage/migrations/versions/071_dispatcher_persona.py"
    )
    spec = importlib.util.spec_from_file_location("m071", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

DELEGATION_CHAIN = ("bind_repo", "setup_env", "dispatch_agent")
COMPREHENSION = ("read_file", "search", "diff_file")
# Every route to doing the work directly instead of delegating it.
EDITING = ("bash", "write_file", "edit_file", "task_agent")


@pytest.fixture
def dispatcher(backend: Any) -> dict[str, Any]:
    """The persona as migration 071 defines it, stored and read back."""
    m = _migration()
    backend.create_persona(
        {
            "persona_id": m.PERSONA_ID,
            "name": m.PERSONA_NAME,
            "display_name": "Dispatcher",
            "description": "Delegates coding work to an agent CLI.",
            "base_prompt": m.PROMPT,
            "tool_allowlist": list(m.ALLOWED_TOOLS),
            "applies_to_kinds": ["interactive"],
        }
    )
    row = backend.get_persona_by_name(m.PERSONA_NAME)
    assert row is not None
    return row


class TestDispatcherPersona:
    def test_is_seeded_and_enabled(self, dispatcher: dict[str, Any]) -> None:
        assert dispatcher["enabled"] is True
        assert dispatcher["applies_to_kinds"] == ["interactive"]
        # Never the default: it is opt-in for coding children only.
        assert not dispatcher["is_default"]

    def test_can_delegate(self, dispatcher: dict[str, Any]) -> None:
        tools = set(dispatcher["tool_allowlist"] or [])
        for tool in DELEGATION_CHAIN:
            assert tool in tools, f"a dispatcher must be able to call {tool}"

    def test_can_understand_the_code_it_briefs_about(
        self, dispatcher: dict[str, Any]
    ) -> None:
        tools = set(dispatcher["tool_allowlist"] or [])
        for tool in COMPREHENSION:
            assert tool in tools, f"briefing an agent needs {tool}"

    def test_cannot_edit(self, dispatcher: dict[str, Any]) -> None:
        tools = set(dispatcher["tool_allowlist"] or [])
        leaked = sorted(set(EDITING) & tools)
        assert not leaked, f"dispatcher could bypass delegation via {leaked}"

    def test_allowlist_is_a_hard_set_not_unrestricted(
        self, dispatcher: dict[str, Any]
    ) -> None:
        # None means unrestricted; the whole point is that this one is a set.
        assert dispatcher["tool_allowlist"] is not None
        assert len(dispatcher["tool_allowlist"]) > 0

    def test_snapshot_preserves_the_restriction(self, dispatcher: dict[str, Any]) -> None:
        # The snapshot is what a session is actually built from, so the
        # restriction has to survive that conversion.
        snap = snapshot_from_persona(dispatcher)
        assert snap.tools is not None
        assert set(EDITING) & snap.tools == set()
        assert "dispatch_agent" in snap.tools

    def test_prompt_explains_why(self, dispatcher: dict[str, Any]) -> None:
        # A model that understands the constraint cooperates with it; one that
        # only hits a wall tries to route around it.
        prompt = (dispatcher["base_prompt"] or "").lower()
        assert "do not write code" in prompt or "not write code" in prompt
        assert "dispatch_agent" in prompt
