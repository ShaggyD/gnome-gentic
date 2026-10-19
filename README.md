# Harness Usage

GNOME Shell extension that shows AI coding harness **subscription usage** in
the top panel. Read from local harness state, plus one quota lookup against
OpenCode's own API.

| Source | What you get |
|---|---|
| **Codex CLI** — `codex app-server --stdio` | 5-hour + weekly rate-limit windows (% remaining, reset countdown), plan type, banked reset credits |
| **OpenCode Go** — `GET opencode.ai/zen/go/v1/usage` | rolling / weekly / monthly subscription quota (% remaining) |

The panel shows each harness's brand mark beside the **remaining** percentage
of its tightest window (`[codex] 36% · [opencode] 0%`). The tightest window is
the one that actually blocks you, so that is the number worth surfacing.
Signals are always `used_percent` internally; the UI converts to "left" at
render time.

Click for a per-window bar, each with a reset countdown *and* the wall-clock
moment it resets:

```
Codex (ChatGPT plan)                      ▾
  5h window                          36% left
  ▓▓▓▓▓▓▓░░░░░░░░░░░░░░░
  resets in 2h 54m (23:18)
  weekly window                      43% left
  ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░
  resets in 5d 20h (Oct 24, 16:26)
  plan plus · 3 reset credits
──────────────────────────────────────────
OpenCode Go quota                         ▾
  5h window                          49% left
  ▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░
  resets in 2h 33m (23:04)
  weekly window                      80% left
  resets in 6d 21h (Oct 25, 18:00)
  monthly window                    0% left
  rate-limited · resets in 22d 15h (Nov 10, 10:44)
  Read with pi credentials (OpenCode key rejected)
```

Both sections are always open — there is no collapsing. A window with no
number hides its row (e.g. no weekly bucket), but the section heading and the
error line are never hidden, so an absent reading still says why it is absent.

## Requirements

| | |
|---|---|
| GNOME Shell | 45 – 50, Wayland or X11 |
| `python3` | 3.8+ — **stdlib only**, nothing to `pip install` |
| `make` | optional, wraps the install/pack/uninstall commands |
| a harness | the `codex` CLI and/or an OpenCode sign-in, otherwise there is nothing to read |

The only network call is the OpenCode Go quota lookup. Codex numbers come
from the local CLI, so they work offline.

## Setup and install

```sh
make install
```

That copies files into `~/.local/share/gnome-shell/extensions/harness-usage@dchadwick.local/`.
Then restart GNOME Shell — on Wayland that means **log out and back in**, since
the shell is the compositor (see [Developing](#developing) for why no in-place
reload exists) — and enable the extension:

```sh
gnome-extensions enable harness-usage@dchadwick.local
```

Confirm it works without touching the shell — the collector runs standalone:

```sh
make check
python3 harness-usage@dchadwick.local/collector.py | python3 -m json.tool
```

The second command prints exactly the JSON the panel renders, so a wrong
number is diagnosable from a terminal.

If a harness shows nothing, the menu says why (e.g. `could not find the codex
CLI (set CODEX_BIN)`) rather than hiding the row — check there first.

## Uninstall

```sh
make uninstall
```

## Configuration

Defaults are deliberately hardcoded — there is no prefs window and no
GSettings schema, because the values below cover normal use.

| Setting | How |
|---|---|
| `REFRESH_SECONDS` (300) | constant at the top of `extension.js`; needs a shell restart |
| `CODEX_BIN` | env var, or `--codex-bin`, when the CLI is somewhere unusual |
| `OPENCODE_AUTH`, `PI_AUTH` | env vars, or `--opencode-auth` / `--pi-auth`, to point at different credential files |
| `--offline` | skip the one network call (Codex still reports) |

Make targets:

| Target | Does |
|---|---|
| `make install` | copy into the extensions directory |
| `make uninstall` | delete it from there |
| `make check` | collector self-test + JSON validation |
| `make pack` | build a distributable `.shell-extension.zip` |

## Developing

`collector.py` is spawned fresh on every refresh, so **edits to it take effect
immediately** — no reload of any kind. Only `extension.js` needs a restart.

On Wayland, that restart must be a log out / log in. GNOME Shell 50 caches
extension JS as an ES module and offers no way to re-read it at runtime:

| Approach | Result |
|---|---|
| `gnome-extensions disable/enable` | reuses cached module (verified with a marker string) |
| `Extensions.ReloadExtension` D-Bus | not implemented on GNOME 50 |
| `Eval` + `global.reexec_self()` | `Eval` is patched off by Ubuntu (`development-tools` is `true` yet Eval returns `false`) |
| `gnome-shell --replace` | unsupported on Wayland — the shell *is* the compositor |
| new UUID (new module URL) | Shell doesn't rescan the extensions dir at runtime |

For fast iteration, run a nested shell instead:

```sh
dbus-run-session -- gnome-shell --nested --wayland
```

Most logic is deliberately outside `extension.js` so it can be exercised
without the Shell: `make check` covers the collector, and the render
functions take plain data and only touch `St`/`PopupMenu` in `_buildMenu`.

### Icons

`icons/codex.symbolic.png` and `icons/opencode.symbolic.png` are the real
brand marks, not hand-drawn:

| Icon | Origin |
|---|---|
| `codex.symbolic.png` | `cdn.oaistatic.com/assets/favicon-*.ico` — OpenAI's own CDN |
| `opencode.symbolic.png` | `opencode.ai/apple-touch-icon.png` — official mark |

Both are 48x48 (scaled to panel size by the shell) and hold **black pixels
with an alpha mask**, which is a specific requirement, not a style choice:

* **The `.symbolic.png` suffix is what enables tinting.** For a file-based
  `GIcon`, `st_icon_info_is_symbolic()` tests the file *URI* against
  `.symbolic.png` (`icon_uri_is_symbolic`, `src/st/st-icon-theme.c`). Name it
  `codex.png` and the shell leaves it untinted — which is why an earlier
  version rendered as a dark blob on a dark panel.
* **The pixels must be pure black.** In `color_symbolic_pixbuf()` a pixel
  that is black with alpha > 0 is replaced by the panel foreground colour;
  any other colour is blended against the success/warning/error palette and
  renders as garbage. So white artwork would be broken here, despite looking
  right in an image viewer.

Together those two make each mark follow the panel's text colour — white on a
dark panel, dark on a light one — with no runtime logic and no hardcoded
colour. OpenAI's asset is already a black mark on transparency; OpenCode's
ships a light glyph on a near-black tile, so the tile is dropped and the
glyph becomes the mask.

Regenerate either from source:

```sh
python3 tools/make-icons.py
```

The script asserts both invariants (black pixels, plausible alpha coverage)
so this class of bug fails loudly instead of shipping.

## How it works

`collector.py` prints one JSON document on stdout; `extension.js` only
renders it. Each source is independent and degrades on its own, so one broken
harness never blanks the other.

Overrides via environment (`CODEX_BIN`, `OPENCODE_AUTH`, `PI_AUTH`) or the
matching `--codex-bin`, `--opencode-auth`, `--pi-auth` flags. `--offline`
skips the single network call.

### Codex (0.16x) detail

Modern Codex keeps no rollout JSONL — state lives in SQLite under `~/.codex`.
The subscription windows come from the CLI's app server, reached with a
single JSON-RPC round trip:

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"x","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}' \
  | codex app-server --stdio
```

`account/rateLimits/read` returns the same 5-hour / weekly windows the TUI's
`/status` screen shows, plus the plan type and any banked reset credits.

Two details are load-bearing:

* **`--stdio` means no daemon.** The app server can also run as a long-lived
  daemon behind a unix socket, but that process lives inside the session and
  does not survive a logout. `--stdio` starts a server per call and reaps it,
  so nothing needs to be running beforehand. (An earlier version talked
  WebSocket to that daemon and needed ~170 extra lines plus a
  `daemon start` after every relogin. Both are gone.)
* **stdin must stay open.** The server treats EOF as shutdown and exits
  before answering — `subprocess.communicate()` returns zero bytes — so the
  collector selects on stdout instead of piping and closing.

The binary is located explicitly, because the extension runs under
**gnome-shell's** environment, whose PATH is systemd's user default: no npm
shim directory and no `node`. A plain `which codex` fails there. Lookup order
is `CODEX_BIN`, then `PATH`, then known install paths, preferring the daemon
package's standalone ELF over the npm `#!/usr/bin/env node` shim. The shim
still works when used, because spawning puts the binary's own directory on
PATH so it can find its interpreter.

There is no local fallback for these numbers. `~/.codex/logs_2.sqlite`
records the `account/rateLimits/read` *request* but never its response.

Useful for debugging:

```sh
codex app-server generate-json-schema --out /tmp/codex-schema --experimental
```

### OpenCode Go quota detail

Subscription quota comes from `GET https://opencode.ai/zen/go/v1/usage`,
authenticated with the account's API key as a bearer token. It returns three
windows, each a *used* percentage with a `status` and a reset time:

```json
{"usage": {
  "rolling": {"status": "ok",           "percent": 51,  "resetsAt": "2026-10-19T05:04:28.000Z"},
  "weekly":  {"status": "ok",           "percent": 20,  "resetsAt": "2026-10-26T00:00:00.000Z"},
  "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-11-10T17:44:56.000Z"}}}
```

Two things that are easy to get wrong, both learned from live responses:

* **Cloudflare rejects unknown clients.** A default Python user agent gets
  `403 error 1010 browser_signature_banned`, so the collector identifies as
  the OpenCode CLI. A plain `curl` against the endpoint will not reproduce
  the call.
* **OpenCode's stored key is often expired.** `~/.local/share/opencode/auth.json`
  can hold a key the API answers with `401 api_key_expired` while pi's key
  still works, because pi and OpenCode share the same subscription. The
  collector tries OpenCode's key first and falls back to
  `~/.pi/agent/auth.json`, and the menu reports which one succeeded — so a
  rejection is not fatal, it just moves on to the next source. If every
  source fails, the menu names each one's reason rather than hiding them.

A window at `percent: 100` / `status: rate-limited` is exhausted for its
period and renders as `rate-limited · resets in …`.

## Removed: collapsible section headings

Section headings were briefly clickable to fold their rows away. They are
plain labels again.

This is worth recording because the reason was not obvious. A heading built as
an ordinary `PopupMenu.PopupBaseMenuItem` **does** toggle its section, and then
the dropdown closes on the same click. `PopupMenuSection._connectItemSignals()`
connects every added item's `'activate'` signal with `ConnectFlags.AFTER` and
calls `itemActivated()`, which runs `this._getTopMenu().close(animate)`. So a
clickable heading can never merely fold a section — the click also dismisses
the menu, which reads as "the toggles are broken".

The workaround tried was an inert item (`activate: false`, so no
`Clutter.ClickGesture` and no `'activate'` ever emitted, plus
`remove_style_class_name('popup-inactive-menu-item')` to undo the insensitive
styling the shell applies to inert items) with a nested `St.Button` owning the
click. **This did not work.** It was loaded into a live shell (extension
installed 20:45:34, shell restarted 20:48:15) and the headings still did not
respond to clicks; the journal shows no JS error from the extension, so it
failed silently rather than throwing. Do not treat that approach as
known-good — a nested `St.Button` inside a `PopupBaseMenuItem` appears
not to receive the click at all, and that was not pursued further.

## Removed: OpenCode local usage

There used to be a second OpenCode section reporting **local** token/cost
history, read from `~/.local/share/opencode/opencode.db` (table `session`:
`model`, `cost`, `tokens_input`/`output`/`reasoning`, `time_created`),
aggregated into last-5h / today / this-week / all-time totals and top models.
It is gone — that database had been stale since March, so it only ever showed
`$0.00` — while the **subscription** readings above were kept. `--opencode-db`
and `OPENCODE_DB` went with it; `--opencode-auth` did not, because the Go
quota still needs a key.

## License

GPL-3.0 — see [LICENSE](LICENSE).
