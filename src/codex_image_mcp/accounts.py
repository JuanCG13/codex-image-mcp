"""Several ChatGPT accounts for Codex, their usage limits and their rate-limit reset credits.

Each account is a Codex home of its own holding only that account's auth.json:
- "default" is the user's own Codex home ($CODEX_HOME or ~/.codex).
- any other name lives under $CODEX_IMAGE_ACCOUNTS_DIR (default
  ~/.local/share/codex-image-mcp/accounts/<name>/).

Usage limits and reset credits are read and redeemed through the official
`codex app-server` JSON-RPC methods `account/rateLimits/read` and
`account/rateLimitResetCredit/consume`, so tokens never leave Codex.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT = "default"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
FILE_STORE = 'cli_auth_credentials_store="file"'
APP_SERVER_TIMEOUT = 60
# How long an account that hit its limit is skipped when Codex gives no reset time.
FALLBACK_COOLDOWN = 15 * 60
USAGE_LIMIT_RE = re.compile(r"usage limit|usage_limit_exceeded|usageLimitExceeded", re.I)


class AccountError(RuntimeError):
    """Raised when an account cannot be found, read or changed."""


def accounts_dir() -> Path:
    return Path(os.environ.get("CODEX_IMAGE_ACCOUNTS_DIR")
                or Path.home() / ".local" / "share" / "codex-image-mcp" / "accounts")


def default_home(override: str | None = None) -> Path:
    return Path(override or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise AccountError(f"invalid account name {name!r}: use 1-32 of a-z, 0-9, '-' or '_'")
    return name


def account_home(name: str, default_override: str | None = None) -> Path:
    """The Codex home of an account. Does not check that the account is signed in."""
    if name == DEFAULT:
        return default_home(default_override)
    return accounts_dir() / validate_name(name)


def list_accounts() -> list[str]:
    """"default" first, then every added account that holds a login, by name."""
    root = accounts_dir()
    added = sorted(p.name for p in root.iterdir()
                   if p.is_dir() and NAME_RE.fullmatch(p.name) and p.name != DEFAULT
                   and (p / "auth.json").is_file()) if root.is_dir() else []
    return [DEFAULT, *added]


def remove_account(name: str) -> dict:
    """Delete an added account's Codex home, which signs it out on this machine."""
    if name == DEFAULT:
        raise AccountError("the default account is your own Codex home; sign out with `codex logout`")
    home = account_home(name)
    if not home.is_dir():
        raise AccountError(f"no account named {name!r}")
    try:
        shutil.rmtree(home)
    except OSError as e:
        raise AccountError(f"could not remove account {name!r}: {e}") from e
    clear_exhausted(name)
    return {"removed": name}


def token_claims(home: Path) -> dict:
    """Plan and email from the id_token in auth.json, without verifying it (local display only)."""
    try:
        token = json.loads((home / "auth.json").read_text())["tokens"]["id_token"]
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        return {}
    auth = claims.get("https://api.openai.com/auth") or {}
    return {"email": claims.get("email"), "plan": auth.get("chatgpt_plan_type")}


# --- codex app-server -------------------------------------------------------------------

def file_store_args(name: str) -> list[str]:
    """Added accounts keep their login in their own auth.json, never a shared keyring entry."""
    return [] if name == DEFAULT else ["-c", FILE_STORE]


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group: `codex` is often a Node wrapper whose native child
    survives a plain kill and would keep the pipes open forever."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()


def app_server_call(home: Path, method: str, params, codex_bin: str,
                    timeout: int = APP_SERVER_TIMEOUT, extra_args: list[str] | None = None):
    """Run one JSON-RPC request against `codex app-server` with CODEX_HOME=home."""
    env = dict(os.environ, CODEX_HOME=str(home))
    try:
        proc = subprocess.Popen([codex_bin, "app-server", *(extra_args or [])],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=env,
                                start_new_session=os.name == "posix")
    except FileNotFoundError as e:
        raise AccountError(f"codex CLI not found ({codex_bin}); install it or set CODEX_BIN") from e
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_group(proc)

    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def reply(request_id: int) -> dict:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == request_id and ("result" in msg or "error" in msg):
                    return msg
            if timed_out.is_set():
                raise AccountError(f"codex app-server did not answer {method} within {timeout}s")
            raise AccountError(f"codex app-server exited without answering {method}")

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "codex-image-mcp", "version": "0.1.0"},
                         "capabilities": {"experimentalApi": True}}})
        if "error" in (init := reply(1)):
            raise AccountError(f"codex app-server initialize failed: {init['error'].get('message')}")
        send({"method": "initialized"})
        send({"id": 2, "method": method, "params": params})
        msg = reply(2)
    except (BrokenPipeError, OSError) as e:
        raise AccountError(f"codex app-server failed during {method}: {e}") from e
    finally:
        timer.cancel()
        _kill_group(proc)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        proc.wait()
    if "error" in msg:
        raise AccountError(f"{method} failed: {msg['error'].get('message', msg['error'])}")
    return msg["result"]


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _window(win: dict | None, now: float) -> dict | None:
    if not win:
        return None
    resets_at = win.get("resetsAt")
    return {"used_percent": win.get("usedPercent", 0),
            "window_minutes": win.get("windowDurationMins"),
            "resets_at": _iso(resets_at),
            "resets_in_minutes": max(0, round((resets_at - now) / 60)) if resets_at else None}


def summarize(name: str, raw: dict, home: Path) -> dict:
    """Turn an account/rateLimits/read result into what an agent needs to decide."""
    now = time.time()
    snap = (raw.get("rateLimitsByLimitId") or {}).get("codex") or raw.get("rateLimits") or {}
    primary, secondary = snap.get("primary"), snap.get("secondary")
    full = [w for w in (primary, secondary) if w and w.get("usedPercent", 0) >= 100]
    limited = bool(full or snap.get("rateLimitReachedType"))
    resets = [w["resetsAt"] for w in full if w.get("resetsAt")]
    credits = raw.get("rateLimitResetCredits") or {}
    claims = token_claims(home)
    return {
        "account": name,
        "email": claims.get("email"),
        "plan": snap.get("planType") or claims.get("plan"),
        "limit_reached": limited,
        "available_again_at": _iso(max(resets)) if limited and resets else None,
        "five_hour": _window(primary, now),
        "weekly": _window(secondary, now),
        "reset_credits": {
            "available": credits.get("availableCount", 0),
            "credits": [{"id": c.get("id"), "title": c.get("title"),
                         "expires_at": _iso(c.get("expiresAt"))}
                        for c in credits.get("credits") or [] if c.get("status") == "available"],
        },
    }


def read_usage(name: str, codex_bin: str, default_override: str | None = None) -> dict:
    home = account_home(name, default_override)
    if not (home / "auth.json").is_file():
        raise AccountError(f"account {name!r} is not signed in; call start_device_login "
                           f"with account={name!r}")
    raw = app_server_call(home, "account/rateLimits/read", None, codex_bin,
                          extra_args=file_store_args(name))
    summary = summarize(name, raw, home)
    if summary["limit_reached"]:
        until = summary["available_again_at"]
        mark_exhausted(name, datetime.fromisoformat(until).timestamp() if until else None)
    else:
        clear_exhausted(name)
    return summary


def usage_report(codex_bin: str, account: str | None = None,
                 default_override: str | None = None) -> dict:
    names = [account] if account else list_accounts()
    report = []
    for name in names:
        try:
            report.append(read_usage(name, codex_bin, default_override))
        except AccountError as e:
            report.append({"account": name, "error": str(e)})
    usable = [a["account"] for a in report if "error" not in a and not a["limit_reached"]]
    return {"accounts": report, "usable_accounts": usable,
            "total_reset_credits": sum(a.get("reset_credits", {}).get("available", 0)
                                       for a in report)}


_RESET_LOCK = threading.Lock()


def use_reset_credit(name: str, codex_bin: str, credit_id: str | None = None,
                     force: bool = False, default_override: str | None = None) -> dict:
    """Redeem one rate-limit reset credit of an account (irreversible).

    Unless force, it refuses while the account still has usage left, so a credit is
    never spent on limits that were not reached. Resets run one at a time, so two calls
    cannot both pass the check and spend two credits."""
    with _RESET_LOCK:
        return _use_reset_credit(name, codex_bin, credit_id, force, default_override)


def _use_reset_credit(name: str, codex_bin: str, credit_id: str | None, force: bool,
                      default_override: str | None) -> dict:
    before = read_usage(name, codex_bin, default_override)
    if before["reset_credits"]["available"] < 1:
        raise AccountError(f"account {name!r} has no reset credits available")
    if not before["limit_reached"] and not force:
        raise AccountError(
            f"account {name!r} has not reached its usage limit (5h "
            f"{(before['five_hour'] or {}).get('used_percent')}%, weekly "
            f"{(before['weekly'] or {}).get('used_percent')}%); a reset now would waste the "
            "credit. Pass force=true to redeem it anyway")
    if not credit_id and before["reset_credits"]["credits"]:
        # Spend the credit that expires first, so none lapses unused.
        credit_id = min(before["reset_credits"]["credits"],
                        key=lambda c: c["expires_at"] or "9999")["id"]
    params = {"idempotencyKey": str(uuid.uuid4())}
    if credit_id:
        params["creditId"] = credit_id
    try:
        result = app_server_call(account_home(name, default_override),
                                 "account/rateLimitResetCredit/consume", params, codex_bin,
                                 extra_args=file_store_args(name))
    except AccountError as e:
        raise AccountError(f"{e}. The credit may or may not have been spent: call "
                           "account_usage before trying again") from e
    report = {"account": name, "outcome": result.get("outcome"), "credit_id": credit_id}
    try:
        report["usage"] = read_usage(name, codex_bin, default_override)
    except AccountError as e:
        report["usage_error"] = str(e)
    return report


# --- which account runs the next job ----------------------------------------------------

_EXHAUSTED: dict[str, float] = {}
_EXHAUSTED_LOCK = threading.Lock()


def mark_exhausted(name: str, until: float | None = None) -> None:
    with _EXHAUSTED_LOCK:
        _EXHAUSTED[name] = until or time.time() + FALLBACK_COOLDOWN


def clear_exhausted(name: str) -> None:
    with _EXHAUSTED_LOCK:
        _EXHAUSTED.pop(name, None)


def is_exhausted(name: str) -> bool:
    with _EXHAUSTED_LOCK:
        until = _EXHAUSTED.get(name)
        if until and until <= time.time():
            del _EXHAUSTED[name]
            until = None
        return until is not None


def candidates(account: str | None) -> list[str]:
    """Accounts to try in order: the one asked for, or every signed-in account that has
    not hit its limit (the default first)."""
    if account:
        if account != DEFAULT and account not in list_accounts():
            raise AccountError(f"no signed-in account named {account!r}; "
                               f"known accounts: {', '.join(list_accounts())}")
        return [account]
    return [n for n in list_accounts() if not is_exhausted(n)]


def is_usage_limit(message: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(message or ""))
