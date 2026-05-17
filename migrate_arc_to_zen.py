#!/usr/bin/env python3
"""
Arc -> Zen migration helper (macOS).

Migrates:
  1. Open + pinned tabs from Arc's StorableSidebar.json -> bookmarks HTML
     you can import into Zen (Bookmarks > Manage > Import HTML).
  2. Browsing history from Arc's Chromium History DB.
       --history-mode html   (default, SAFE)  -> writes a searchable HTML page.
       --history-mode merge  (advanced, RISKY) -> inserts into Zen's places.sqlite.
  3. Passwords: prints GUI instructions (macOS Keychain prevents scripting).

Safety:
  * Both Arc and Zen MUST be fully quit (Cmd+Q) before running.
  * places.sqlite is backed up before any write.
  * Run with --dry-run first to see what will happen.

Usage:
  python3 migrate_arc_to_zen.py                 # safe defaults
  python3 migrate_arc_to_zen.py --dry-run       # preview only
  python3 migrate_arc_to_zen.py --history-mode merge
  python3 migrate_arc_to_zen.py --skip-tabs --skip-history

Tested against Arc 1.x and Zen Browser 1.x on macOS. Verify paths below
match your install before running.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
HOME = Path.home()
ARC_DIR = HOME / "Library" / "Application Support" / "Arc"
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"

ARC_HISTORY_DB = ARC_DIR / "User Data" / "Default" / "History"
ARC_SIDEBAR_JSON_CANDIDATES = [
    ARC_DIR / "StorableSidebar.json",
    ARC_DIR / "StorableSidebar.json.backup",
]

# Chrome timestamp epoch is 1601-01-01 UTC, microseconds.
# Firefox (places.sqlite) uses 1970-01-01 UTC, microseconds.
CHROME_EPOCH_DELTA_US = 11_644_473_600 * 1_000_000

OUT_DIR = HOME / "Desktop" / "arc_zen_migration"

# --------------------------------------------------------------------------- #
# Utils
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)

def ensure_out_dir() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

def is_app_running(app_name: str) -> bool:
    """
    True iff a process from the named .app bundle is currently running.

    We match on the bundle's MacOS executable path rather than the bare process
    name. Bare-name matching is too noisy on macOS: 'Arc' as a substring hits
    SearchAgent, ArchiveService, etc.; even `pgrep -x Arc` can collide with
    unrelated helpers. Bundle-path matching is the precise signal.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", f"/{app_name}.app/Contents/MacOS/"],
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False

def safety_checks() -> None:
    # macOS .app bundle names. Zen ships as "Zen Browser.app".
    apps = ("Arc", "Zen Browser")
    problems = []
    for app in apps:
        if is_app_running(app):
            problems.append(f"  - {app} appears to be running. Quit it (Cmd+Q).")
    if problems:
        log("Refusing to proceed:")
        for p in problems:
            log(p)
        log("\nIf you're sure neither is running, override with --force-running.")
        sys.exit(1)

def backup_file(path: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(path.name + f".bak-{ts}")
    shutil.copy2(path, dst)
    return dst

def find_zen_default_profile() -> Path:
    ini = ZEN_DIR / "profiles.ini"
    if not ini.exists():
        raise FileNotFoundError(
            f"Zen profiles.ini not found at {ini}. Launch Zen at least once first."
        )
    cp = configparser.ConfigParser()
    cp.read(ini)

    # 1) Prefer the Install*.Default key (Firefox-style).
    default_rel_path = None
    for section in cp.sections():
        if section.startswith("Install"):
            default_rel_path = cp[section].get("Default")
            if default_rel_path:
                break

    # 2) Fall back to a Profile section with Default=1.
    if not default_rel_path:
        for section in cp.sections():
            if section.startswith("Profile") and cp[section].get("Default", "0") == "1":
                default_rel_path = cp[section].get("Path", "")
                break

    # 3) Last resort: first Profile section.
    if not default_rel_path:
        for section in cp.sections():
            if section.startswith("Profile"):
                default_rel_path = cp[section].get("Path", "")
                break

    if not default_rel_path:
        raise RuntimeError("Could not determine Zen default profile path.")

    profile = (ZEN_DIR / default_rel_path).resolve()
    if not profile.exists():
        raise FileNotFoundError(f"Zen profile dir not found: {profile}")
    return profile

# --------------------------------------------------------------------------- #
# Tabs / pinned -> bookmarks HTML
# --------------------------------------------------------------------------- #
def find_arc_sidebar_json() -> Path | None:
    for cand in ARC_SIDEBAR_JSON_CANDIDATES:
        if cand.exists():
            return cand
    return None

def extract_tabs_from_sidebar(data) -> list[tuple[str, str, str]]:
    """
    Walk Arc's sidebar JSON and pull (space_name, title, url) tuples.

    Arc's schema has shifted over versions; this is best-effort: any dict node
    that exposes a URL gets captured. Space association is also best-effort
    (uses the nearest enclosing object with a 'title' that looks like a Space).
    """
    found: list[tuple[str, str, str]] = []

    def best_url(obj: dict) -> str | None:
        for key in ("savedURL", "url", "storedURL"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://", "file://")):
                return v
        return None

    def best_title(obj: dict, url: str) -> str:
        for key in ("title", "savedTitle", "name"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return url

    def walk(obj, space: str = "Arc"):
        if isinstance(obj, dict):
            url = best_url(obj)
            if url:
                found.append((space, best_title(obj, url), url))
            # Heuristic: if this dict looks like a Space, use its title for children.
            new_space = space
            if "spaceItems" in obj or "containerIDs" in obj:
                t = obj.get("title")
                if isinstance(t, str) and t.strip():
                    new_space = t.strip()
            for v in obj.values():
                walk(v, new_space)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, space)

    walk(data)

    # Dedupe by URL, keep first occurrence.
    seen = set()
    unique = []
    for space, title, url in found:
        if url in seen:
            continue
        seen.add(url)
        unique.append((space, title, url))
    return unique

def write_bookmarks_html(tabs: list[tuple[str, str, str]], out_path: Path) -> None:
    # Group by space.
    by_space: dict[str, list[tuple[str, str]]] = {}
    for space, title, url in tabs:
        by_space.setdefault(space, []).append((title, url))

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))

    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
        "    <DT><H3>Arc Tabs (migrated)</H3>",
        "    <DL><p>",
    ]
    for space, items in by_space.items():
        lines.append(f"        <DT><H3>{esc(space)}</H3>")
        lines.append("        <DL><p>")
        for title, url in items:
            lines.append(f'            <DT><A HREF="{esc(url)}">{esc(title)}</A>')
        lines.append("        </DL><p>")
    lines.append("    </DL><p>")
    lines.append("</DL><p>")
    out_path.write_text("\n".join(lines), encoding="utf-8")

def migrate_tabs(dry_run: bool) -> None:
    sidebar = find_arc_sidebar_json()
    if not sidebar:
        log("  [tabs] No StorableSidebar.json found under Arc. Skipping.")
        return
    log(f"  [tabs] Reading {sidebar}")
    data = json.loads(sidebar.read_text(encoding="utf-8"))
    tabs = extract_tabs_from_sidebar(data)
    log(f"  [tabs] Found {len(tabs)} unique URLs.")
    if dry_run:
        log("  [tabs] (dry-run) Skipping write.")
        return
    ensure_out_dir()
    out = OUT_DIR / "arc_tabs_for_zen.html"
    write_bookmarks_html(tabs, out)
    log(f"  [tabs] Wrote {out}")
    log("         In Zen: Bookmarks menu > Manage Bookmarks > Import and Backup > Import Bookmarks from HTML.")

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def read_arc_history() -> list[tuple[str, str, int, int]]:
    """Returns list of (url, title, visit_time_chrome_us, transition)."""
    if not ARC_HISTORY_DB.exists():
        raise FileNotFoundError(f"Arc history DB not found: {ARC_HISTORY_DB}")
    # Copy to /tmp because the live DB can be locked or WAL-pending.
    tmp = Path("/tmp") / f"arc_history_{os.getpid()}.sqlite"
    shutil.copy2(ARC_HISTORY_DB, tmp)
    try:
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            SELECT u.url, u.title, v.visit_time, v.transition
            FROM visits v
            JOIN urls u ON u.id = v.url
            ORDER BY v.visit_time DESC
            """
        )
        rows = [(r["url"], r["title"] or "", r["visit_time"], r["transition"]) for r in cur.fetchall()]
        con.close()
        return rows
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

def chrome_us_to_dt(chrome_us: int) -> datetime.datetime:
    unix_us = chrome_us - CHROME_EPOCH_DELTA_US
    return datetime.datetime.fromtimestamp(unix_us / 1_000_000)

def write_history_html(rows, out_path: Path) -> None:
    """Single-file, searchable HTML log of Arc history. No DB risk."""
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))

    html_head = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Arc history (exported)</title>
<style>
body{font:14px/1.4 -apple-system, system-ui, sans-serif; margin:24px; max-width:1100px}
input{width:100%; padding:8px; font-size:14px; margin-bottom:12px}
table{border-collapse:collapse; width:100%}
th,td{border-bottom:1px solid #ddd; padding:6px 8px; vertical-align:top}
th{text-align:left; background:#f5f5f5; position:sticky; top:0}
td.date{white-space:nowrap; color:#666; font-variant-numeric:tabular-nums}
a{color:#0a58ca; text-decoration:none}
a:hover{text-decoration:underline}
</style></head><body>
<h1>Arc browsing history (exported)</h1>
<p>%COUNT% visits. Type to filter by title or URL.</p>
<input id="q" placeholder="Filter…">
<table id="t">
<thead><tr><th>When</th><th>Title</th><th>URL</th></tr></thead>
<tbody>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(html_head.replace("%COUNT%", str(len(rows))))
        for url, title, chrome_us, _trans in rows:
            try:
                dt = chrome_us_to_dt(chrome_us).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = ""
            f.write(
                f'<tr><td class="date">{esc(dt)}</td>'
                f'<td>{esc(title)}</td>'
                f'<td><a href="{esc(url)}">{esc(url)}</a></td></tr>\n'
            )
        f.write("""</tbody></table>
<script>
const q=document.getElementById('q'),rows=document.querySelectorAll('#t tbody tr');
q.addEventListener('input',()=>{const v=q.value.toLowerCase();
  rows.forEach(r=>{r.style.display=r.innerText.toLowerCase().includes(v)?'':'none'})});
</script>
</body></html>
""")

def merge_history_into_zen(rows, dry_run: bool) -> None:
    """
    Advanced: inserts records into Zen's places.sqlite.

    Caveats:
      * url_hash is left at 0. Imported visits appear in history search but
        URL-bar autocomplete may not surface them until Firefox rebuilds.
      * frecency is set to -1 so Firefox recomputes lazily.
      * Both browsers MUST be closed (places.sqlite is exclusive-locked when running).
    """
    profile = find_zen_default_profile()
    places = profile / "places.sqlite"
    if not places.exists():
        raise FileNotFoundError(f"places.sqlite not found in {profile}")

    log(f"  [history] Zen profile: {profile}")
    if dry_run:
        log(f"  [history] (dry-run) Would insert {len(rows)} visits into {places}")
        return

    bkp = backup_file(places)
    # Also back up the WAL/SHM siblings if present.
    for sib in ("places.sqlite-wal", "places.sqlite-shm"):
        sp = profile / sib
        if sp.exists():
            backup_file(sp)
    log(f"  [history] Backed up places.sqlite -> {bkp.name}")

    con = sqlite3.connect(places)
    cur = con.cursor()
    inserted_places = 0
    inserted_visits = 0
    skipped = 0
    for url, title, chrome_us, _trans in rows:
        ff_us = chrome_us - CHROME_EPOCH_DELTA_US
        if ff_us <= 0:
            skipped += 1
            continue
        # rev_host: Firefox stores reversed host + '.'  e.g. example.com -> moc.elpmaxe.
        rev_host = ""
        try:
            host = url.split("/", 3)[2]
            rev_host = host[::-1] + "."
        except Exception:
            pass
        guid = uuid.uuid4().hex[:12]
        try:
            cur.execute(
                "INSERT INTO moz_places (url, title, rev_host, frecency, guid, url_hash) "
                "VALUES (?, ?, ?, -1, ?, 0)",
                (url, title, rev_host, guid),
            )
            place_id = cur.lastrowid
            inserted_places += 1
        except sqlite3.IntegrityError:
            row = cur.execute("SELECT id FROM moz_places WHERE url = ?", (url,)).fetchone()
            if not row:
                skipped += 1
                continue
            place_id = row[0]
        cur.execute(
            "INSERT INTO moz_historyvisits (place_id, visit_date, visit_type, session) "
            "VALUES (?, ?, 1, 0)",
            (place_id, ff_us),
        )
        inserted_visits += 1
    con.commit()
    con.close()
    log(f"  [history] +{inserted_places} URLs, +{inserted_visits} visits, skipped {skipped}.")
    log("  [history] Note: URL-bar autocomplete may not surface imported items until")
    log("            Firefox/Zen rebuilds its hash index on next idle-maintenance.")

def migrate_history(mode: str, dry_run: bool) -> None:
    if not ARC_HISTORY_DB.exists():
        log("  [history] Arc history DB not found. Skipping.")
        return
    log(f"  [history] Reading {ARC_HISTORY_DB}")
    rows = read_arc_history()
    log(f"  [history] {len(rows)} visits found in Arc.")
    if mode == "html":
        if dry_run:
            log("  [history] (dry-run) Would write HTML export.")
            return
        ensure_out_dir()
        out = OUT_DIR / "arc_history.html"
        write_history_html(rows, out)
        log(f"  [history] Wrote {out}")
        log("            Open it in any browser; it has a built-in filter box.")
    elif mode == "merge":
        merge_history_into_zen(rows, dry_run=dry_run)
    else:
        raise ValueError(f"Unknown history-mode: {mode}")

# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
PASSWORDS_INSTRUCTIONS = """
  Passwords cannot be migrated by a script — macOS Keychain protects them by design.
  GUI steps (5 minutes):

    1. Open Arc -> address bar -> arc://password-manager/settings
       (or Settings -> Profiles -> Manage Profile).
    2. Click Export passwords. Authenticate with Touch ID / your Mac password.
       Save the CSV somewhere temporary, e.g. ~/Desktop/passwords.csv
    3. Open Zen -> address bar -> about:logins
    4. Click the three-dot menu (top right) -> Import from a File...
       Select your CSV. Confirm.
    5. SECURITY: Immediately delete the CSV (it is plaintext passwords) and
       empty the Trash:
         rm ~/Desktop/passwords.csv && osascript -e 'tell app "Finder" to empty trash'
"""

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Arc -> Zen migration helper.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen, write nothing.")
    parser.add_argument("--skip-tabs", action="store_true")
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--skip-passwords", action="store_true")
    parser.add_argument("--history-mode", choices=("html", "merge"), default="html",
                        help="html = safe export; merge = insert into places.sqlite.")
    parser.add_argument("--force-running", action="store_true",
                        help="Skip the browser-running guard. Only use if you're certain.")
    args = parser.parse_args()

    log("Arc -> Zen migration")
    log("=" * 50)
    if not args.force_running:
        safety_checks()
    else:
        log("(--force-running: skipping browser-running check)")

    if not args.skip_tabs:
        log("\n[1] Tabs and pinned URLs")
        migrate_tabs(args.dry_run)

    if not args.skip_history:
        log(f"\n[2] History (mode = {args.history_mode})")
        migrate_history(args.history_mode, args.dry_run)

    if not args.skip_passwords:
        log("\n[3] Passwords (manual GUI step)")
        log(PASSWORDS_INSTRUCTIONS)

    log("Done. Outputs (if any) are in: " + str(OUT_DIR))


if __name__ == "__main__":
    main()
