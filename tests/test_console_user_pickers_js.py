"""Every user picker in the console loads its own users.

Reported from the running console: the API/tokens page showed no selectable
users "unless I go into the user page then api control again". The list was
not slow — it had never been requested. ``_adminUsers`` was a cache only
``loadAdminUsers()`` ever filled, and the token, channel and audit pickers all
read it blindly, so whichever tab you opened first decided whether they worked.

These tests drive the real ``admin.js`` under ``node`` with a small DOM shim,
because the bug is in the wiring between functions and a text assertion would
pass against a file that still does the wrong thing at runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ADMIN_JS = _ROOT / "pebble/console/static/admin.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# Enough browser to let admin.js evaluate and drive a <select>. Deliberately
# minimal: this pins the picker wiring, not the DOM.
_HARNESS = """
const fs = require('fs'), vm = require('vm');
let fetches = 0;
const els = {};
function mkEl(id) {
  return { id, value: '', children: [],
           appendChild(c) { this.children.push(c); },
           querySelectorAll() { return []; }, addEventListener() {},
           setAttribute() {}, getAttribute() { return ''; }, style: {} };
}
['admin-token-user', 'admin-channel-user', 'audit-user-filter',
 'admin-users-table'].forEach(function (i) { els[i] = mkEl(i); });
global.document = {
  getElementById: (id) => els[id] || null,
  createElement: () => ({ value: '', textContent: '' }),
  querySelectorAll: () => [], querySelector: () => null, addEventListener() {},
  body: { addEventListener() {}, classList: { add() {}, remove() {} } },
};
global.addEventListener = function () {}; global.removeEventListener = function () {};
global.window = global;
global.location = { pathname: '/', href: '/' };
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.EventSource = function () { return { close() {}, addEventListener() {} }; };
global.setInterval = function () { return 0; };
global.authFetch = function () {
  fetches++;
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ users: [
    { user_id: 'u1', username: 'rin', display_name: 'Rin' },
    { user_id: 'u2', username: 'bot', display_name: 'Bot' },
  ] }) });
};
global.setSafeHtml = function (el, html) { el.children = []; el._html = html; };
global.escapeHtml = (s) => String(s);
vm.runInThisContext(fs.readFileSync(%(admin)s, 'utf8'), { filename: 'admin.js' });

%(body)s
"""


def _run(body: str) -> dict:
    script = _HARNESS % {"admin": json.dumps(str(_ADMIN_JS)), "body": body}
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestPickersLoadTheirOwnUsers:
    def test_the_token_picker_populates_without_visiting_users_first(self) -> None:
        """The exact reported bug: open API/tokens first, get an empty list."""
        out = _run(
            """
            _populateTokenUserSelect();
            setTimeout(function () {
              console.log(JSON.stringify({ n: els['admin-token-user'].children.length }));
            }, 40);
            """
        )
        assert out["n"] == 2, "the picker must fetch its own users, not wait for the Users tab"

    def test_the_channel_picker_populates_on_its_own(self) -> None:
        out = _run(
            """
            _populateChannelUserSelect();
            setTimeout(function () {
              console.log(JSON.stringify({ n: els['admin-channel-user'].children.length }));
            }, 40);
            """
        )
        assert out["n"] == 2

    def test_a_selection_survives_the_refresh_repaint(self) -> None:
        # The picker repaints when the refresh lands. Losing the operator's
        # choice mid-interaction is its own kind of jank.
        out = _run(
            """
            _populateTokenUserSelect();
            setTimeout(function () {
              els['admin-token-user'].value = 'u2';
              _populateTokenUserSelect();
              setTimeout(function () {
                console.log(JSON.stringify({ v: els['admin-token-user'].value }));
              }, 40);
            }, 40);
            """
        )
        assert out["v"] == "u2"


class TestItDoesNotStampede:
    def test_pickers_opened_together_share_one_request(self) -> None:
        """Three pickers on one page load must not be three round trips.

        They also must not race: each used to overwrite `_adminUsers`, so the
        last response won regardless of which was freshest.
        """
        out = _run(
            """
            _populateTokenUserSelect();
            _populateChannelUserSelect();
            _fillUserSelect('audit-user-filter', 'All users');
            setTimeout(function () {
              console.log(JSON.stringify({ fetches: fetches,
                                           n: els['audit-user-filter'].children.length }));
            }, 60);
            """
        )
        assert out["fetches"] == 1, "in-flight requests must be shared, not duplicated"
        assert out["n"] == 2

    def test_a_warm_cache_costs_no_request(self) -> None:
        out = _run(
            """
            _populateTokenUserSelect();
            setTimeout(function () {
              fetches = 0;
              _populateChannelUserSelect();
              setTimeout(function () {
                console.log(JSON.stringify({ fetches: fetches }));
              }, 40);
            }, 40);
            """
        )
        assert out["fetches"] == 0


class TestFailureKeepsTheLastGoodList:
    def test_a_failed_refresh_does_not_empty_the_picker(self) -> None:
        """A dropdown that blanks itself on one bad refresh is worse than a
        stale one: the operator cannot tell which happened."""
        out = _run(
            """
            _populateTokenUserSelect();
            setTimeout(function () {
              global.authFetch = function () { return Promise.reject(new Error('offline')); };
              _fillUserSelect('admin-token-user', 'Select user...');
              setTimeout(function () {
                console.log(JSON.stringify({ n: els['admin-token-user'].children.length }));
              }, 40);
            }, 40);
            """
        )
        assert out["n"] == 2
