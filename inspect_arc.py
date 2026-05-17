#!/usr/bin/env python3
"""
Structural inspector for Arc's StorableSidebar.json. Read-only. The goal is
to find where Spaces live and how they reference their pinned/topApps
containers, so the reconcile script can parse them correctly.

Output is schema-only — no URLs, just shape/keys/counts. Safe to paste.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ARC_SIDEBAR = Path.home() / "Library" / "Application Support" / "Arc" / "StorableSidebar.json"


def kshape(d: dict, k: str) -> str:
    v = d.get(k)
    if v is None:
        return "None"
    if isinstance(v, dict):
        return f"dict(keys={len(v)})"
    if isinstance(v, list):
        return f"list(len={len(v)})"
    return type(v).__name__


def top_level_structure(obj, depth=0, max_depth=4, prefix=""):
    pad = "  " * depth
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                print(f"{pad}{k}: dict (keys={len(v)})")
                if depth < max_depth:
                    top_level_structure(v, depth + 1, max_depth)
            elif isinstance(v, list):
                print(f"{pad}{k}: list (len={len(v)})")
                if v and depth < max_depth:
                    sample = v[0]
                    if isinstance(sample, (dict, list)):
                        print(f"{pad}  [0]:")
                        top_level_structure(sample, depth + 2, max_depth)
                    else:
                        print(f"{pad}  [0]: {type(sample).__name__}")
            else:
                print(f"{pad}{k}: {type(v).__name__}")
    elif isinstance(obj, list) and obj and depth < max_depth:
        top_level_structure(obj[0], depth + 1, max_depth)


def find_space_like(obj, path="$", hits=None):
    """A 'space-like' object has a containerIDs array and either a title or a
    customInfo or a profile field."""
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        cid = obj.get("containerIDs")
        if isinstance(cid, list) and len(cid) >= 2 and (
            "title" in obj or "customInfo" in obj or "profile" in obj
        ):
            # Build a "marker map" from containerIDs (assumed alternating
            # marker, uuid).
            markers = {}
            for i in range(0, len(cid) - 1, 2):
                m, u = cid[i], cid[i + 1]
                if isinstance(m, str):
                    markers.setdefault(m, []).append(u if isinstance(u, str) else None)
            hits.append({
                "path": path,
                "id": obj.get("id"),
                "title": obj.get("title"),
                "has_customInfo": isinstance(obj.get("customInfo"), dict),
                "has_profile": isinstance(obj.get("profile"), dict),
                "containerIDs_len": len(cid),
                "markers": markers,
            })
        for k, v in obj.items():
            find_space_like(v, f"{path}.{k}", hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_space_like(v, f"{path}[{i}]", hits)
    return hits


def find_container_like(obj, path="$", hits=None):
    """Anything with childrenIds (excluding spaces themselves)."""
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        if isinstance(obj.get("childrenIds"), list):
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            ic = data.get("itemContainer") if isinstance(data, dict) else None
            ct = ic.get("containerType") if isinstance(ic, dict) else None
            hits.append({
                "path": path,
                "id": obj.get("id"),
                "children": len(obj["childrenIds"]),
                "containerType_keys": (list(ct.keys()) if isinstance(ct, dict) else None),
                "has_data": bool(data),
                "data_keys": (list(data.keys())[:6] if isinstance(data, dict) else []),
            })
        for v in obj.values():
            find_container_like(v, path, hits)
    elif isinstance(obj, list):
        for v in obj:
            find_container_like(v, path, hits)
    return hits


def find_tab_like(obj, hits=None):
    """Items with data.tab.savedURL — to confirm where tab leaves live."""
    if hits is None:
        hits = [0]
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict):
            tab = data.get("tab")
            if isinstance(tab, dict) and tab.get("savedURL"):
                hits[0] += 1
        for v in obj.values():
            find_tab_like(v, hits)
    elif isinstance(obj, list):
        for v in obj:
            find_tab_like(v, hits)
    return hits[0]


def main() -> None:
    if not ARC_SIDEBAR.exists():
        raise SystemExit(f"Not found: {ARC_SIDEBAR}")
    data = json.loads(ARC_SIDEBAR.read_text(encoding="utf-8"))
    print(f"File: {ARC_SIDEBAR}\nSize: {ARC_SIDEBAR.stat().st_size} bytes\n")

    print("=== Top-level structure (depth 4) ===")
    top_level_structure(data, max_depth=4)

    print("\n=== Space-like objects ===")
    spaces = find_space_like(data)
    print(f"Total: {len(spaces)}")
    # Distinct marker names across spaces
    marker_count = Counter()
    for s in spaces:
        for m in s["markers"]:
            marker_count[m] += 1
    print(f"Distinct containerID markers across spaces: {dict(marker_count)}")
    for s in spaces[:20]:
        print(f"  path: {s['path']}")
        print(f"    id={s['id']}  title={s['title']!r}  "
              f"customInfo={s['has_customInfo']}  profile={s['has_profile']}  "
              f"containerIDs_len={s['containerIDs_len']}")
        print(f"    markers: {list(s['markers'].keys())}")
    if len(spaces) > 20:
        print(f"  ... +{len(spaces)-20} more")

    print("\n=== Container-like objects (has childrenIds) ===")
    conts = find_container_like(data)
    # Tally containerType keys
    ct_keys = Counter()
    for c in conts:
        for k in (c["containerType_keys"] or []):
            ct_keys[k] += 1
    print(f"Total containers: {len(conts)}")
    print(f"Distinct containerType keys: {dict(ct_keys)}")
    # Show a few examples per containerType
    examples = {}
    for c in conts:
        for k in (c["containerType_keys"] or []):
            examples.setdefault(k, []).append(c)
    for k, lst in examples.items():
        print(f"  containerType.{k}: {len(lst)} container(s); first example:")
        print(f"    path: {lst[0]['path']}")
        print(f"    id={lst[0]['id']}  children={lst[0]['children']}  "
              f"data_keys={lst[0]['data_keys']}")

    print("\n=== Tab counts ===")
    print(f"Items with data.tab.savedURL: {find_tab_like(data)}")


if __name__ == "__main__":
    main()