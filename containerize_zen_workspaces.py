#!/usr/bin/env python3
"""
Post-arc2zen fix: bind each Zen workspace to its own Firefox container,
re-tag that workspace's Essentials with the matching container, and
enable container-specific Essentials. Run this with Zen FULLY QUIT.

What it does, in order:
  1. Locates the default Zen profile.
  2. Backs up: containers.json, places.sqlite (+ -wal/-shm), user.js if exists.
  3. Reads workspaces from places.sqlite (zen_workspaces table).
  4. Creates one container per workspace in containers.json
     (name = workspace name, color cycled, icon = "fingerprint").
  5. Sets zen_workspaces.container_id (or equivalent column) to the
     workspace's new userContextId.
  6. Updates zen_pins for that workspace's Essentials to userContextId =
     workspace's container.
  7. Writes user.js to enable container-specific Essentials + containers.

Requires standard library only.

Usage:
  python3 containerize_zen_workspaces.py --dry-run
  python3 containerize_zen_workspaces.py
  python3 containerize_zen_workspaces.py --workspaces "Personal,Work"   # subset

If you're on Zen 1.18+ which writes the source of truth to
zen-sessions.jsonlz4, the SQLite path may not be authoritative; see the
notes at the bottom of this file.
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
from pathlib import Path

HOME = Path.home()
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"

# Firefox container colors and icons (Mozilla-validated set).
CONTAINER_COLORS = ["blue", "turquoise", "green", "yellow", "orange",
                    "red", "pink", "purple"]
DEFAULT_ICON = "fingerprint"

PREF_LINES = [
    'user_pref("privacy.userContext.enabled", true);',
    'user_pref("privacy.userContext.ui.enabled", true);',
    'user_pref("zen.workspaces.container-specific-essentials-enabled", true);',
]


# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def is_app_running(app_name: str) -> bool:
    """True iff a process from the named .app bundle is running. See note in
    migrate_arc_to_zen.py — bundle-path matching avoids false positives like
    'SearchAgent' hitting on a bare 'Arc' substring."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", f"/{app_name}.app/Contents/MacOS/"],
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def safety(force: bool = False) -> None:
    if force:
        log("(--force-running: skipping browser-running check)")
        return
    if is_app_running("Zen Browser"):
        log("Refusing to proceed: Zen Browser is running. Quit it with Cmd+Q first.")
        log("Override with --force-running if you're certain.")
        sys.exit(1)


def find_zen_profile(name_hint: str | None = None) -> Path:
    ini = ZEN_DIR / "profiles.ini"
    if not ini.exists():
        raise SystemExit(f"profiles.ini not found at {ini}")
    cp = configparser.ConfigParser()
    cp.read(ini)
    default_rel = None
    for section in cp.sections():
        if section.startswith("Install"):
            default_rel = cp[section].get("Default")
            if default_rel:
                break
    if not default_rel:
        for section in cp.sections():
            if section.startswith("Profile") and cp[section].get("Default", "0") == "1":
                default_rel = cp[section].get("Path", "")
                break
    if not default_rel:
        raise SystemExit("Could not determine Zen default profile.")
    profile = (ZEN_DIR / default_rel).resolve()
    if not profile.exists():
        raise SystemExit(f"Profile dir missing: {profile}")
    return profile


def backup(p: Path) -> Path | None:
    if not p.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = p.with_name(p.name + f".bak-{ts}")
    shutil.copy2(p, dst)
    return dst


# --------------------------------------------------------------------------- #
# Schema discovery — robust to column-name drift across Zen versions.
# --------------------------------------------------------------------------- #
def cols(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def pick(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in columns:
            return c
    return None


def discover_schema(con: sqlite3.Connection) -> dict:
    ws_cols = cols(con, "zen_workspaces")
    pin_cols = cols(con, "zen_pins")
    if not ws_cols:
        raise SystemExit("zen_workspaces table not found. Did arc2zen actually run? "
                         "Or is this Zen on the jsonlz4-only storage path?")
    schema = {
        "ws.id": pick(ws_cols, ("uuid", "id", "workspace_id")),
        "ws.name": pick(ws_cols, ("name", "title")),
        "ws.container": pick(ws_cols, ("container_id", "containerId",
                                       "userContextId", "user_context_id",
                                       "default_container_id")),
        "pin.workspace": pick(pin_cols, ("workspace_uuid", "workspace_id",
                                         "workspaceUuid", "uuid_workspace")),
        "pin.essential": pick(pin_cols, ("is_essential", "essential", "isEssential")),
        "pin.container": pick(pin_cols, ("container_id", "containerId",
                                         "userContextId", "user_context_id")),
    }
    log("  Schema probe:")
    for k, v in schema.items():
        log(f"    {k}: {v or '(missing)'}")
    missing = [k for k, v in schema.items() if v is None]
    if "ws.id" in missing or "ws.name" in missing:
        raise SystemExit(f"Required workspace columns missing: {missing}")
    return schema


# --------------------------------------------------------------------------- #
def read_workspaces(con: sqlite3.Connection, schema: dict) -> list[dict]:
    q = f"SELECT {schema['ws.id']} AS id, {schema['ws.name']} AS name FROM zen_workspaces"
    return [{"id": r[0], "name": r[1]} for r in con.execute(q).fetchall()]


def load_containers(profile: Path) -> dict:
    path = profile / "containers.json"
    if not path.exists():
        return {"version": 5, "lastUserContextId": 4, "identities": []}
    return json.loads(path.read_text(encoding="utf-8"))


def container_for_workspace(containers: dict, ws_name: str, color: str) -> dict:
    """Return existing container with same name, else create a new one."""
    target_name = f"Zen: {ws_name}"
    for ident in containers["identities"]:
        if ident.get("name") == target_name:
            return ident
    new_id = int(containers.get("lastUserContextId", 0)) + 1
    containers["lastUserContextId"] = new_id
    ident = {
        "userContextId": new_id,
        "public": True,
        "icon": DEFAULT_ICON,
        "color": color,
        "name": target_name,
    }
    containers["identities"].append(ident)
    return ident


def write_containers(profile: Path, containers: dict, dry_run: bool) -> None:
    path = profile / "containers.json"
    if dry_run:
        log(f"  (dry-run) Would write {len(containers['identities'])} identities to {path}")
        return
    path.write_text(json.dumps(containers, indent=2), encoding="utf-8")
    log(f"  Wrote {path}")


def write_user_js(profile: Path, dry_run: bool) -> None:
    path = profile / "user.js"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    to_add = [ln for ln in PREF_LINES if ln not in existing]
    if not to_add:
        log("  user.js already has all required prefs.")
        return
    if dry_run:
        log(f"  (dry-run) Would append {len(to_add)} pref(s) to {path}")
        return
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# Added by containerize_zen_workspaces.py\n")
        for ln in to_add:
            f.write(ln + "\n")
    log(f"  Updated {path}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workspaces", help="Comma-separated names to limit to (case-insensitive substring).")
    ap.add_argument("--force-running", action="store_true",
                    help="Skip the browser-running guard. Only use if you're certain.")
    args = ap.parse_args()

    safety(force=args.force_running)

    profile = find_zen_profile()
    log(f"Zen profile: {profile}")
    places = profile / "places.sqlite"
    if not places.exists():
        raise SystemExit(f"places.sqlite missing: {places}")

    # Backups first.
    log("Backing up:")
    for p in (places, profile / "places.sqlite-wal", profile / "places.sqlite-shm",
              profile / "containers.json", profile / "user.js"):
        b = backup(p)
        if b:
            log(f"  {p.name} -> {b.name}")

    con = sqlite3.connect(places)
    con.execute("PRAGMA foreign_keys = ON")
    schema = discover_schema(con)

    workspaces = read_workspaces(con, schema)
    if not workspaces:
        raise SystemExit("No workspaces found in zen_workspaces table. Nothing to do.")
    if args.workspaces:
        wanted = {w.strip().lower() for w in args.workspaces.split(",")}
        workspaces = [w for w in workspaces
                      if any(s in (w["name"] or "").lower() for s in wanted)]
        if not workspaces:
            raise SystemExit("--workspaces filter matched nothing.")

    log(f"Workspaces to process ({len(workspaces)}):")
    for w in workspaces:
        log(f"  - {w['name']}  ({w['id']})")

    containers = load_containers(profile)

    # Step 1: ensure a container per workspace.
    log("\n[1] Creating / reusing containers")
    ws_to_uctx: dict[str, int] = {}
    for i, w in enumerate(workspaces):
        color = CONTAINER_COLORS[i % len(CONTAINER_COLORS)]
        ident = container_for_workspace(containers, w["name"] or f"WS-{i}", color)
        ws_to_uctx[w["id"]] = ident["userContextId"]
        log(f"  {w['name']!r} -> userContextId {ident['userContextId']} "
            f"(name={ident['name']!r}, color={ident['color']})")
    write_containers(profile, containers, args.dry_run)

    # Step 2: bind workspace -> container in zen_workspaces.
    log("\n[2] Binding workspaces to containers")
    ws_container_col = schema["ws.container"]
    if not ws_container_col:
        log("  ! zen_workspaces has no container column — cannot bind. "
            "The pref will still filter Essentials by their own userContextId, "
            "but the workspace's *default* container won't be set. You'll have "
            "to right-click each workspace in Zen and set its default container "
            "manually after launch.")
    else:
        for w in workspaces:
            uctx = ws_to_uctx[w["id"]]
            if args.dry_run:
                log(f"  (dry-run) UPDATE zen_workspaces SET {ws_container_col}={uctx} "
                    f"WHERE {schema['ws.id']}='{w['id']}'")
            else:
                con.execute(
                    f"UPDATE zen_workspaces SET {ws_container_col} = ? "
                    f"WHERE {schema['ws.id']} = ?",
                    (uctx, w["id"]),
                )
                log(f"  {w['name']!r}.{ws_container_col} = {uctx}")

    # Step 3: re-tag this workspace's Essentials.
    log("\n[3] Re-tagging Essentials by workspace")
    pin_ws_col = schema["pin.workspace"]
    pin_ess_col = schema["pin.essential"]
    pin_ctx_col = schema["pin.container"]
    if not (pin_ws_col and pin_ctx_col):
        log("  ! zen_pins is missing workspace or container columns; skipping.")
    else:
        ess_clause = f"AND {pin_ess_col} = 1" if pin_ess_col else ""
        total = 0
        for w in workspaces:
            uctx = ws_to_uctx[w["id"]]
            if args.dry_run:
                cnt = con.execute(
                    f"SELECT COUNT(*) FROM zen_pins WHERE {pin_ws_col} = ? {ess_clause}",
                    (w["id"],),
                ).fetchone()[0]
                log(f"  (dry-run) Would tag {cnt} essential(s) in {w['name']!r} -> uctx {uctx}")
                total += cnt
            else:
                cur = con.execute(
                    f"UPDATE zen_pins SET {pin_ctx_col} = ? "
                    f"WHERE {pin_ws_col} = ? {ess_clause}",
                    (uctx, w["id"]),
                )
                log(f"  {w['name']!r}: {cur.rowcount} pin(s) updated to uctx {uctx}")
                total += cur.rowcount
        log(f"  Total: {total}")

    if not args.dry_run:
        con.commit()
    con.close()

    # Step 4: toggle prefs.
    log("\n[4] Writing user.js prefs")
    write_user_js(profile, args.dry_run)

    log("\nDone.")
    log("Launch Zen. If Essentials still appear in every workspace, double-check:")
    log("  - about:preferences > Tab Management > 'Enable container-specific Essentials' is ON")
    log("  - Each workspace's default container matches what this script created.")
    log("    (Right-click workspace > Edit > default container)")
    log("  - If on Zen 1.18+ and SQLite changes didn't stick, see jsonlz4 note below.")


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------- #
# Notes on Zen 1.18+ storage:
#
# Newer Zen writes the sidebar/Essentials state to zen-sessions.jsonlz4 (mozLz4
# compressed JSON) and may treat zen_pins/zen_workspaces as a fallback or
# legacy export only. If after running this script Zen wipes your changes on
# launch, the source of truth is the jsonlz4 file. The fix in that case:
#
#   1. Decompress zen-sessions.jsonlz4:
#        magic 8B "mozLz40\0" + uint32 LE decompressed size + lz4 block data
#   2. Walk the JSON, find each workspace -> its essentials list.
#   3. Set each essential's "userContextId" (or "container") to the
#      workspace's container id (matching the userContextId you just created
#      in containers.json).
#   4. Recompress and write back.
#
# Python recipe (requires `pip3 install lz4`):
#
#   import lz4.block, struct
#   raw = open(path, 'rb').read()
#   assert raw[:8] == b"mozLz40\0"
#   size = struct.unpack("<I", raw[8:12])[0]
#   data = lz4.block.decompress(raw[12:], uncompressed_size=size)
#   obj = json.loads(data)
#   # ... mutate obj ...
#   blob = json.dumps(obj).encode()
#   out = b"mozLz40\0" + struct.pack("<I", len(blob)) + lz4.block.compress(blob)
#   open(path, 'wb').write(out)
#
# Ping me back if the SQLite path doesn't take and I'll wire that up too.
