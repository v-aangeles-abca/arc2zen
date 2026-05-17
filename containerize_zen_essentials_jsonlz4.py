#!/usr/bin/env python3
"""
Bind Zen Essentials to their workspace's container by editing
zen-sessions.jsonlz4 in place. Default mode is DRY-RUN: pass --apply to
write.

Why this exists:
  Containers and workspace<->container bindings are already correct in your
  profile (containers.json + spaces[].containerTabId). But every tab —
  including Essentials — was migrated with userContextId=0 (default
  container). The container-specific-Essentials feature filters by
  userContextId, so with everything at 0 there's nothing to filter and
  Essentials appear in every workspace.

What this script does:
  1. Backs up zen-sessions.jsonlz4 and user.js (if present).
  2. Decompresses zen-sessions.jsonlz4 (mozLz4).
  3. Builds workspace_uuid -> containerTabId from spaces[].
  4. For every tab where zenEssential is True, sets userContextId to the
     workspace's container. Optionally also re-tags non-essential pinned
     tabs (--pinned-too). Leaves non-pinned tabs alone.
  5. Recompresses and writes back.
  6. Appends the required prefs to user.js so the filter is on at launch.

Requires:
  pip install lz4
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"

PREFS = [
    'user_pref("privacy.userContext.enabled", true);',
    'user_pref("privacy.userContext.ui.enabled", true);',
    'user_pref("zen.workspaces.container-specific-essentials-enabled", true);',
]


# --------------------------------------------------------------------------- #
def log(m: str) -> None:
    print(m, flush=True)


def is_app_running(app_name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", f"/{app_name}.app/Contents/MacOS/"],
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def find_profile() -> Path:
    ini = ZEN_DIR / "profiles.ini"
    if not ini.exists():
        raise SystemExit(f"profiles.ini not found at {ini}")
    cp = configparser.ConfigParser()
    cp.read(ini)
    rel = None
    for s in cp.sections():
        if s.startswith("Install"):
            rel = cp[s].get("Default")
            if rel:
                break
    if not rel:
        for s in cp.sections():
            if s.startswith("Profile") and cp[s].get("Default", "0") == "1":
                rel = cp[s].get("Path", "")
                break
    if not rel:
        raise SystemExit("Could not determine default profile in profiles.ini")
    p = (ZEN_DIR / rel).resolve()
    if not p.exists():
        raise SystemExit(f"Profile dir not found: {p}")
    return p


def backup(p: Path) -> Path | None:
    if not p.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = p.with_name(p.name + f".bak-{ts}")
    shutil.copy2(p, dst)
    return dst


# --------------------------------------------------------------------------- #
# mozLz4 codec
# --------------------------------------------------------------------------- #
MAGIC = b"mozLz40\0"


def mozlz4_decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != MAGIC:
        raise SystemExit(f"{path.name}: not a mozLz4 file (bad magic).")
    size = struct.unpack("<I", raw[8:12])[0]
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("Missing lz4. Install with:  pip install lz4")
    return lz4.block.decompress(raw[12:], uncompressed_size=size)


def mozlz4_compress(data: bytes) -> bytes:
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("Missing lz4. Install with:  pip install lz4")
    compressed = lz4.block.compress(data, mode="default")
    return MAGIC + struct.pack("<I", len(data)) + compressed


# --------------------------------------------------------------------------- #
def build_ws_to_ctx(obj: dict) -> dict[str, int]:
    """Map workspace UUID -> containerTabId from spaces[]."""
    m: dict[str, int] = {}
    for sp in obj.get("spaces", []):
        uuid = sp.get("uuid")
        ctx = sp.get("containerTabId")
        if uuid and isinstance(ctx, int) and ctx > 0:
            m[uuid] = ctx
    return m


def update_tab_list(tabs: list, ws_to_ctx: dict[str, int],
                    *, include_pinned: bool,
                    label: str) -> dict:
    """Mutate tabs in place. Returns counts."""
    stats = {"essentials_changed": 0, "essentials_skipped_no_ws": 0,
             "pinned_changed": 0, "pinned_skipped_no_ws": 0,
             "untouched": 0}
    for t in tabs:
        if not isinstance(t, dict):
            stats["untouched"] += 1
            continue
        ws = t.get("zenWorkspace")
        is_ess = bool(t.get("zenEssential"))
        is_pinned = bool(t.get("pinned"))
        if is_ess:
            if ws not in ws_to_ctx:
                stats["essentials_skipped_no_ws"] += 1
                continue
            target = ws_to_ctx[ws]
            if t.get("userContextId") != target:
                t["userContextId"] = target
                stats["essentials_changed"] += 1
            else:
                stats["untouched"] += 1
        elif is_pinned and include_pinned:
            if ws not in ws_to_ctx:
                stats["pinned_skipped_no_ws"] += 1
                continue
            target = ws_to_ctx[ws]
            if t.get("userContextId") != target:
                t["userContextId"] = target
                stats["pinned_changed"] += 1
            else:
                stats["untouched"] += 1
        else:
            stats["untouched"] += 1
    log(f"  {label}: "
        f"ess +{stats['essentials_changed']}  "
        f"pinned +{stats['pinned_changed']}  "
        f"no-ws-essentials {stats['essentials_skipped_no_ws']}  "
        f"untouched {stats['untouched']}")
    return stats


def update_groups(groups: list, ws_to_ctx: dict[str, int],
                  *, include_pinned: bool) -> dict:
    """Saved/closed groups also carry tab state with userContextId; update
    them so reopening preserves container assignment."""
    total = {"essentials_changed": 0, "pinned_changed": 0,
             "essentials_skipped_no_ws": 0, "pinned_skipped_no_ws": 0,
             "untouched": 0}
    for g in groups:
        gtabs = g.get("tabs", []) if isinstance(g, dict) else []
        # Each saved-group entry has .state which is the same shape as a
        # top-level tab. Update the state dicts.
        states = []
        for gt in gtabs:
            if isinstance(gt, dict) and isinstance(gt.get("state"), dict):
                states.append(gt["state"])
        if not states:
            continue
        sub = update_tab_list(states, ws_to_ctx,
                              include_pinned=include_pinned,
                              label=f"  group {g.get('name','?')!r}")
        for k in total:
            total[k] += sub.get(k, 0)
    return total


def write_user_js(profile: Path, dry_run: bool) -> None:
    path = profile / "user.js"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    to_add = [p for p in PREFS if p not in existing]
    if not to_add:
        log("  user.js already has all required prefs.")
        return
    if dry_run:
        log(f"  (dry-run) Would append {len(to_add)} pref(s) to {path}")
        return
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# Added by containerize_zen_essentials_jsonlz4.py\n")
        for line in to_add:
            f.write(line + "\n")
    log(f"  Updated {path} (+{len(to_add)} prefs)")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--pinned-too", action="store_true",
                    help="Also re-tag non-essential pinned tabs to their "
                         "workspace's container. Off by default to avoid "
                         "breaking logged-in sessions on those tabs.")
    ap.add_argument("--force-running", action="store_true",
                    help="Skip the Zen-running check. Don't use unless certain.")
    args = ap.parse_args()
    dry_run = not args.apply

    log(f"Mode: {'DRY-RUN (no writes)' if dry_run else 'APPLY'}")
    if not args.force_running and is_app_running("Zen Browser"):
        raise SystemExit("Zen Browser is running. Quit it (Cmd+Q) first, or "
                         "pass --force-running.")

    profile = find_profile()
    log(f"Profile: {profile}")

    sess = profile / "zen-sessions.jsonlz4"
    if not sess.exists():
        raise SystemExit(f"Not found: {sess}")

    if not dry_run:
        log("\nBacking up:")
        for p in (sess, profile / "user.js"):
            b = backup(p)
            if b:
                log(f"  {p.name} -> {b.name}")

    log("\nDecompressing zen-sessions.jsonlz4 ...")
    raw_json = mozlz4_decompress(sess)
    obj = json.loads(raw_json)

    ws_to_ctx = build_ws_to_ctx(obj)
    log(f"Workspace -> containerTabId map ({len(ws_to_ctx)} workspaces):")
    by_uuid = {sp.get("uuid"): sp.get("name", "?")
               for sp in obj.get("spaces", [])}
    for uuid, ctx in ws_to_ctx.items():
        log(f"  {by_uuid.get(uuid, '?')!r}  ({uuid})  -> userContextId {ctx}")

    if not ws_to_ctx:
        raise SystemExit("No workspace -> container mapping found. Did "
                         "arc2zen run? Are containers assigned to spaces?")

    log("\nUpdating tab userContextIds:")
    update_tab_list(obj.get("tabs", []), ws_to_ctx,
                    include_pinned=args.pinned_too,
                    label="top-level tabs")
    update_groups(obj.get("groups", []), ws_to_ctx,
                  include_pinned=args.pinned_too)

    # Recompress
    new_blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    log(f"\nRecompressed payload: {len(new_blob)} bytes (was {len(raw_json)}).")

    if dry_run:
        log("\n(dry-run) Not writing zen-sessions.jsonlz4. "
            "Pass --apply to commit.")
    else:
        out_bytes = mozlz4_compress(new_blob)
        sess.write_bytes(out_bytes)
        log(f"Wrote {sess} ({len(out_bytes)} bytes on disk).")

    log("\nUser prefs:")
    write_user_js(profile, dry_run)

    log("\nDone.")
    if dry_run:
        log("Re-run with --apply to commit changes.")
    else:
        log("Launch Zen. Verify in about:preferences > Tab Management that "
            "'Enable container-specific Essentials' is on. Your Essentials "
            "should now appear only in the matching workspace.")
        log("\nReminder: delete the decoded plaintext dump from earlier:")
        log("  /Users/arielangeles/code/personal/automation/zen-browser/zen-sessions.decoded.json")


if __name__ == "__main__":
    main()
