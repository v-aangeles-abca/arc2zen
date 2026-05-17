#!/usr/bin/env python3
"""
Targeted Arc inspector v2. Answers two questions:
  1. For each Space, what's its profile.directoryBasename and where does
     'pinned' point to?
  2. For each `containerType.topApps` container, what's its identifier and
     how does it link back to a Space?

No URLs are printed. Safe to paste.
"""

from __future__ import annotations

import json
from pathlib import Path

ARC_SIDEBAR = Path.home() / "Library" / "Application Support" / "Arc" / "StorableSidebar.json"


def get_live_spaces(root) -> list[dict]:
    """Spaces live at $.sidebar.containers[1].spaces (per inspect_arc.py)."""
    try:
        return root["sidebar"]["containers"][1]["spaces"]
    except (KeyError, IndexError, TypeError):
        return []


def get_live_items(root) -> list[dict]:
    """Items array, sibling of spaces."""
    try:
        return root["sidebar"]["containers"][1]["items"]
    except (KeyError, IndexError, TypeError):
        return []


def get_topapps_container_ids(root):
    """The mapping list at sidebarSyncState.container.value.topAppsContainerIDs."""
    try:
        return root["sidebarSyncState"]["container"]["value"]["topAppsContainerIDs"]
    except (KeyError, TypeError):
        return None


def get_topapps_container_id(root):
    try:
        return root["sidebarSyncState"]["container"]["value"]["topAppsContainerID"]
    except (KeyError, TypeError):
        return None


def find_items_by_id(items: list[dict], wanted_ids: set[str]) -> dict[str, dict]:
    """Linear scan of the flat items array."""
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("id") in wanted_ids:
            out[it["id"]] = it
    return out


def main() -> None:
    root = json.loads(ARC_SIDEBAR.read_text(encoding="utf-8"))

    spaces = get_live_spaces(root)
    items = get_live_items(root)
    print(f"Live spaces: {len(spaces)}")
    print(f"Live items: {len(items)}\n")

    # -- 1. Space metadata
    space_meta = []
    print("=== Spaces (live) ===")
    for i, sp in enumerate(spaces):
        if not isinstance(sp, dict):
            continue
        title = sp.get("title")
        sp_id = sp.get("id")
        cids = sp.get("containerIDs") or []
        # profile
        prof_keys, prof_basename = [], None
        prof = sp.get("profile")
        if isinstance(prof, dict):
            prof_keys = list(prof.keys())
            default = prof.get("default")
            if isinstance(default, dict):
                prof_basename = default.get("directoryBasename")
            elif isinstance(prof.get("custom"), dict):
                cust = prof["custom"]
                cust_keys = list(cust.keys())
                prof_keys = prof_keys + [f"custom:{cust_keys}"]
                # Sometimes profile is {custom: {"_0": {"directoryBasename": ...}}}
                for v in cust.values():
                    if isinstance(v, dict) and v.get("directoryBasename"):
                        prof_basename = v["directoryBasename"]
                        break
        markers = {}
        for j in range(0, len(cids) - 1, 2):
            m, u = cids[j], cids[j + 1]
            if isinstance(m, str):
                markers[m] = u
        print(f"  [{i}] {title!r}  id={sp_id}")
        print(f"      profile_keys={prof_keys}  directoryBasename={prof_basename!r}")
        print(f"      markers={markers}")
        space_meta.append({"i": i, "id": sp_id, "title": title,
                           "profile": prof_basename, "pinned_cid": markers.get("pinned"),
                           "unpinned_cid": markers.get("unpinned")})

    # -- 2. topApps containers
    print("\n=== topApps containers ===")
    topapps = []
    for it in items:
        if not isinstance(it, dict):
            continue
        data = it.get("data")
        if not isinstance(data, dict):
            continue
        ic = data.get("itemContainer")
        if not isinstance(ic, dict):
            continue
        ct = ic.get("containerType")
        if not isinstance(ct, dict) or "topApps" not in ct:
            continue
        ta = ct["topApps"]
        child_count = len(it.get("childrenIds") or [])
        topapps.append({"id": it.get("id"), "topApps_value": ta,
                        "children": child_count})
    print(f"Total: {len(topapps)}")
    for ta in topapps:
        # Print the raw topApps value structure
        v = ta["topApps_value"]
        if isinstance(v, dict):
            v_repr = {k: (repr(val)[:80] if not isinstance(val, (dict, list))
                          else f"{type(val).__name__}(keys={list(val.keys())[:5]})"
                          if isinstance(val, dict)
                          else f"list(len={len(val)})")
                      for k, val in v.items()}
        else:
            v_repr = repr(v)[:120]
        print(f"  id={ta['id']}  children={ta['children']}")
        print(f"    topApps_value: {v_repr}")

    # -- 3. topAppsContainerIDs mapping
    print("\n=== topAppsContainerIDs mapping ===")
    mapping = get_topapps_container_ids(root)
    single = get_topapps_container_id(root)
    print(f"topAppsContainerID (single): {single!r}")
    if isinstance(mapping, list):
        print(f"topAppsContainerIDs (list of {len(mapping)}):")
        for i, m in enumerate(mapping):
            if isinstance(m, dict):
                # Common shape: {"custom": {"_0": ..., "_1": ...}} or similar
                shape = {}
                for k, v in m.items():
                    if isinstance(v, dict):
                        shape[k] = {kk: (repr(vv)[:100] if not isinstance(vv, (dict, list))
                                          else type(vv).__name__)
                                     for kk, vv in v.items()}
                    else:
                        shape[k] = repr(v)[:100]
                print(f"  [{i}] {shape}")
            else:
                print(f"  [{i}] {repr(m)[:120]}")
    else:
        print(f"  (not a list: {type(mapping).__name__})")

    # -- 4. Cross-check: does any topApps container id appear in the mapping?
    print("\n=== Cross-check ===")
    ta_ids = {t["id"] for t in topapps}
    if isinstance(mapping, list):
        flat_strs = []
        def collect_strs(o):
            if isinstance(o, str):
                flat_strs.append(o)
            elif isinstance(o, dict):
                for v in o.values():
                    collect_strs(v)
            elif isinstance(o, list):
                for v in o:
                    collect_strs(v)
        collect_strs(mapping)
        matches = ta_ids & set(flat_strs)
        print(f"topApps container ids referenced in topAppsContainerIDs: {len(matches)}/{len(ta_ids)}")
        # Show the first few orphans
        orphans = ta_ids - set(flat_strs)
        if orphans:
            print(f"  orphan topApps containers (not in mapping): "
                  f"{list(orphans)[:5]}")


if __name__ == "__main__":
    main()