// Harness Usage — GNOME Shell extension
//
// Shows subscription usage of local AI coding harnesses in the top panel:
//   * Codex CLI    : 5-hour / weekly rate-limit windows, % remaining
//   * OpenCode Go  : rolling / weekly / monthly quota, % remaining
//   * OpenCode     : token + cost usage from its local SQLite database
//
// Percentages are always shown as what is LEFT, not what is spent. All
// data comes from the bundled collector.py run as a short-lived
// subprocess; the only network call is the OpenCode Go quota lookup, and
// it degrades to local-only when unavailable.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// ponytail: settings deferred (no gschema/prefs). Edit + restart shell if needed.
const REFRESH_SECONDS = 300;

// OpenCode Go quota windows, in the order the endpoint reports them
// ("rolling" is the 5-hour window the console calls Rolling).
const GO_WINDOWS = [
    ['rolling', '5h window'],
    ['weekly', 'weekly window'],
    ['monthly', 'monthly window'],
];

// Windows report what is *spent*; the UI always shows what is *left*.
const pctLeft = win => win?.used_percent == null
    ? null : Math.max(0, 100 - win.used_percent);

// The tightest window is the one that actually blocks you, so that is the
// number worth putting in the panel.
const minLeft = windows => {
    const values = windows.map(pctLeft).filter(v => v != null);
    return values.length ? Math.min(...values) : null;
};

const fmtPct = left => left == null ? '–' : `${Math.round(left)}%`;

function fmtWindow(minutes) {
    if (minutes == null) return 'window';
    if (minutes === 10080) return 'weekly';
    if (minutes % 60 === 0) return `${minutes / 60}h`;
    return `${minutes}m`;
}

function fmtCountdown(epochSeconds) {
    if (!epochSeconds) return '';
    const delta = epochSeconds - Math.floor(Date.now() / 1000);
    if (delta <= 0) return 'resets soon';
    const d = Math.floor(delta / 86400);
    const h = Math.floor((delta % 86400) / 3600);
    const m = Math.ceil((delta % 3600) / 60);
    if (d > 0) return `resets in ${d}d ${h}h`;
    if (h > 0) return `resets in ${h}h ${m}m`;
    return `resets in ${m}m`;
}

// Wall-clock moment of the reset, so a long window reads as a date rather
// than only a countdown ("22d 16h" is hard to place).
function fmtResetAt(epochSeconds) {
    if (!epochSeconds) return '';
    const when = GLib.DateTime.new_from_unix_local(epochSeconds);
    if (!when) return '';
    const now = GLib.DateTime.new_now_local();
    if (when.get_year() !== now.get_year()) return when.format('%b %d %Y');
    if (when.get_day_of_year() === now.get_day_of_year()) return when.format('%H:%M');
    return when.format('%b %d, %H:%M');
}


// Pump a subprocess stream to EOF, one line at a time (async, never blocks
// the shell). GJS 1.88's communicate_utf8_async returns null streams, and
// pipes must be drained while the process runs, so read them directly.
function readLinesToEof(stream, sink, done) {
    const dis = new Gio.DataInputStream({base_stream: stream});
    const pump = () => {
        dis.read_line_async(GLib.PRIORITY_DEFAULT, null, (src, res) => {
            let line = null;
            try {
                [line] = src.read_line_finish_utf8(res);
            } catch (e) {
                logError(e, 'harness-usage: stream read failed');
            }
            if (line === null) {
                done();
                return;
            }
            sink.push(line);
            pump();
        });
    };
    pump();
}


// Small cairo-drawn progress bar. It shows what is LEFT, so a full green
// bar means headroom and a shrinking bar means spend: >= 50% left green,
// >= 20% amber, below that red.
const UsageBar = GObject.registerClass(
class UsageBar extends St.DrawingArea {
    _init() {
        super._init();
        this._fraction = 0;
        this.set_size(220, 10);
    }

    set fraction(value) {
        this._fraction = Math.max(0, Math.min(1, value ?? 0));
        this.queue_repaint();
    }

    vfunc_repaint() {
        const [width, height] = this.get_surface_size();
        const cr = this.get_context();
        const r = height / 2;
        const pill = (x, y, w, h) => {
            cr.newSubPath();
            cr.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
            cr.arc(x + w - r, y + h - r, r, 0, Math.PI);
            cr.arc(x + r, y + h - r, r, Math.PI / 2, 3 * Math.PI / 2);
            cr.arc(x + r, y + r, r, Math.PI, 3 * Math.PI / 2);
            cr.closePath();
        };

        cr.setSourceRGBA(1, 1, 1, 0.14);
        pill(0, 0, width, height);
        cr.fill();

        if (this._fraction > 0) {
            if (this._fraction >= 0.5) cr.setSourceRGBA(0.35, 0.82, 0.42, 0.95);
            else if (this._fraction >= 0.2) cr.setSourceRGBA(0.96, 0.72, 0.20, 0.95);
            else cr.setSourceRGBA(0.94, 0.33, 0.31, 0.95);
            pill(0, 0, Math.max(height, width * this._fraction), height);
            cr.fill();
        }
        cr.$dispose();
    }
});


const HarnessUsageIndicator = GObject.registerClass(
class HarnessUsageIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Harness Usage');
        this._extension = extension;
        this._data = null;
        this._proc = null;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        // One icon per harness so the two percentages in the panel are
        // self-identifying instead of an unlabelled pair of numbers.  The
        // files are named `*.symbolic.png`, which is what makes the shell
        // tint them with the panel foreground colour (white on a dark panel,
        // dark on a light one) — see tools/make-icons.py.
        const panelIcon = name => new St.Icon({
            gicon: Gio.icon_new_for_string(
                GLib.build_filenamev([this._extension.path, 'icons', `${name}.symbolic.png`])),
            style_class: 'system-status-icon',
            y_align: Clutter.ActorAlign.CENTER,
        });
        const panelValue = () => new St.Label({
            text: '–', y_expand: true, y_align: Clutter.ActorAlign.CENTER,
        });
        this._cxIcon = panelIcon('codex');
        this._cxValue = panelValue();
        this._ocIcon = panelIcon('opencode');
        this._ocValue = panelValue();
        const gap = new St.Label({
            text: '·', y_align: Clutter.ActorAlign.CENTER, style_class: 'usage-panel-gap',
        });
        box.add_child(this._cxIcon);
        box.add_child(this._cxValue);
        box.add_child(gap);
        box.add_child(this._ocIcon);
        box.add_child(this._ocValue);
        this.add_child(box);

        this._buildMenu();
        this.menu.connect('open-state-changed', (_m, open) => open && this.refresh());

        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECONDS,
            () => { this.refresh(); return GLib.SOURCE_CONTINUE; });
        // Re-render countdown strings once a minute without re-collecting.
        this._tick = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 60,
            () => { this._render(); return GLib.SOURCE_CONTINUE; });
        this.refresh();
    }

    _buildMenu() {
        this._info = text => {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
            const label = new St.Label({text, style_class: 'usage-info'});
            item.add_child(label);
            item._label = label;
            return item;
        };
        const barRow = () => {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
            const vbox = new St.BoxLayout({vertical: true, style_class: 'usage-bar-row'});
            const top = new St.BoxLayout({x_expand: true});
            const title = new St.Label({style_class: 'usage-bar-title', x_expand: true});
            const value = new St.Label({style_class: 'usage-bar-value'});
            top.add_child(title);
            top.add_child(value);
            const bar = new UsageBar();
            const detail = new St.Label({style_class: 'usage-bar-detail'});
            vbox.add_child(top);
            vbox.add_child(bar);
            vbox.add_child(detail);
            item.add_child(vbox);
            return {item, title, value, bar, detail};
        };

        const header = text => {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
            item.add_child(new St.Label({text, style_class: 'usage-header'}));
            return item;
        };

        this.menu.addMenuItem(header('Codex (ChatGPT plan)'));
        this._cxPrimary = barRow();
        this._cxSecondary = barRow();
        this._cxInfo = this._info('');
        this.menu.addMenuItem(this._cxPrimary.item);
        this.menu.addMenuItem(this._cxSecondary.item);
        this.menu.addMenuItem(this._cxInfo);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(header('OpenCode Go quota'));
        this._ocRows = GO_WINDOWS.map(([key, label]) => ({key, label, row: barRow()}));
        for (const {row} of this._ocRows)
            this.menu.addMenuItem(row.item);
        this._ocInfo = this._info('');
        this.menu.addMenuItem(this._ocInfo);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._updated = this._info('No data collected yet');
        this.menu.addMenuItem(this._updated);
        const refresh = new PopupMenu.PopupMenuItem('Refresh now');
        refresh.connect('activate', () => this.refresh());
        this.menu.addMenuItem(refresh);
    }

    refresh() {
        if (this._proc) return; // collection already running
        const argv = ['python3', GLib.build_filenamev([this._extension.path, 'collector.py'])];
        let proc;
        try {
            proc = Gio.Subprocess.new(argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            logError(e, 'harness-usage: failed to spawn collector');
            this._updated._label.text = 'Error: could not run collector (python3 missing?)';
            return;
        }
        this._proc = proc;

        const out = [], err = [];
        let pending = 2;
        const finish = () => {
            if (--pending > 0)
                return;
            proc.wait_check_async(null, (p, res) => {
                let ok = false;
                try {
                    ok = p.wait_check_finish(res);
                } catch (e) {
                    logError(e, 'harness-usage: collector exited non-zero');
                }
                this._proc = null;
                if (ok) {
                    try {
                        this._data = JSON.parse(out.join('\n'));
                    } catch (e) {
                        logError(e, 'harness-usage: bad collector JSON');
                    }
                } else {
                    logError(new Error(err.join('\n') || 'collector failed'), 'harness-usage');
                    this._updated._label.text = 'Collector failed — see journal';
                }
                this._render();
            });
        };
        readLinesToEof(proc.get_stdout_pipe(), out, finish);
        readLinesToEof(proc.get_stderr_pipe(), err, finish);
    }

    _render() {
        const d = this._data;
        // The tightest window is the one that actually blocks you, so that
        // is the number worth putting in the panel.
        const cx = d?.codex?.rate_limits;
        this._cxValue.text = fmtPct(cx ? minLeft([cx.primary, cx.secondary]) : null);
        const go = d?.opencode?.go;
        this._ocValue.text = fmtPct(
            go?.available ? minLeft(Object.values(go.windows)) : null);
        if (!d)
            return;
        this._renderCodex(d.codex ?? {});
        this._renderOpenCodeGo(go ?? {});
        this._updated._label.text = `Updated ${GLib.DateTime.new_now_local().format('%H:%M')}`;
    }

    _setWindow(row, win, title) {
        if (!win) {
            row.title.text = title;
            row.value.text = '–';
            row.bar.fraction = 0;
            row.detail.text = '';
            return;
        }
        const left = pctLeft(win);
        row.title.text = title ?? `${fmtWindow(win.window_minutes)} window`;
        row.value.text = `${Math.round(left)}% left`;
        row.bar.fraction = left / 100;
        const bits = [];
        if (win.status && win.status !== 'ok')
            bits.push(win.status);
        const countdown = fmtCountdown(win.resets_at);
        const at = fmtResetAt(win.resets_at);
        if (countdown)
            bits.push(at ? `${countdown} (${at})` : countdown);
        row.detail.text = bits.join(' · ');
    }

    _renderCodex(cx) {
        this._cxPrimary.item.visible = cx.available;
        this._cxSecondary.item.visible = cx.available && !!cx.rate_limits?.secondary;
        if (!cx.available) {
            this._cxInfo._label.text = `Codex: ${cx.error ?? 'no data'}`;
            return;
        }
        const rl = cx.rate_limits;
        this._setWindow(this._cxPrimary, rl?.primary);
        this._setWindow(this._cxSecondary, rl?.secondary);

        const bits = [];
        if (rl?.plan_type)
            bits.push(`plan ${rl.plan_type}`);
        const credits = rl?.reset_credits_available;
        if (credits)
            bits.push(`${credits} reset credit${credits === 1 ? '' : 's'}`);
        this._cxInfo._label.text = bits.join(' · ') || 'Live from codex app-server';
    }

    _renderOpenCodeGo(go) {
        const windows = go.available ? go.windows : null;
        for (const {key, label, row} of this._ocRows) {
            const win = windows?.[key];
            row.item.visible = !!win;
            if (win)
                this._setWindow(row, win, label);
        }
        this._ocInfo.visible = go.available || !!go.error;
        this._ocInfo._label.text = !go.available
            ? (go.error ? `Go quota unavailable: ${go.error}` : '')
            : go.key_source === 'pi'
                ? 'Read with pi credentials (OpenCode key rejected)'
                : 'Via opencode.ai/zen/go/v1/usage';
    }

    destroy() {
        if (this._timer) { GLib.source_remove(this._timer); this._timer = null; }
        if (this._tick) { GLib.source_remove(this._tick); this._tick = null; }
        if (this._proc) { this._proc.force_exit(); this._proc = null; }
        super.destroy();
    }
});


export default class HarnessUsageExtension extends Extension {
    enable() {
        this._indicator = new HarnessUsageIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
