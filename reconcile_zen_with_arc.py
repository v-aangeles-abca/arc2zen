#!/usr/bin/env python3
"""
Reconcile Zen's per-workspace Essentials against the source of truth in Arc.

In a single pass it:
  1. Reads Arc's StorableSidebar.json to enumerate every pinned tab per
     Space and every top-app (Favorite) per Profile.
  2. Reads Zen's zen-sessions.jsonlz4 and containers.json to see what's
     already there and which container is bound to which workspace.
  3. Maps Arc Spaces to Zen Workspaces by name (case-insensitive,
     overridable). Maps Arc top-apps to a target workspace (default:
     "Personal", overridable with --topapps-target).
  4. For every Arc URL that should exist in a Zen workspace but doesn't,
     synthesizes a new essential tab and inserts it.
  5. For every Essential (existing or newly added), sets userContextId to
     match that workspace's containerTabId.
  6. Enables container-specific Essentials via user.js.

Safety:
  * Default is DRY-RUN. Use --apply to actually write.
  * Zen must be fully quit (Cmd+Q). Override with --force-running.
  * Backs up zen-sessions.jsonlz4 and user.js before any write.
  * Synthesized tabs are deep-copied from an existing Essential template
    (preserving all fields Zen expects), then minimally edited. If no
    template can be found, the script aborts before writing.

Requires:  pip install lz4

Usage:
  python3 reconcile_zen_with_arc.py                       # dry-run
  python3 reconcile_zen_with_arc.py --dump-arc            # just dump Arc inventory and exit
  python3 reconcile_zen_with_arc.py --apply               # do it
  python3 reconcile_zen_with_arc.py --topapps-target Personal --apply
  python3 reconcile_zen_with_arc.py --map "Arc Space Name=Zen Workspace Name,Other=Other2"
"""

from __future__ import annotations

import argparse
import configparser
import copy
import datetime
import json
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
ARC_DIR = HOME / "Library" / "Application Support" / "Arc"
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"

ARC_SIDEBAR = ARC_DIR / "StorableSidebar.json"

PREFS = [
    'user_pref("privacy.userContext.enabled", true);',
    'user_pref("privacy.userContext.ui.enabled", true);',
    'user_pref("zen.workspaces.container-specific-essentials-enabled", true);',
]

# Firefox container colors & icons. Cycled when auto-creating containers.
CONTAINER_COLORS = ["blue", "turquoise", "green", "yellow", "orange",
                    "red", "pink", "purple"]
CONTAINER_ICONS = ["fingerprint", "briefcase", "dollar", "cart", "circle",
                   "gift", "vacation", "food", "fruit", "pet", "tree",
                   "chill", "fence"]


# --------------------------------------------------------------------------- #
def log(m: str = "") -> None:
    print(m, flush=True)


def norm(s: str | None) -> str:
    return (s or "").strip().lower()


def is_app_running(app_name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", f"/{app_name}.app/Contents/MacOS/"],
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def find_zen_profile() -> Path:
    ini = ZEN_DIR / "profiles.ini"
    if not ini.exists():
        raise SystemExit(f"Zen profiles.ini not found at {ini}")
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
        raise SystemExit("Could not determine default Zen profile.")
    p = (ZEN_DIR / rel).resolve()
    if not p.exists():
        raise SystemExit(f"Zen profile dir not found: {p}")
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


def mozlz4_read(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != MAGIC:
        raise SystemExit(f"{path.name}: not a mozLz4 file (bad magic).")
    size = struct.unpack("<I", raw[8:12])[0]
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("Missing dependency. Install with:  pip install lz4")
    return lz4.block.decompress(raw[12:], uncompressed_size=size)


def mozlz4_write(data: bytes) -> bytes:
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("Missing dependency. Install with:  pip install lz4")
    return MAGIC + struct.pack("<I", len(data)) + lz4.block.compress(data, store_size=False)


# --------------------------------------------------------------------------- #
# Arc parser
# --------------------------------------------------------------------------- #
_DEFAULT_PROFILE_KEY = "__default__"  # sentinel for Arc's "default" profile


def _tab_url_title(item: dict) -> tuple[str | None, str | None]:
    """Best-effort (url, title) from an Arc item representing a tab."""
    data = item.get("data") if isinstance(item, dict) else None
    if not isinstance(data, dict):
        return (None, None)
    tab = data.get("tab")
    if isinstance(tab, dict):
        url = tab.get("savedURL")
        title = tab.get("savedTitle") or tab.get("customTitle") or item.get("title")
        if url:
            return (url, title)
    return (None, None)


def _collect_tabs_under(item_id: str, items_by_id: dict, into: list,
                       seen: set | None = None) -> None:
    """Walk a container's childrenIds collecting tab-like items."""
    if seen is None:
        seen = set()
    if item_id in seen:
        return
    seen.add(item_id)
    item = items_by_id.get(item_id)
    if not item:
        return
    url, title = _tab_url_title(item)
    if url:
        into.append({"url": url, "title": title or url, "arc_id": item_id})
        return
    for child_id in item.get("childrenIds") or []:
        if isinstance(child_id, str):
            _collect_tabs_under(child_id, items_by_id, into, seen)


def _arc_folder_title(item: dict) -> str:
    """Best-effort folder title from an Arc item."""
    name = (item.get("title") or "").strip()
    if not name:
        data = item.get("data")
        if isinstance(data, dict):
            lst = data.get("list")
            if isinstance(lst, dict):
                name = (lst.get("title") or "").strip()
    return name or "(folder)"


def _collect_pinned_with_folders(container_id: str,
                                 items_by_id: dict) -> list[dict]:
    """Walk a Space's pinned container preserving one level of folder grouping.

    Returns a list of tab dicts each with an extra ``folder`` key:
      - ``folder=None`` for top-level tabs (no folder)
      - ``folder="Social"`` for tabs nested inside an Arc folder named "Social"
    """
    container = items_by_id.get(container_id, {})
    result: list[dict] = []
    for child_id in container.get("childrenIds") or []:
        if not isinstance(child_id, str):
            continue
        child = items_by_id.get(child_id)
        if not child:
            continue
        url, title = _tab_url_title(child)
        if url:
            result.append({"url": url, "title": title or url,
                           "arc_id": child_id, "folder": None})
        elif child.get("childrenIds"):
            folder_name = _arc_folder_title(child)
            for gchild_id in child["childrenIds"]:
                if not isinstance(gchild_id, str):
                    continue
                tabs: list[dict] = []
                _collect_tabs_under(gchild_id, items_by_id, tabs)
                for t in tabs:
                    t["folder"] = folder_name
                result.extend(tabs)
    return result


def _profile_key(profile_field) -> str | None:
    """Normalize an Arc 'profile' enum-shaped dict to a key.

    Arc Swift enum encodes as either:
      {"default": ...}            -> default profile (sentinel)
      {"custom": {"_0": {...}}}   -> custom; inner dict has 'directoryBasename'

    Returns _DEFAULT_PROFILE_KEY for default, the directoryBasename string for
    custom, or None if not parseable.
    """
    if not isinstance(profile_field, dict):
        return None
    if "default" in profile_field:
        return _DEFAULT_PROFILE_KEY
    if "custom" in profile_field:
        cust = profile_field["custom"]
        if isinstance(cust, dict):
            inner = cust.get("_0")
            if isinstance(inner, dict):
                db = inner.get("directoryBasename")
                if isinstance(db, str):
                    return db
    return None


def _extract_arc_space_style(sp: dict) -> dict:
    """Extract emoji icon and color from an Arc Space's customInfo."""
    ci = sp.get("customInfo") or {}
    result: dict = {"icon": None, "color": None}
    # Emoji / icon
    icon_type = ci.get("iconType")
    if isinstance(icon_type, dict):
        emoji = icon_type.get("emoji_v2")
        if emoji and isinstance(emoji, str):
            result["icon"] = emoji
    # Color (midTone RGB from windowTheme)
    try:
        mid = ci["windowTheme"]["primaryColorPalette"]["midTone"]
        r = max(0, min(255, int(mid["red"] * 255)))
        g = max(0, min(255, int(mid["green"] * 255)))
        b = max(0, min(255, int(mid["blue"] * 255)))
        result["color"] = [r, g, b]
    except (KeyError, TypeError, ValueError):
        pass
    return result


def parse_arc(sidebar_path: Path) -> dict:
    """Return Arc data anchored to the real shape:

      {
        'spaces':            [ { uuid, name, profile_key, pinned: [...], unpinned: [...],
                                 icon: str|None, color: [r,g,b]|None }, ... ],
        'topapps_by_profile':{ profile_key: [ {url, title, arc_id}, ... ], ... },
      }

    Spaces are read from $.sidebar.containers[1].spaces. topApps are linked
    per Arc Profile via $.sidebarSyncState.container.value.topAppsContainerIDs
    (pairs of profileSpec, containerID).
    """
    root = json.loads(sidebar_path.read_text(encoding="utf-8"))

    # Build a name -> style map from ALL containers (icon/color metadata
    # may live in a different container than the tab data).
    style_by_name: dict[str, dict] = {}
    for c in root.get("sidebar", {}).get("containers", []):
        if not isinstance(c, dict):
            continue
        for sp in c.get("spaces", []):
            if not isinstance(sp, dict):
                continue
            name = norm(sp.get("title") or "")
            if not name:
                continue
            style = _extract_arc_space_style(sp)
            if style["icon"] or style["color"]:
                style_by_name[name] = style

    # 1. Locate the active container (the one with the most titled spaces
    #    and items). Arc rotates which container index is live.
    live_spaces: list = []
    live_items: list = []
    best_score = -1
    for c in root.get("sidebar", {}).get("containers", []):
        if not isinstance(c, dict):
            continue
        spaces = c.get("spaces", [])
        items = c.get("items", [])
        titled = sum(1 for s in spaces if isinstance(s, dict) and s.get("title"))
        score = titled * 1000 + len(items)
        if score > best_score:
            best_score = score
            live_spaces = spaces
            live_items = items

    items_by_id: dict[str, dict] = {}
    for it in live_items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            items_by_id[it["id"]] = it

    # 2. Per-Space metadata + pinned/unpinned tabs.
    spaces_out: list[dict] = []
    for sp in live_spaces:
        if not isinstance(sp, dict):
            continue
        sp_id = sp.get("id")
        title = (sp.get("title") or "").strip()
        if not isinstance(sp_id, str) or not title:
            continue
        # containerIDs is a flat alternating [marker, uuid] list.
        cids = sp.get("containerIDs") or []
        markers: dict[str, str] = {}
        for i in range(0, len(cids) - 1, 2):
            m, u = cids[i], cids[i + 1]
            if isinstance(m, str) and isinstance(u, str):
                markers[m] = u

        pkey = _profile_key(sp.get("profile")) or _DEFAULT_PROFILE_KEY

        pinned: list[dict] = []
        if isinstance(markers.get("pinned"), str):
            pinned = _collect_pinned_with_folders(markers["pinned"], items_by_id)
        unpinned: list[dict] = []
        if isinstance(markers.get("unpinned"), str):
            for ch in items_by_id.get(markers["unpinned"], {}).get("childrenIds") or []:
                if isinstance(ch, str):
                    _collect_tabs_under(ch, items_by_id, unpinned)

        style = style_by_name.get(norm(title), {})
        spaces_out.append({
            "uuid": sp_id,
            "name": title,
            "profile_key": pkey,
            "pinned": pinned,
            "unpinned": unpinned,
            "icon": style.get("icon"),
            "color": style.get("color"),
        })

    # 3. profile_key -> topApps container id, from topAppsContainerIDs pairs.
    profile_to_container: dict[str, str] = {}
    try:
        mapping = root["sidebarSyncState"]["container"]["value"]["topAppsContainerIDs"]
    except (KeyError, TypeError):
        mapping = []
    if isinstance(mapping, list):
        for i in range(0, len(mapping) - 1, 2):
            spec = mapping[i]
            cid = mapping[i + 1]
            if not isinstance(cid, str):
                continue
            pkey = _profile_key(spec)
            if pkey is not None:
                profile_to_container[pkey] = cid

    # 4. Collect topApps tabs per profile.
    topapps_by_profile: dict[str, list[dict]] = {}
    for pkey, cid in profile_to_container.items():
        container = items_by_id.get(cid)
        if not container:
            continue
        collected: list[dict] = []
        for ch in container.get("childrenIds") or []:
            if isinstance(ch, str):
                _collect_tabs_under(ch, items_by_id, collected)
        # Dedup by URL preserving order.
        seen, uniq = set(), []
        for t in collected:
            if t["url"] in seen:
                continue
            seen.add(t["url"])
            uniq.append(t)
        topapps_by_profile[pkey] = uniq

    return {"spaces": spaces_out, "topapps_by_profile": topapps_by_profile}


# --------------------------------------------------------------------------- #
# Zen reading and writing
# --------------------------------------------------------------------------- #
def zen_load_session(profile: Path) -> tuple[dict, Path]:
    sess = profile / "zen-sessions.jsonlz4"
    if not sess.exists():
        raise SystemExit(f"Not found: {sess}")
    return json.loads(mozlz4_read(sess)), sess


def zen_workspaces(obj: dict) -> list[dict]:
    out = []
    for sp in obj.get("spaces") or []:
        if not isinstance(sp, dict):
            continue
        out.append({
            "uuid": sp.get("uuid"),
            "name": sp.get("name") or "",
            "containerTabId": sp.get("containerTabId") or 0,
        })
    return out


def zen_essentials_by_ws(obj: dict) -> dict[str, list[dict]]:
    """workspace uuid -> list of essential tab dicts (references into obj)."""
    out: dict[str, list[dict]] = {}
    for t in obj.get("tabs") or []:
        if not isinstance(t, dict) or not t.get("zenEssential"):
            continue
        out.setdefault(t.get("zenWorkspace") or "", []).append(t)
    return out


def tab_url(t: dict) -> str | None:
    entries = t.get("entries") if isinstance(t, dict) else None
    if isinstance(entries, list) and entries:
        e = entries[-1]  # current entry
        if isinstance(e, dict):
            return e.get("url")
    return None


def tab_title(t: dict) -> str | None:
    entries = t.get("entries") if isinstance(t, dict) else None
    if isinstance(entries, list) and entries:
        e = entries[-1]
        if isinstance(e, dict):
            return e.get("title")
    return None


# --------------------------------------------------------------------------- #
# Bootstrap: create Zen workspaces for Arc Spaces that have no match.
# --------------------------------------------------------------------------- #
_DEFAULT_WS_NAMES = ("space", "new space", "default")


def bootstrap_missing_workspaces(obj: dict, arc: dict, containers: dict,
                                 explicit_map: dict[str, str]) -> dict:
    """For each unique Arc Space name without a matching Zen workspace,
    create one. If a default-named workspace ('Space') exists, rename it
    to the first missing Arc Space (and rename its bound container to
    match too). Allocate a new container per new workspace.

    Mutates obj['spaces'] and containers in place. Returns a summary dict.
    """
    summary = {"renamed": None, "created": []}
    existing_names = {norm(w.get("name", ""))
                      for w in obj.get("spaces", []) if isinstance(w, dict)}

    # Unique Arc space names (preserve order). Honor --map overrides — when
    # an Arc Space is mapped to a different Zen workspace name, we look for
    # that mapped name in Zen rather than the Arc name.
    seen, arc_unique = set(), []
    for sp in arc.get("spaces", []):
        arc_name = sp.get("name", "")
        zen_target = explicit_map.get(norm(arc_name), arc_name)
        key = norm(zen_target)
        if not key or key in seen:
            continue
        seen.add(key)
        arc_unique.append(zen_target)

    missing = [n for n in arc_unique if norm(n) not in existing_names]
    if not missing:
        return summary

    # Try to rename a default-named workspace to consume one missing.
    default_ws = None
    for sp in obj.get("spaces", []):
        if isinstance(sp, dict) and norm(sp.get("name", "")) in _DEFAULT_WS_NAMES:
            default_ws = sp
            break

    if default_ws:
        old = default_ws.get("name", "")
        new = missing[0]
        default_ws["name"] = new
        # If the default's bound container is named like a default, rename it too.
        bound_ctx = default_ws.get("containerTabId")
        if bound_ctx:
            for ident in containers.get("identities", []):
                if ident.get("userContextId") == bound_ctx:
                    if norm(ident.get("name", "")) in _DEFAULT_WS_NAMES:
                        ident["name"] = new
                    break
        summary["renamed"] = {"from": old, "to": new}
        existing_names.add(norm(new))
        missing = missing[1:]

    # For the rest, clone a template workspace. Use the renamed/first space.
    template = None
    for sp in obj.get("spaces", []):
        if isinstance(sp, dict) and sp.get("uuid"):
            template = sp
            break
    if not template and missing:
        raise SystemExit("Cannot bootstrap: no workspace exists in Zen to clone.")

    for i, name in enumerate(missing):
        new_uuid = "{" + str(uuid.uuid4()) + "}"
        # Create a container for this workspace.
        new_ctx = int(containers.get("lastUserContextId") or 0) + 1
        containers["lastUserContextId"] = new_ctx
        containers.setdefault("identities", []).append({
            "userContextId": new_ctx,
            "public": True,
            "icon": CONTAINER_ICONS[i % len(CONTAINER_ICONS)],
            "color": CONTAINER_COLORS[i % len(CONTAINER_COLORS)],
            "name": name,
        })
        new_ws = copy.deepcopy(template)
        new_ws["uuid"] = new_uuid
        new_ws["name"] = name
        new_ws["containerTabId"] = new_ctx
        new_ws["hasCollapsedPinnedTabs"] = False
        obj.setdefault("spaces", []).append(new_ws)
        summary["created"].append({
            "name": name, "uuid": new_uuid, "container_uctx": new_ctx,
        })
        existing_names.add(norm(name))

    return summary


# --------------------------------------------------------------------------- #
# Workspace <-> container validation/repair
# --------------------------------------------------------------------------- #
def load_containers(profile: Path) -> dict:
    p = profile / "containers.json"
    if not p.exists():
        return {"version": 5, "lastUserContextId": 4, "identities": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save_containers(profile: Path, c: dict) -> Path:
    p = profile / "containers.json"
    p.write_text(json.dumps(c, indent=2), encoding="utf-8")
    return p


def _find_identity_by_uctx(c: dict, uctx: int) -> dict | None:
    for ident in c.get("identities", []):
        if ident.get("userContextId") == uctx:
            return ident
    return None


def _find_identity_by_name(c: dict, name: str) -> dict | None:
    target = norm(name)
    for ident in c.get("identities", []):
        if norm(ident.get("name")) == target:
            return ident
    return None


def validate_and_repair_workspace_containers(obj: dict, containers: dict) -> dict:
    """For each Zen workspace, make sure spaces[i].containerTabId points to a
    real, public container in containers.json. Create one if missing. Returns
    a summary of changes (does not call save_containers — caller decides).
    """
    changes = {"created_containers": [], "rebound_workspaces": []}
    spaces = obj.get("spaces") or []
    for i, sp in enumerate(spaces):
        if not isinstance(sp, dict):
            continue
        ws_name = (sp.get("name") or f"Workspace {i+1}").strip()
        ctx = sp.get("containerTabId") or 0
        ident = _find_identity_by_uctx(containers, ctx) if ctx > 0 else None
        if ident:
            continue  # already valid

        # Look for an existing identity whose name matches the workspace.
        ident = _find_identity_by_name(containers, ws_name)
        if not ident:
            # Create one.
            new_id = int(containers.get("lastUserContextId") or 0) + 1
            containers["lastUserContextId"] = new_id
            ident = {
                "userContextId": new_id,
                "public": True,
                "icon": CONTAINER_ICONS[i % len(CONTAINER_ICONS)],
                "color": CONTAINER_COLORS[i % len(CONTAINER_COLORS)],
                "name": ws_name,
            }
            containers.setdefault("identities", []).append(ident)
            changes["created_containers"].append(ident)

        old_ctx = sp.get("containerTabId")
        sp["containerTabId"] = ident["userContextId"]
        changes["rebound_workspaces"].append({
            "name": ws_name,
            "uuid": sp.get("uuid"),
            "old_ctx": old_ctx,
            "new_ctx": ident["userContextId"],
        })
    return changes


# --------------------------------------------------------------------------- #
# Tab synthesis
# --------------------------------------------------------------------------- #
def _new_uuid_braces() -> str:
    return "{" + str(uuid.uuid4()) + "}"


def _new_sync_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def _nullprincipal_b64() -> str:
    """Returns Zen-shaped triggeringPrincipal_base64 (JSON-stringified)."""
    return json.dumps({"0": {"0": f"moz-nullprincipal:{_new_uuid_braces()}"}})


def synth_tab(template: dict, *, url: str, title: str,
              ws_uuid: str, uctx: int, essential: bool = True,
              group_id: str | None = None) -> dict:
    """Build a new tab by deep-copying the template, then overwriting the
    few fields that must be unique/correct.

    When essential=True the tab becomes a Zen Essential (sidebar icon).
    When essential=False the tab becomes a regular tab in the workspace."""
    t = copy.deepcopy(template)
    # Reset entries to a single, fresh entry for this URL.
    t["entries"] = [{
        "url": url,
        "title": title or url,
        "cacheKey": 0,
        "ID": 0,
        "docshellUUID": _new_uuid_braces(),
        "originalURI": url,
        "resultPrincipalURI": url,
        "loadReplace": False,
        "loadReplace2": False,
        "contentType": "text/html",
        "principalToInherit_base64": _nullprincipal_b64(),
        "triggeringPrincipal_base64": _nullprincipal_b64(),
        "hasUserInteraction": False,
        "docIdentifier": 0,
        "transient": False,
        "navigationKey": _new_uuid_braces(),
        "navigationId": _new_uuid_braces(),
    }]
    t["lastAccessed"] = int(time.time() * 1000)
    t["pinned"] = True  # both essentials and imported pinned tabs are pinned
    t["hidden"] = False
    t["zenWorkspace"] = ws_uuid
    t["zenSyncId"] = _new_sync_id()
    t["zenEssential"] = essential
    t["zenIsEmpty"] = False
    t["zenIsGlance"] = False
    t["zenHasStaticIcon"] = False
    t["zenDefaultUserContextId"] = None
    t["zenPinnedIcon"] = None
    t["zenGlanceId"] = None
    t["zenLiveFolderItemId"] = None
    t["groupId"] = group_id
    t["searchMode"] = None
    t["userContextId"] = uctx
    t["attributes"] = {}
    t["userTypedValue"] = ""
    t["userTypedClear"] = 0
    # Drop session-storage / favicon image / saved initial state — Zen will
    # repopulate these on first activation. Keeping stale ones from the
    # template would map them to the WRONG site.
    for k in ("storage", "image", "_zenPinnedInitialState",
              "iconLoadingPrincipal_base64", "formdata", "scroll",
              "extData"):
        t.pop(k, None)
    return t


def pick_template(obj: dict) -> dict | None:
    """Pick an existing tab to use as a structural template for new essentials.

    Preference: zenEssential tab > any pinned tab > any tab > bare minimum.
    The returned dict is only used for its outer structure; synth_essential_tab
    replaces all content-bearing fields.
    """
    best = None
    for t in obj.get("tabs") or []:
        if not isinstance(t, dict):
            continue
        if t.get("zenEssential"):
            return t  # perfect match
        if best is None or t.get("pinned"):
            best = t
    if best is not None:
        return best
    # No tabs at all — build a bare-minimum skeleton. synth_essential_tab
    # will overwrite every meaningful field anyway.
    return {
        "entries": [],
        "lastAccessed": 0,
        "pinned": False,
        "hidden": False,
        "attributes": {},
        "userTypedValue": "",
        "userTypedClear": 0,
        "searchMode": None,
        "userContextId": 0,
        "zenWorkspace": "",
        "zenEssential": False,
    }


# --------------------------------------------------------------------------- #
# Reconciler
# --------------------------------------------------------------------------- #
def parse_map(s: str | None) -> dict[str, str]:
    if not s:
        return {}
    out: dict[str, str] = {}
    for pair in s.split(","):
        if "=" not in pair:
            continue
        a, b = pair.split("=", 1)
        out[norm(a)] = b.strip()
    return out


def build_arc_to_zen(arc: dict, zen_ws: list[dict], explicit_map: dict[str, str]) -> dict[str, str]:
    """Arc Space name (norm) -> Zen workspace UUID."""
    zen_by_name = {norm(w["name"]): w["uuid"] for w in zen_ws if w.get("uuid")}
    result: dict[str, str] = {}
    for sp in arc["spaces"]:
        arc_name = norm(sp["name"])
        # explicit override first
        if arc_name in explicit_map:
            target_name = norm(explicit_map[arc_name])
            if target_name in zen_by_name:
                result[arc_name] = zen_by_name[target_name]
                continue
        # name match
        if arc_name in zen_by_name:
            result[arc_name] = zen_by_name[arc_name]
    return result


def reconcile(arc: dict, obj: dict, topapps_target_name: str | None,
              include_unpinned: bool, explicit_map: dict[str, str]) -> dict:
    """Compute a plan that brings Zen's per-workspace Essentials to match
    Arc exactly.

    Strategy: Arc dictates a flat set of desired (workspace_uuid, url, title)
    tuples. Existing Zen essentials are pooled by URL. For each desired pair,
    we either (a) keep an existing tab that's already in the correct
    workspace, (b) MOVE an existing tab with the same URL from a wrong
    workspace into the correct one, or (c) synthesize a new tab. Anything
    left in the pool with no Arc demand is an "extra" — kept by default, or
    removed with --prune.
    """
    zen_ws = zen_workspaces(obj)
    ws_by_uuid = {w["uuid"]: w for w in zen_ws if w.get("uuid")}
    arc_to_zen = build_arc_to_zen(arc, zen_ws, explicit_map)

    # Workspace style (icon/color) from Arc -> Zen UUID.
    ws_styles: dict[str, dict] = {}
    for sp in arc["spaces"]:
        ws_uuid = arc_to_zen.get(norm(sp["name"]))
        if ws_uuid and (sp.get("icon") or sp.get("color")):
            ws_styles[ws_uuid] = {"icon": sp.get("icon"),
                                  "color": sp.get("color")}

    # 1. Flat desired Zen-essentials list, dedup by (ws_uuid, url).
    #
    # Zen essentials are sourced from Arc's per-PROFILE topApps. Arc's
    # per-Space pinned tabs become Zen pinned tabs (not essentials) — that
    # part is already handled by arc2zen and the user confirmed it's fine.
    #
    # Mapping: each Arc Space's profile_key -> Zen workspace name match.
    # EVOPOINT and Evo Learning share Profile 4 so they get the same topApps.
    desired_pairs: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_desired(ws_uuid: str, url: str, title: str | None,
                    source: str) -> None:
        key = (ws_uuid, url)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        desired_pairs.append({"ws_uuid": ws_uuid, "url": url,
                              "title": title or url, "source": source})

    # For each Arc Space, push that Space's Profile topApps into the matched
    # Zen workspace. Multiple Arc Spaces can share a profile (EVOPOINT &
    # Evo Learning) — both get the same essentials.
    ws_claimed: dict[str, list[str]] = {}
    for sp in arc["spaces"]:
        ws_uuid = arc_to_zen.get(norm(sp["name"]))
        if not ws_uuid:
            continue
        ws_claimed.setdefault(ws_uuid, []).append(sp["name"])
        pkey = sp.get("profile_key") or _DEFAULT_PROFILE_KEY
        for t in arc["topapps_by_profile"].get(pkey, []):
            add_desired(ws_uuid, t["url"], t["title"],
                        source=f"topApps:{pkey}")

    # Warn about duplicate Arc Space -> Zen workspace name matches (e.g. two
    # Spaces both called "Personal").
    for ws_uuid, names in ws_claimed.items():
        if len(set(map(norm, names))) > 1:
            log(f"  ! Multiple Arc Spaces map to the same Zen workspace: "
                f"{names}. URLs from all of them will merge.")

    # 1b. Arc per-Space pinned tabs -> Zen regular (non-essential) tabs.
    desired_pinned: list[dict] = []
    seen_pinned: set[tuple[str, str]] = set()
    for sp in arc["spaces"]:
        ws_uuid = arc_to_zen.get(norm(sp["name"]))
        if not ws_uuid:
            continue
        for t in sp["pinned"]:
            key = (ws_uuid, t["url"])
            if key in seen_pinned or key in seen_pairs:
                continue  # skip if already an essential or duplicate
            seen_pinned.add(key)
            desired_pinned.append({"ws_uuid": ws_uuid, "url": t["url"],
                                   "title": t["title"] or t["url"],
                                   "folder": t.get("folder")})
        if include_unpinned:
            for t in sp.get("unpinned", []):
                key = (ws_uuid, t["url"])
                if key in seen_pinned or key in seen_pairs:
                    continue
                seen_pinned.add(key)
                desired_pinned.append({"ws_uuid": ws_uuid, "url": t["url"],
                                       "title": t["title"] or t["url"],
                                       "folder": None})

    # Handle orphan topApps profiles (profile whose Arc Spaces don't match
    # any Zen workspace by name). Route them via --topapps-target if set.
    used_profiles = {sp.get("profile_key") for sp in arc["spaces"]
                     if arc_to_zen.get(norm(sp["name"]))}
    orphan_profiles = [p for p in arc["topapps_by_profile"]
                       if p not in used_profiles
                       and arc["topapps_by_profile"][p]]  # only non-empty
    if orphan_profiles and topapps_target_name and topapps_target_name.lower() != "none":
        target_uuid = None
        for w in zen_ws:
            if norm(w["name"]) == norm(topapps_target_name):
                target_uuid = w["uuid"]
                break
        if target_uuid:
            for p in orphan_profiles:
                for t in arc["topapps_by_profile"][p]:
                    add_desired(target_uuid, t["url"], t["title"],
                                source=f"orphan-topApps:{p}")

    # 2. Snapshot current Zen essentials into a per-URL pool.
    pool_by_url: dict[str, list[dict]] = {}
    for t in obj.get("tabs") or []:
        if not isinstance(t, dict) or not t.get("zenEssential"):
            continue
        u = tab_url(t)
        if not u:
            continue
        pool_by_url.setdefault(u, []).append({
            "tab": t,
            "ws_uuid": t.get("zenWorkspace") or "",
            "title": tab_title(t) or "",
        })

    # 3. Match each desired pair against the pool. Prefer in-place match.
    moves: list[dict] = []
    adds: list[dict] = []
    keeps: list[dict] = []
    for d in desired_pairs:
        candidates = pool_by_url.get(d["url"], [])
        match = None
        # Prefer one already in the right workspace.
        for i, c in enumerate(candidates):
            if c["ws_uuid"] == d["ws_uuid"]:
                match = candidates.pop(i)
                break
        # Otherwise take any same-URL essential (we'll move it).
        if match is None and candidates:
            match = candidates.pop(0)
        if match is None:
            adds.append({"url": d["url"], "title": d["title"], "ws_uuid": d["ws_uuid"]})
        elif match["ws_uuid"] != d["ws_uuid"]:
            moves.append({
                "tab": match["tab"],
                "from_ws": match["ws_uuid"],
                "to_ws": d["ws_uuid"],
                "url": d["url"],
                "title": d["title"] or match["title"],
            })
        else:
            keeps.append({"tab": match["tab"], "ws_uuid": d["ws_uuid"],
                          "url": d["url"]})

    # 4. Whatever's left in the pool has no Arc demand: extras.
    extras: list[dict] = []
    for u, lst in pool_by_url.items():
        for ess in lst:
            extras.append({"tab": ess["tab"], "url": u, "ws_uuid": ess["ws_uuid"]})

    # 4b. Pinned tabs: check which desired_pinned already exist in Zen
    # (as any tab, not just essentials) and compute what to add.
    existing_tab_urls_by_ws: dict[str, set[str]] = {}
    for t in obj.get("tabs") or []:
        if not isinstance(t, dict):
            continue
        u = tab_url(t)
        ws = t.get("zenWorkspace") or ""
        if u:
            existing_tab_urls_by_ws.setdefault(ws, set()).add(u)

    pinned_adds: list[dict] = []
    for d in desired_pinned:
        ws_urls = existing_tab_urls_by_ws.get(d["ws_uuid"], set())
        if d["url"] not in ws_urls:
            pinned_adds.append(d)

    # 5. Per-workspace summary for the print_plan dump.
    ws_summary = []
    for w in zen_ws:
        u = w["uuid"]
        if not u:
            continue
        ws_summary.append({
            "name": w["name"],
            "uuid": u,
            "containerTabId": w["containerTabId"] or 0,
            "desired": sum(1 for d in desired_pairs if d["ws_uuid"] == u),
            "keep": sum(1 for k in keeps if k["ws_uuid"] == u),
            "move_in": sum(1 for m in moves if m["to_ws"] == u),
            "move_out": sum(1 for m in moves if m["from_ws"] == u),
            "add": sum(1 for a in adds if a["ws_uuid"] == u),
            "pinned_add": sum(1 for a in pinned_adds if a["ws_uuid"] == u),
            "extras_in_ws": sum(1 for e in extras if e["ws_uuid"] == u),
        })

    return {
        "moves": moves,
        "adds": adds,
        "keeps": keeps,
        "extras": extras,
        "pinned_adds": pinned_adds,
        "ws_summary": ws_summary,
        "ws_by_uuid": ws_by_uuid,
        "ws_styles": ws_styles,
    }


def _make_empty_tab_for_folder(folder_id: str, ws_uuid: str) -> dict:
    """Create the empty placeholder tab that Zen requires for each folder."""
    return {
        "entries": [{"url": "about:blank",
                     "triggeringPrincipal_base64": '{"3":{}}'}],
        "lastAccessed": int(time.time() * 1000),
        "pinned": True,
        "hidden": False,
        "groupId": folder_id,
        "zenWorkspace": ws_uuid,
        "zenSyncId": _new_sync_id(),
        "zenEssential": False,
        "zenDefaultUserContextId": None,
        "zenPinnedIcon": None,
        "zenIsEmpty": True,
        "zenHasStaticIcon": False,
        "zenGlanceId": None,
        "zenIsGlance": False,
        "zenLiveFolderItemId": None,
        "searchMode": None,
        "userContextId": 0,
        "attributes": {},
        "userTypedValue": "",
        "userTypedClear": 0,
        "image": None,
    }


def _make_folder_entry(folder_id: str, name: str, ws_uuid: str,
                       empty_tab_sync_id: str,
                       collapsed: bool = False) -> dict:
    """Create a proper Zen folder entry for obj['folders']."""
    return {
        "pinned": True,
        "splitViewGroup": False,
        "id": folder_id,
        "name": name,
        "collapsed": collapsed,
        "saveOnWindowClose": True,
        "parentId": None,
        "prevSiblingInfo": {"type": "start", "id": None},
        "emptyTabIds": [empty_tab_sync_id],
        "userIcon": "",
        "workspaceId": ws_uuid,
    }


def _make_group_entry(folder_id: str, name: str,
                      collapsed: bool = False) -> dict:
    """Create a corresponding Zen group entry for obj['groups']."""
    return {
        "pinned": True,
        "splitView": False,
        "id": folder_id,
        "name": name,
        "color": "zen-workspace-color",
        "collapsed": collapsed,
        "saveOnWindowClose": True,
        "saved": True,
        "closedAt": int(time.time() * 1000),
        "windowClosedId": 2,
        "tabs": [],
        "splitViews": [],
    }


def _migrate_groups_to_folders(obj: dict) -> int:
    """Migrate any entries in obj['groups'] to proper obj['folders'] format.

    Zen requires BOTH a 'folders' entry AND a 'groups' entry with the same id,
    plus an empty placeholder tab referenced by emptyTabIds. Earlier versions
    of this script only created 'groups' entries.

    Returns the number of migrated entries.
    """
    groups = obj.get("groups", [])
    if not groups:
        return 0

    existing_folder_ids = {f["id"] for f in obj.get("folders", [])}
    existing_group_ids = {g["id"] for g in groups}
    # Determine workspace for each group by looking at tabs that reference it
    group_to_ws: dict[str, str] = {}
    for t in obj.get("tabs", []):
        gid = t.get("groupId")
        ws = t.get("zenWorkspace")
        if gid and ws and gid not in group_to_ws:
            group_to_ws[gid] = ws

    migrated = 0
    new_groups = []
    for g in groups:
        gid = g.get("id", "")
        ws_uuid = group_to_ws.get(gid)

        if gid in existing_folder_ids:
            # Already has a folders entry — keep the group, ensure format.
            new_groups.append(g)
            continue

        if not ws_uuid:
            new_groups.append(g)
            continue

        # Create the empty placeholder tab
        empty_tab = _make_empty_tab_for_folder(gid, ws_uuid)
        obj.setdefault("tabs", []).append(empty_tab)

        # Create the proper folder entry
        obj.setdefault("folders", []).append(
            _make_folder_entry(gid, g.get("name", ""), ws_uuid,
                               empty_tab["zenSyncId"],
                               collapsed=g.get("collapsed", False)))

        # Replace the old-format group entry with a proper one
        new_groups.append(
            _make_group_entry(gid, g.get("name", ""),
                              collapsed=g.get("collapsed", False)))
        migrated += 1

    obj["groups"] = new_groups
    return migrated


def apply_plan(obj: dict, plan: dict, also_pinned: bool, prune: bool) -> None:
    template = pick_template(obj)

    ws_ctx = {sp.get("uuid"): sp.get("containerTabId") or 0
              for sp in obj.get("spaces") or [] if isinstance(sp, dict)}

    # 0. Migrate any broken 'groups' entries from earlier runs to proper 'folders'.
    migrated = _migrate_groups_to_folders(obj)
    if migrated:
        log(f"  Migrated {migrated} broken group(s) to proper folder format.")

    # 1. Moves: re-tag essentials that are in the wrong workspace.
    for m in plan["moves"]:
        target_ctx = ws_ctx.get(m["to_ws"], 0)
        m["tab"]["zenWorkspace"] = m["to_ws"]
        m["tab"]["userContextId"] = target_ctx
        m["tab"]["zenEssential"] = True
        m["tab"]["pinned"] = True

    # 2. Keeps: just ensure userContextId is right.
    for k in plan["keeps"]:
        k["tab"]["userContextId"] = ws_ctx.get(k["ws_uuid"], 0)
        k["tab"]["zenEssential"] = True
        k["tab"]["pinned"] = True

    # 3. Adds: synthesize new essentials from template.
    for a in plan["adds"]:
        new_tab = synth_tab(template, url=a["url"],
                            title=a["title"],
                            ws_uuid=a["ws_uuid"],
                            uctx=ws_ctx.get(a["ws_uuid"], 0),
                            essential=True)
        obj.setdefault("tabs", []).append(new_tab)

    # 3b. Pinned-tab adds: synthesize regular (non-essential) tabs.
    # Create Zen folders for Arc folders. Zen requires entries in BOTH
    # obj['folders'] and obj['groups'], plus an empty placeholder tab.
    folder_groups: dict[tuple[str, str], str] = {}  # (ws_uuid, folder) -> folder_id
    for a in plan.get("pinned_adds", []):
        folder = a.get("folder")
        if folder:
            key = (a["ws_uuid"], folder)
            if key not in folder_groups:
                fid = _new_sync_id()
                folder_groups[key] = fid
                # Create empty placeholder tab
                empty_tab = _make_empty_tab_for_folder(fid, a["ws_uuid"])
                obj.setdefault("tabs", []).append(empty_tab)
                # Create the folder entry
                obj.setdefault("folders", []).append(
                    _make_folder_entry(fid, folder, a["ws_uuid"],
                                       empty_tab["zenSyncId"]))
                # Create the corresponding group entry
                obj.setdefault("groups", []).append(
                    _make_group_entry(fid, folder))

    for a in plan.get("pinned_adds", []):
        folder = a.get("folder")
        gid = folder_groups.get((a["ws_uuid"], folder)) if folder else None
        new_tab = synth_tab(template, url=a["url"],
                            title=a["title"],
                            ws_uuid=a["ws_uuid"],
                            uctx=ws_ctx.get(a["ws_uuid"], 0),
                            essential=False,
                            group_id=gid)
        obj.setdefault("tabs", []).append(new_tab)

    # 4. Extras: prune or leave but fix uctx.
    if prune and plan["extras"]:
        extra_ids = {id(e["tab"]) for e in plan["extras"]}
        obj["tabs"] = [t for t in obj.get("tabs") or [] if id(t) not in extra_ids]
    else:
        for e in plan["extras"]:
            ctx = ws_ctx.get(e["ws_uuid"], 0)
            if ctx > 0:
                e["tab"]["userContextId"] = ctx

    # 5. Optional: also re-tag non-essential pinned tabs to their workspace's
    # container so the container-isolation applies there too.
    if also_pinned:
        for t in obj.get("tabs") or []:
            if not isinstance(t, dict):
                continue
            if t.get("zenEssential"):
                continue  # handled above
            if not t.get("pinned"):
                continue
            ws_uuid = t.get("zenWorkspace")
            ctx = ws_ctx.get(ws_uuid, 0)
            if ctx > 0:
                t["userContextId"] = ctx

    # 6. Apply workspace icons and colors from Arc.
    ws_styles = plan.get("ws_styles", {})
    if ws_styles:
        for sp in obj.get("spaces") or []:
            if not isinstance(sp, dict):
                continue
            style = ws_styles.get(sp.get("uuid"))
            if not style:
                continue
            if style.get("icon") and not sp.get("icon"):
                sp["icon"] = style["icon"]
            color = style.get("color")
            if color:
                theme = sp.setdefault("theme", {
                    "type": "gradient", "gradientColors": [],
                    "opacity": 0.5, "texture": 0,
                })
                if not theme.get("gradientColors"):
                    theme["gradientColors"] = [{
                        "c": color,
                        "isCustom": False,
                        "algorithm": "floating",
                        "isPrimary": True,
                        "lightness": "60",
                        "position": {"x": 81, "y": 152},
                        "type": "explicit-lightness",
                    }]


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
        f.write("\n# Added by reconcile_zen_with_arc.py\n")
        for line in to_add:
            f.write(line + "\n")
    log(f"  Updated {path} (+{len(to_add)} prefs)")


# --------------------------------------------------------------------------- #
def print_arc_inventory(arc: dict) -> None:
    log("=== Arc inventory ===")
    log("Spaces:")
    for sp in arc["spaces"]:
        icon = sp.get("icon") or ""
        color = sp.get("color")
        color_str = f"  color=#{color[0]:02x}{color[1]:02x}{color[2]:02x}" if color else ""
        log(f"  - {icon}{sp['name']!r}  profile_key={sp['profile_key']!r}  "
            f"pinned={len(sp['pinned'])}  unpinned={len(sp['unpinned'])}{color_str}")
        for t in sp["pinned"][:50]:
            folder = t.get('folder')
            tag = f" [{folder}]" if folder else ""
            log(f"      pinned: {t['title'][:60]:<60}{tag}  {t['url']}")
        if len(sp["pinned"]) > 50:
            log(f"      ... +{len(sp['pinned'])-50} more pinned")
    log("\nTop apps by profile:")
    for prof, items in arc["topapps_by_profile"].items():
        log(f"  profile_key={prof!r}  ({len(items)} items)")
        for t in items:
            log(f"    - {t['title'][:60]:<60}  {t['url']}")


def print_plan(plan: dict) -> None:
    log("\n=== Reconcile plan ===")
    log(f"  totals: moves={len(plan['moves'])}  adds={len(plan['adds'])}  "
        f"keeps={len(plan['keeps'])}  pinned_adds={len(plan.get('pinned_adds', []))}  "
        f"extras={len(plan['extras'])}")

    ws_name = {w["uuid"]: w["name"] for w in plan["ws_summary"]}
    log("\n  per-workspace summary:")
    for w in plan["ws_summary"]:
        log(f"    - {w['name']!r}  (container {w['containerTabId']})  "
            f"desired={w['desired']}  keep={w['keep']}  "
            f"move_in={w['move_in']}  move_out={w['move_out']}  "
            f"add={w['add']}  pinned_add={w.get('pinned_add', 0)}  "
            f"extras_here={w['extras_in_ws']}")

    if plan["moves"]:
        log("\n  MOVES (existing essential -> correct workspace):")
        for m in plan["moves"][:50]:
            log(f"    ~ {m['title'][:45]:<45}  "
                f"{ws_name.get(m['from_ws'], '?')!r:>15} -> "
                f"{ws_name.get(m['to_ws'], '?')!r:<15}  {m['url']}")
        if len(plan["moves"]) > 50:
            log(f"    ~ ... +{len(plan['moves'])-50} more")

    if plan["adds"]:
        log("\n  ADDS (new essential):")
        for a in plan["adds"][:50]:
            log(f"    + {a['title'][:45]:<45}  "
                f"-> {ws_name.get(a['ws_uuid'], '?')!r:<15}  {a['url']}")
        if len(plan["adds"]) > 50:
            log(f"    + ... +{len(plan['adds'])-50} more")

    if plan.get("pinned_adds"):
        log("\n  PINNED TAB ADDS (Arc per-Space pinned -> Zen pinned tab):")
        for a in plan["pinned_adds"][:50]:
            folder = a.get('folder')
            tag = f" [{folder}]" if folder else ""
            log(f"    + {a['title'][:45]:<45}{tag}  "
                f"-> {ws_name.get(a['ws_uuid'], '?')!r:<15}  {a['url']}")
        if len(plan["pinned_adds"]) > 50:
            log(f"    + ... +{len(plan['pinned_adds'])-50} more")

    if plan["extras"]:
        log("\n  EXTRAS (in Zen but not in Arc — kept unless --prune):")
        for e in plan["extras"][:30]:
            log(f"    ? in {ws_name.get(e['ws_uuid'], '?')!r:<15}  {e['url']}")
        if len(plan["extras"]) > 30:
            log(f"    ? ... +{len(plan['extras'])-30} more")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--dump-arc", action="store_true",
                    help="Print the parsed Arc inventory and exit.")
    ap.add_argument("--include-unpinned", action="store_true",
                    help="Also import each Arc Space's UNpinned tabs as "
                         "regular tabs. Off by default — usually a lot of stuff.")
    ap.add_argument("--pinned-too", action="store_true",
                    help="Also retag existing non-essential PINNED tabs to "
                         "their workspace's container.")
    ap.add_argument("--prune", action="store_true",
                    help="Delete Zen essentials whose URL is not in Arc. "
                         "Off by default — extras are kept.")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Create Zen workspaces (and matching containers) for "
                         "Arc Spaces that have no matching Zen workspace. "
                         "Will rename a default-named 'Space' workspace to the "
                         "first missing Arc Space.")
    ap.add_argument("--topapps-target", default="Personal",
                    help="Zen workspace name where Arc top-apps (Favorites) "
                         "land. Default: Personal. Use 'none' to skip.")
    ap.add_argument("--map", dest="mapping",
                    help='Override name matching: '
                         '"Arc Space=Zen Workspace,Other Arc=Other Zen"')
    ap.add_argument("--force-running", action="store_true",
                    help="Skip the Zen-running guard.")
    args = ap.parse_args()
    dry_run = not args.apply

    if not args.force_running and is_app_running("Zen Browser"):
        raise SystemExit("Zen Browser is running. Quit with Cmd+Q first, or "
                         "pass --force-running.")

    if not ARC_SIDEBAR.exists():
        raise SystemExit(f"Arc data not found: {ARC_SIDEBAR}")

    log(f"Mode: {'DRY-RUN (no writes)' if dry_run else 'APPLY'}")
    log(f"Reading Arc:  {ARC_SIDEBAR}")
    arc = parse_arc(ARC_SIDEBAR)
    log(f"  spaces: {len(arc['spaces'])}   "
        f"profiles with topapps: {len(arc['topapps_by_profile'])}")

    if args.dump_arc:
        print_arc_inventory(arc)
        return

    profile = find_zen_profile()
    log(f"Zen profile:  {profile}")
    obj, sess_path = zen_load_session(profile)
    zen_ws = zen_workspaces(obj)
    log(f"  Zen workspaces ({len(zen_ws)}):")
    for w in zen_ws:
        log(f"    - {w['name']!r}  ({w['uuid']})  containerTabId={w['containerTabId']}")

    explicit_map = parse_map(args.mapping)
    if explicit_map:
        log(f"  explicit mapping: {explicit_map}")

    # Load containers up front; bootstrap may also need to mutate them.
    containers = load_containers(profile)

    # Bootstrap missing workspaces if requested. This runs before the
    # validate step so the validator sees the just-created workspaces.
    if args.bootstrap:
        log("\nBootstrapping missing Zen workspaces from Arc...")
        bs = bootstrap_missing_workspaces(obj, arc, containers, explicit_map)
        if bs["renamed"]:
            log(f"  ~ Renamed default workspace: "
                f"{bs['renamed']['from']!r} -> {bs['renamed']['to']!r}")
        if bs["created"]:
            log(f"  + Created {len(bs['created'])} workspace(s):")
            for c in bs["created"]:
                log(f"      {c['name']!r}  uuid={c['uuid']}  "
                    f"container_uctx={c['container_uctx']}")
        if not bs["renamed"] and not bs["created"]:
            log("  All Arc Spaces already have matching Zen workspaces.")
        # Refresh zen_ws view.
        zen_ws = zen_workspaces(obj)
        log(f"  Zen workspaces now ({len(zen_ws)}):")
        for w in zen_ws:
            log(f"    - {w['name']!r}  ({w['uuid']})  "
                f"containerTabId={w['containerTabId']}")

    # Treat Zen's workspace<->container binding as suspect. Validate against
    # containers.json and repair any workspace that lacks a real, public
    # container. New containers get created with the workspace name.
    log("\nValidating Zen workspace -> container bindings...")
    container_changes = validate_and_repair_workspace_containers(obj, containers)
    if container_changes["created_containers"]:
        log(f"  Need to create {len(container_changes['created_containers'])} container(s):")
        for c in container_changes["created_containers"]:
            log(f"    + uctx={c['userContextId']}  name={c['name']!r}  "
                f"color={c['color']}  icon={c['icon']}")
    if container_changes["rebound_workspaces"]:
        log(f"  Need to rebind {len(container_changes['rebound_workspaces'])} workspace(s):")
        for w in container_changes["rebound_workspaces"]:
            log(f"    ~ {w['name']!r}  containerTabId {w['old_ctx']} -> {w['new_ctx']}")
    if (not container_changes["created_containers"]
            and not container_changes["rebound_workspaces"]):
        log("  All workspaces already bound to real containers. No changes.")

    # Refresh zen_ws view in case containerTabIds changed.
    zen_ws = zen_workspaces(obj)

    plan = reconcile(arc, obj,
                     topapps_target_name=args.topapps_target,
                     include_unpinned=args.include_unpinned,
                     explicit_map=explicit_map)
    print_plan(plan)

    if dry_run:
        log("\nDry-run only. Re-run with --apply to commit.")
        return

    log("\nBacking up:")
    for p in (sess_path, profile / "containers.json", profile / "user.js"):
        b = backup(p)
        if b:
            log(f"  {p.name} -> {b.name}")

    # Persist any container fixes we computed.
    if (container_changes["created_containers"]
            or container_changes["rebound_workspaces"]):
        cpath = save_containers(profile, containers)
        log(f"Wrote {cpath} (containers + bindings).")

    log("\nApplying plan...")
    apply_plan(obj, plan, also_pinned=args.pinned_too, prune=args.prune)

    new_blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    out_bytes = mozlz4_write(new_blob)
    sess_path.write_bytes(out_bytes)
    log(f"Wrote {sess_path} ({len(out_bytes)} bytes on disk).")

    log("\nUser prefs:")
    write_user_js(profile, dry_run=False)

    log("\nDone. Launch Zen. Verify under about:preferences > Tab Management "
        "that 'Enable container-specific Essentials' is on.")
    log("If anything looks wrong, restore from the .bak-* files next to "
        f"{sess_path.name}.")


if __name__ == "__main__":
    main()