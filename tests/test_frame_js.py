"""App frame (frame.js): right-panel column rules and the panel itself.

The numbers are DeepSeek Harness's: centre keeps 400px, the panel opens at
45% of the viewport, is clamped to [300, 70%], closes when fewer than 300px
are left, and goes fullscreen below 768px.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FRAME = _ROOT / "pebble/shared_static/frame.js"
_SHELL = _ROOT / "pebble/shared_static/shell.js"
_FAKE_DOM = Path(__file__).resolve().parent / "_fake_dom.mjs"


def _run(tmp_path: Path, body: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")
    script = tmp_path / "frame_harness.mjs"
    script.write_text(
        f'import {{ installFakeDom }} from "file://{_FAKE_DOM}";\n'
        "installFakeDom();\n"
        f'const F = await import("file://{_FRAME}");\n'
        "function eq(a, b, m) {\n"
        "  const x = JSON.stringify(a), y = JSON.stringify(b);\n"
        "  if (x !== y) throw new Error((m || 'mismatch') + ': ' + x + ' !== ' + y);\n"
        "}\n"
        "function ok(c, m) { if (!c) throw new Error(m || 'assertion failed'); }\n"
        + body
        + '\nconsole.log("OK");\n',
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_compute_columns(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
eq(F.computeColumns(1600, 266, 0), {right: 0, fullscreen: false, autoClosed: false}, "closed");
eq(F.computeColumns(1600, 266, 720).right, 720, "preferred width fits");
eq(F.computeColumns(1600, 266, 100).right, 300, "clamped up to 300");
eq(F.computeColumns(1600, 266, 1500).right, 934, "never squeezes the centre below 400");
eq(F.computeColumns(2400, 266, 2000).right, 1680, "max 70% of the viewport");
eq(F.computeColumns(900, 266, 400), {right: 0, fullscreen: false, autoClosed: true},
   "under 300px left -> auto-close");
eq(F.computeColumns(700, 0, 300), {right: 700, fullscreen: true, autoClosed: false},
   "below 768 the panel covers the screen");
eq(F.defaultRightWidth(1600), 720, "opens at 45%");
eq(F.clampWidth(500, 264, 420), 420);
""",
    )


def test_right_panel_open_close_and_auto_close(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
let vw = 1600;
const ws = document.createElement("div");
document.body.appendChild(ws);
const rp = new F.RightPanel(ws, {viewport: () => vw, railWidth: () => 266});
eq(ws.dataset.right, "closed");
ok(rp.root.hidden, "starts closed");
const body = rp.addTab("preview", "Preview");
let shown = 0;
rp.addTab("other", "Other", () => shown++);
rp.show("preview");
eq(ws.dataset.right, "open");
ok(!rp.root.hidden && !body.hidden, "preview tab visible");
eq(rp.tabs.get("preview").tab.getAttribute("aria-selected"), "true");
rp.show("other");
ok(body.hidden && shown === 1, "switching tabs runs onShow");
rp.toggle();
eq(ws.dataset.right, "closed", "toggle closes");
rp.toggle();
eq(ws.dataset.right, "open", "toggle reopens on the last tab");
vw = 900;  // window narrows: 900 - 266 - 400 < 300
rp.layout();
eq(ws.dataset.right, "closed", "auto-closed when the centre would starve");
ok(!rp.isOpen(), "and stays closed");
vw = 700;
ok(rp.hasRoom(), "narrow screens use fullscreen instead");
rp.show("preview");
eq(ws.dataset.right, "fullscreen");
ok(rp.handle.hidden, "no resize handle in fullscreen");
rp.closeBtn.click();
eq(ws.dataset.right, "closed");
""",
    )


def test_fake_style_supports_custom_properties(tmp_path: Path) -> None:
    """The panel writes --pb-right-w via style.setProperty; the fake DOM's
    style is a plain object, so the harness provides setProperty."""
    _run(
        tmp_path,
        """
const ws = document.createElement("div");
const rp = new F.RightPanel(ws, {viewport: () => 1600, railWidth: () => 266});
rp.addTab("p", "P");
rp.show("p");
eq(ws.style["--pb-right-w"], "720px");
""",
    )


def test_shell_wires_frame_and_keeps_preview_fallback() -> None:
    src = _SHELL.read_text(encoding="utf-8")
    assert 'import { RightPanel, attachRailResize } from "./frame.js";' in src
    assert 'const workspace = make("div", "pb-workspace");' in src
    assert "rightPanel.hasRoom()" in src
    # The split-cell preview stays as the no-room fallback.
    assert 'pm.openPaneBeside("preview")' in src
    # Pane-modifier chords, yielding to macOS text editing.
    assert 'e.key.toLowerCase() !== "b"' in src
    assert "IS_MAC && inEditable(e.target)" in src
