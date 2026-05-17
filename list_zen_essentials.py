#!/usr/bin/env python3
"""
List every essential currently in zen-sessions.jsonlz4, grouped by
workspace. Read-only. Use this to confirm your Arc essentials made it
across (and to which workspace) before running the containerize fix.

Output columns:
  title | URL | userContextId | (✓ if matches workspace's containerTabId)

Requires:  pip install lz4
"""

from __future__ import annotations

import configparser
import json
import struct
from pathlib import Path

HOME = Path.home()
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"


def find_profile() -> Path:
    cp = configparser.ConfigParser()
    cp.read(ZEN_DIR / "profiles.ini")
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
        raise SystemExit("No default profile found in profiles.ini")
    return (ZEN_DIR / rel).resolve()


def mozlz4_decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != b"mozLz40\0":
        raise SystemExit(f"{path.name}: bad mozLz4 magic.")
    size = struct.unpack("<I", raw[8:12])[0]
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("pip install lz4")
    return lz4.block.decompress(raw[12:], uncompressed_size=size)


def main() -> None:
    profile = find_profile()
    sess = profile / "zen-sessions.jsonlz4"
    obj = json.loads(mozlz4_decompress(sess))

    # workspace map
    ws_name: dict[str, str] = {}
    ws_ctx: dict[str, int] = {}
    for sp in obj.get("spaces", []):
        uuid = sp.get("uuid")
        if uuid:
            ws_name[uuid] = sp.get("name", "?")
            ws_ctx[uuid] = sp.get("containerTabId", 0)

    # collect essentials
    by_ws: dict[str, list[dict]] = {}
    no_ws: list[dict] = []
    total = 0
    for t in obj.get("tabs", []):
        if not isinstance(t, dict) or not t.get("zenEssential"):
            continue
        total += 1
        entry0 = (t.get("entries") or [{}])[0]
        info = {
            "title": entry0.get("title") or "(no title)",
            "url": entry0.get("url") or "(no url)",
            "uctx": t.get("userContextId", 0),
            "ws": t.get("zenWorkspace"),
        }
        if info["ws"] and info["ws"] in ws_name:
            by_ws.setdefault(info["ws"], []).append(info)
        else:
            no_ws.append(info)

    print(f"Total essentials: {total}\n")
    for uuid, name in ws_name.items():
        items = by_ws.get(uuid, [])
        target = ws_ctx[uuid]
        print(f"=== {name}  (containerTabId={target})  — {len(items)} essential(s) ===")
        for it in items:
            ok = "✓" if it["uctx"] == target else "✗"
            print(f"  {ok}  uctx={it['uctx']:<3}  {it['title'][:50]:<50}  {it['url']}")
        print()

    if no_ws:
        print(f"=== Essentials NOT tied to any known workspace ({len(no_ws)}) ===")
        for it in no_ws:
            print(f"  uctx={it['uctx']:<3}  ws={it['ws']}  {it['title'][:50]:<50}  {it['url']}")


if __name__ == "__main__":
    main()
