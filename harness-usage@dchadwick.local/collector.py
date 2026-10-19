#!/usr/bin/env python3
"""Usage collector for the Harness Usage GNOME Shell extension.

Reads subscription usage from the Codex CLI and OpenCode's Go quota endpoint,
then prints a JSON document on stdout. Read-only; never writes to harness
state.

Sources:
  * Codex CLI  (`codex app-server --stdio`)
      One JSON-RPC round trip per collect: the CLI starts a transient
      app server, answers, and is reaped.  No daemon is involved, so
      nothing has to survive a logout.  Method
      `account/rateLimits/read` returns the same 5-hour / weekly
      windows the TUI's /status screen shows, plus the plan type and
      any banked rate-limit reset credits.
  * OpenCode Go quota (GET opencode.ai/zen/go/v1/usage)
      The subscription's rolling / weekly / monthly windows as used
      percentages.  Authenticated with the `opencode-go` key, taken
      from OpenCode's auth.json and falling back to pi's — pi and
      OpenCode share the same subscription, and OpenCode's own key is
      often the expired one.  This is the only network call; failure
      degrades to local-only.

Usage:
  collector.py [--self-test] [--codex-bin PATH] [--opencode-auth PATH]
               [--pi-auth PATH] [--offline]
"""

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# The Go quota windows, in the order the endpoint reports them ("rolling" is
# the 5-hour window the console calls Rolling).
GO_WINDOWS = ("rolling", "weekly", "monthly")

OPENCODE_GO_USAGE_URL = os.environ.get(
    "OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage"
)
OPENCODE_AUTH_DEFAULT = "~/.local/share/opencode/auth.json"
PI_AUTH_DEFAULT = "~/.pi/agent/auth.json"
_HTTP_TIMEOUT = 6.0

# Cloudflare fronting opencode.ai rejects unknown user agents outright
# (error 1010, browser_signature_banned), so the client identifies as the
# OpenCode CLI it is standing in for.  A plain curl will not reproduce this.
_ZEN_HEADERS = {
    "User-Agent": "opencode/latest/2.0.30/cli",
    "x-opencode-client": "cli",
    "x-opencode-project": "global",
    "Accept": "application/json",
}

_CODEX_TIMEOUT = 30.0  # ponytail: generous; a cold app-server answers in ~1s

# The extension is spawned by gnome-shell, whose PATH is systemd's user
# default: it has no npm/pi shim directory and no `node`.  Tried in order
# after $CODEX_BIN.  The daemon package ships a standalone ELF, so it is
# preferred over the npm shim (a `#!/usr/bin/env node` script that needs an
# interpreter the shell PATH cannot resolve).
_CODEX_BIN_CANDIDATES = (
    "~/.codex/packages/app-server-daemon/current/bin/codex",
    "~/.local/share/pi-node/current/bin/codex",
    "~/.local/bin/codex",
)


def _find_codex_binary(explicit=None):
    """Locate the codex CLI, or None."""
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    env = os.environ.get("CODEX_BIN")
    if env:
        return env if os.access(env, os.X_OK) else None
    found = shutil.which("codex")
    if found:
        return found
    for candidate in _CODEX_BIN_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.access(path, os.X_OK):
            return path
    return None


def _codex_env(binary):
    """Environment for spawning codex, with its own directory on PATH.

    The npm-distributed `codex` is a `#!/usr/bin/env node` shim, and node
    lives beside it — but not on the shell's systemd PATH.  Prepending the
    binary's directory lets the shim find its interpreter without
    hardcoding a node location.  Harmless for the standalone binary.
    """
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(binary) + os.pathsep + env.get("PATH", "")
    return env


def _codex_rpc(binary, method, timeout=_CODEX_TIMEOUT):
    """One JSON-RPC round trip against a transient `codex app-server --stdio`.

    The app server can also run as a long-lived daemon behind a unix socket,
    but `--stdio` needs no daemon at all: it starts a server per call, answers
    and is reaped.  That removes a dependency on a background process that
    does not survive a logout, and removes a hand-rolled WebSocket client.

    stdin must stay **open** until the reply arrives, because the server
    treats EOF as shutdown and exits without answering (verified:
    communicate() returns zero bytes), so this selects on stdout instead.
    """
    proc = subprocess.Popen(
        [binary, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_codex_env(binary),
    )
    try:
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "harness-usage", "version": "0.1"}},
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": {}},
        ):
            proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout
        buffered = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.25)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buffered += chunk
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if reply.get("id") != 2:
                    continue
                if "error" in reply:
                    raise OSError(str(reply["error"].get("message", reply["error"])))
                return reply.get("result")
        raise OSError("codex app-server did not answer before the timeout")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _normalise_window(raw):
    if not isinstance(raw, dict) or raw.get("usedPercent") is None:
        return None
    return {
        "used_percent": float(raw["usedPercent"]),
        "window_minutes": raw.get("windowDurationMins"),
        "resets_at": raw.get("resetsAt"),
    }


def _normalise_rate_limits(payload):
    """Map the app-server reply onto the shape extension.js renders."""
    by_id = payload.get("rateLimitsByLimitId") or {}
    snapshot = by_id.get("codex") or payload.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    credits = snapshot.get("credits") or {}
    return {
        "limit_id": snapshot.get("limitId"),
        "plan_type": snapshot.get("planType"),
        "primary": _normalise_window(snapshot.get("primary")),
        "secondary": _normalise_window(snapshot.get("secondary")),
        "credits": {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        },
        "reset_credits_available": (payload.get("rateLimitResetCredits") or {}).get(
            "availableCount"
        ),
    }


def collect_codex(codex_bin=None):
    """Subscription windows for the Codex account."""
    result = {"available": False, "rate_limits": None, "error": None}

    binary = _find_codex_binary(codex_bin)
    if not binary:
        result["error"] = "could not find the codex CLI (set CODEX_BIN)"
        return result

    try:
        payload = _codex_rpc(binary, "account/rateLimits/read")
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["rate_limits"] = _normalise_rate_limits(payload or {})
    if result["rate_limits"] is None:
        result["error"] = "codex returned no rate-limit bucket"
        return result
    result["available"] = True
    return result


def _http_error_code(exc):
    """Pull the API's `code` out of an error body, e.g. `api_key_expired`."""
    try:
        return json.loads(exc.read().decode()).get("error", {}).get("code", "")
    except (OSError, ValueError, AttributeError):
        return ""


def _credential_key(path):
    """Read the `opencode-go` API key out of an auth.json, or None."""
    try:
        with open(os.path.expanduser(path)) as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = blob.get("opencode-go") if isinstance(blob, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    return key or None


def _parse_iso_epoch(text):
    """'2026-10-19T05:04:28.000Z' -> epoch seconds. None if unparseable."""
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        return None
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def _normalise_go_window(raw):
    """The endpoint reports an exhaustion `status`; percent is *used*."""
    if not isinstance(raw, dict) or raw.get("percent") is None:
        return None
    return {
        "used_percent": float(raw["percent"]),
        "status": raw.get("status"),
        "resets_at": _parse_iso_epoch(raw.get("resetsAt")),
    }


def _fetch_go_usage(key):
    """GET the Go subscription quota. Raises on any failure."""
    request = urllib.request.Request(
        OPENCODE_GO_USAGE_URL,
        headers={**_ZEN_HEADERS, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.load(response)


def collect_opencode_go(paths):
    """Subscription windows, trying each credential source in turn.

    `paths` is (source-name, auth.json path) pairs.  OpenCode's stored key is
    frequently the expired one while pi's still works, because the two share
    the same subscription, so a rejection is not fatal — keep trying, and
    report which source answered.
    """
    result = {"available": False, "windows": None, "key_source": None, "error": None}
    reasons = []

    for source, path in paths:
        key = _credential_key(path)
        if not key:
            continue
        try:
            payload = _fetch_go_usage(key)
        except urllib.error.HTTPError as exc:
            code = _http_error_code(exc)
            reasons.append(f"{source}: HTTP {exc.code}{' ' + code if code else ''}")
            continue
        except (OSError, ValueError) as exc:
            reasons.append(f"{source}: {exc}")
            continue

        usage = payload.get("usage") if isinstance(payload, dict) else None
        windows = {}
        for name in GO_WINDOWS:
            window = _normalise_go_window((usage or {}).get(name))
            if window:
                windows[name] = window
        if not windows:
            reasons.append(f"{source}: reply held no usable windows")
            continue

        result["windows"] = windows
        result["key_source"] = source
        result["available"] = True
        return result

    result["error"] = "; ".join(reasons) or "no OpenCode Go API key found"
    return result


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

_SAMPLE_REPLY = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "planType": "plus",
        "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1792387110},
        "secondary": {"usedPercent": 52, "windowDurationMins": 10080, "resetsAt": 1792880817},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "planType": "plus",
            "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1792387110},
            "secondary": {"usedPercent": 52, "windowDurationMins": 10080, "resetsAt": 1792880817},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        }
    },
    "rateLimitResetCredits": {"availableCount": 3, "credits": []},
    "accountId": "8bd1cc4c-6fa3-4109-99c7-3ff2c2a40a09",
}


def _self_test():
    rl = _normalise_rate_limits(_SAMPLE_REPLY)
    assert rl is not None, rl
    assert rl["limit_id"] == "codex", rl
    assert rl["plan_type"] == "plus", rl
    assert rl["primary"]["used_percent"] == 30.0, rl
    assert rl["primary"]["window_minutes"] == 300, rl
    assert rl["secondary"]["used_percent"] == 52.0, rl
    assert rl["secondary"]["window_minutes"] == 10080, rl
    assert rl["reset_credits_available"] == 3, rl

    # Degrade rather than raise: empty reply, unusable binary.
    assert _normalise_rate_limits({}) is None
    assert _normalise_window(None) is None
    assert _normalise_window({"windowDurationMins": 300}) is None
    assert collect_codex(codex_bin="/nonexistent/codex")["available"] is False

    assert _find_codex_binary("/bin/true") == "/bin/true"
    assert _find_codex_binary("/nonexistent/codex") is None

    # Spawning must put the binary's own directory on PATH, otherwise the npm
    # shim cannot resolve `#!/usr/bin/env node` under the shell's systemd PATH.
    assert _codex_env("/opt/x/codex")["PATH"].startswith("/opt/x" + os.pathsep)

    # Go quota: percent is *used*, status marks exhaustion, times are ISO.
    assert _parse_iso_epoch("2026-10-19T05:04:28.000Z") == 1792386268
    assert _parse_iso_epoch(None) is None
    assert _parse_iso_epoch("not a date") is None
    window = _normalise_go_window({"status": "rate-limited", "percent": 100,
                                   "resetsAt": "2026-11-10T17:44:56.000Z"})
    assert window["used_percent"] == 100.0, window
    assert window["status"] == "rate-limited", window
    assert window["resets_at"] == 1794332696, window  # 2026-11-10T17:44:56Z
    assert _normalise_go_window({"status": "ok"}) is None

    # No key anywhere: degrade, never raise.
    no_key = collect_opencode_go([("opencode", "/nonexistent"), ("pi", "/nonexistent")])
    assert no_key["available"] is False, no_key
    assert "no OpenCode Go API key" in no_key["error"], no_key

    print("collector self-test OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN"))
    parser.add_argument(
        "--opencode-auth",
        default=os.environ.get("OPENCODE_AUTH", OPENCODE_AUTH_DEFAULT),
    )
    parser.add_argument("--pi-auth", default=os.environ.get("PI_AUTH", PI_AUTH_DEFAULT))
    parser.add_argument("--offline", action="store_true", help="skip the Go quota request")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return

    if args.offline:
        go = {"available": False, "error": "offline mode"}
    else:
        go = collect_opencode_go([
            ("opencode", os.path.expanduser(args.opencode_auth)),
            ("pi", os.path.expanduser(args.pi_auth)),
        ])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "codex": collect_codex(args.codex_bin),
        "opencode": {"go": go},
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
