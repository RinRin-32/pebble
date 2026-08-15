"""Git identity and push credentials for worktrees and dispatched agents.

Two concerns that are easy to conflate, kept apart here:

**Identity** — who a commit is authored by.  Pebble commits as a bot rather
than as the operator, so ``git log`` distinguishes what an agent did from what
a person did.  Without this, work an agent produced is indistinguishable from
the operator's own commits, which is exactly the attribution you want when
something needs explaining later.

**Credentials** — what may be pushed, and where.  A token arrives by
environment and is handed to git through an askpass helper.  The helper is
scoped to a single host: git passes the prompt text (which carries the host)
as ``argv[1]``, so a remote pointing anywhere else gets nothing.  That matters
because the thing running these commands is a coding agent working from a
model's plan — if it adds a remote to some other server, the operator's token
must not follow it there.

The token is never written to disk, never embedded in a remote URL (where it
would land in ``.git/config`` and in any diff of it), and never logged.  The
askpass script itself holds no secret: it reads the token from the environment
at call time.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pebble.core.log import get_logger

log = get_logger(__name__)

#: Only this host is offered the token.  Overridable for GHE / self-hosted.
DEFAULT_HOST = "github.com"

#: Bot identity.  Deliberately not the operator's name: a commit that pebble
#: made should say so.
DEFAULT_AUTHOR_NAME = "pebble"
DEFAULT_AUTHOR_EMAIL = "pebble@users.noreply.github.com"

#: GitHub accepts any username when the password is a token; this is the
#: conventional placeholder and is what the token's own identity overrides.
DEFAULT_USER = "x-access-token"

_ASKPASS_SCRIPT = """#!/bin/sh
# Hands the git token to exactly one host.
#
# git calls this with the prompt text as $1 -- "Username for 'https://host'" or
# "Password for 'https://user@host'" -- so the host is checkable before any
# secret is echoed.  A remote pointing somewhere else gets a non-zero exit and
# no output, which git reports as an auth failure rather than leaking the
# operator's credentials to an unexpected server.
#
# The comparison is EXACT, not a substring: matching "github.com" anywhere in
# the prompt would also match "github.com.evil.net", which is a lookalike
# domain away from handing the token to an attacker.  So the authority is
# parsed out and compared whole.
host="${PEBBLE_GIT_HOST}"
[ -n "$host" ] || exit 1

url="${1#*://}"      # drop scheme and everything before it
url="${url%%\\'*}"    # drop the closing quote and trailing prompt text
url="${url##*@}"     # drop any userinfo
url="${url%%/*}"     # drop any path
url="${url%%:*}"     # drop any port
[ "$url" = "$host" ] || exit 1

case "$1" in
  Username*) printf '%s\\n' "${PEBBLE_GIT_USER:-x-access-token}" ;;
  *) printf '%s\\n' "${PEBBLE_GIT_TOKEN}" ;;
esac
"""

_askpass_cache: str | None = None


def git_token() -> str:
    """The push token, or "" when none is configured.

    ``GITHUB_TOKEN`` is accepted as a fallback because that is the name CI
    systems and the GitHub CLI already use; an operator who exported it should
    not have to learn a second one.
    """
    return (os.environ.get("PEBBLE_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def git_host() -> str:
    return (os.environ.get("PEBBLE_GIT_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST


def has_push_credentials() -> bool:
    """Whether a push could authenticate at all.

    Callers use this to fail with "no token configured" up front rather than
    letting git fail obscurely halfway through a dispatch.
    """
    return bool(git_token())


def author_identity() -> tuple[str, str]:
    name = (os.environ.get("PEBBLE_GIT_AUTHOR_NAME") or DEFAULT_AUTHOR_NAME).strip()
    email = (os.environ.get("PEBBLE_GIT_AUTHOR_EMAIL") or DEFAULT_AUTHOR_EMAIL).strip()
    return name or DEFAULT_AUTHOR_NAME, email or DEFAULT_AUTHOR_EMAIL


def _askpass_path() -> str | None:
    """Materialise the askpass helper, returning its path (or None).

    Written at runtime rather than shipped as package data: git executes this
    path directly, so it has to carry the exec bit, and package installers do
    not reliably preserve one.  The file holds no secret — it reads the token
    from the environment when git calls it — so a 0700 file in the temp dir is
    the whole of it.
    """
    global _askpass_cache
    if _askpass_cache and Path(_askpass_cache).is_file():
        return _askpass_cache
    try:
        d = Path(tempfile.gettempdir()) / "pebble-git"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "askpass.sh"
        p.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        p.chmod(stat.S_IRWXU)  # 0700 — owner only
        _askpass_cache = str(p)
        return _askpass_cache
    except OSError:
        log.warning("git.askpass_unavailable", exc_info=True)
        return None


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for git subprocesses: bot identity, and credentials if any.

    ``GIT_TERMINAL_PROMPT=0`` stays set either way — with a token the askpass
    helper answers, and without one git must fail fast instead of hanging a
    tool call on a prompt nobody can see.
    """
    env = dict(base if base is not None else os.environ)
    name, email = author_identity()
    env.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    token = git_token()
    if not token:
        return env
    askpass = _askpass_path()
    if askpass:
        env["GIT_ASKPASS"] = askpass
        env["PEBBLE_GIT_TOKEN"] = token
        env["PEBBLE_GIT_HOST"] = git_host()
        env["PEBBLE_GIT_USER"] = (
            os.environ.get("PEBBLE_GIT_USER") or DEFAULT_USER
        ).strip() or DEFAULT_USER
    return env


def agent_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Git env plus ``GH_TOKEN``, for a dispatched coding agent.

    The agent CLIs reach for ``gh`` on their own when asked to open a pull
    request — it is the interface they already know — and ``gh`` authenticates
    straight from ``GH_TOKEN`` with no login step and no config file.  So the
    one token serves both paths: git push via askpass, ``gh`` via the
    environment.
    """
    env = git_env(base)
    token = git_token()
    if token:
        env.setdefault("GH_TOKEN", token)
    return env


@dataclass(frozen=True)
class ResolvedCredential:
    """Which token a push will use, and where it came from.

    ``source`` is carried so the caller can say so out loud: "pushing as your
    linked GitHub account" and "pushing with the instance token" are different
    enough that a user should not have to guess which happened.
    """

    token: str
    host: str
    login: str
    source: str  # "user" | "instance" | "none"

    @property
    def ok(self) -> bool:
        return bool(self.token)


def resolve_for_user(
    storage: Any, user_id: str, *, may_use_instance: bool = False
) -> ResolvedCredential:
    """Pick the credential a push by *user_id* should use.

    Order, and the reasoning:

    1. **The user's own token.** GitHub's permissions then bound what the push
       can reach, and the commit is attributed to the person who asked for it.
       No pebble-side grant is needed: they are spending their own access.
    2. **The instance token**, only when *may_use_instance* — the caller checks
       the ``code_push`` capability for that. This spends the OPERATOR's
       credential, which is exactly the asymmetry ``code_dispatch`` already
       encodes for agent spend.
    3. **Nothing**, and the caller reports that rather than letting git fail
       with an opaque auth error halfway through.
    """
    if user_id and storage is not None:
        try:
            row = storage.get_user_git_credential(user_id)
        except Exception:
            log.warning("git.user_credential_read_failed", exc_info=True)
            row = None
        if row and row.get("token_ct"):
            try:
                from pebble.core.secret_cipher import decrypt

                token = decrypt(row["token_ct"])
            except Exception:
                # A key rotated out without re-saving lands here. Fall through
                # to the instance token rather than failing the whole dispatch,
                # but say so in the log — the user's token is now unreadable.
                log.warning("git.user_credential_undecryptable", exc_info=True)
                token = ""
            if token:
                return ResolvedCredential(
                    token=token,
                    host=row.get("host") or DEFAULT_HOST,
                    login=row.get("login") or "",
                    source="user",
                )
    if may_use_instance:
        token = git_token()
        if token:
            return ResolvedCredential(token=token, host=git_host(), login="", source="instance")
    return ResolvedCredential(token="", host=git_host(), login="", source="none")


def env_for_credential(
    cred: ResolvedCredential, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Git/gh environment for a specific resolved credential."""
    env = dict(base if base is not None else os.environ)
    name, email = author_identity()
    if cred.source == "user" and cred.login:
        # Attribute to the user's own GitHub identity when we know it, so the
        # commit is theirs rather than the bot's.
        name = cred.login
        email = f"{cred.login}@users.noreply.github.com"
    env.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if not cred.ok:
        return env
    askpass = _askpass_path()
    if askpass:
        env["GIT_ASKPASS"] = askpass
        env["PEBBLE_GIT_TOKEN"] = cred.token
        env["PEBBLE_GIT_HOST"] = cred.host
        env["PEBBLE_GIT_USER"] = cred.login or DEFAULT_USER
    env["GH_TOKEN"] = cred.token
    env["GH_HOST"] = cred.host
    return env


def redact_credential(text: str, cred: ResolvedCredential) -> str:
    """Scrub a specific resolved token from *text*."""
    if cred.token and len(cred.token) >= 8:
        return text.replace(cred.token, "[REDACTED:git-token]")
    return text


def token_hint(token: str) -> str:
    """A short trailing fragment, for "set, ending 1a2b" in the UI."""
    t = (token or "").strip()
    return t[-4:] if len(t) >= 8 else ""


def redact(text: str) -> str:
    """Remove the token from *text*.

    Anything an agent produces can quote a command line or an error, and git
    and gh both echo URLs on failure.  Cheap insurance before output reaches a
    log, a note, or a Discord message.
    """
    token = git_token()
    if token and len(token) >= 8:
        return text.replace(token, "[REDACTED:git-token]")
    return text
