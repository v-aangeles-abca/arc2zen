#!/usr/bin/env python3
"""
Inspect a Zen profile so we can plan the containerize fix.

Reads (never writes):
  - places.sqlite : lists tables and any zen_* table row counts
  - zen-sessions.jsonlz4 : decompresses mozLz4, dumps a redacted structure
  - containers.json : current Firefox containers

Outputs:
  - prints a structural summary to the terminal
  - writes zen-sessions.decoded.json (full decompressed JSON, plaintext)
    next to this script, so you can grep / open in an editor

Requires `lz4`:
    pip install lz4
"""

from __future__ import annotations

import configparser
import json
import sqlite3
import struct
import sys
from pathlib import Path

HOME = Path.home()
ZEN_DIR = HOME / "Library" / "Application Support" / "zen"


def find_profile() -> Path:
    ini = ZEN_DIR / "profiles.ini"
    if not ini.exists():
        raise SystemExit(f"profiles.ini not found at {ini}")
    cp = configparser.ConfigParser()
    cp.read(ini)
    default_rel = None
    for s in cp.sections():
        if s.startswith("Install"):
            default_rel = cp[s].get("Default")
            if default_rel:
                break
    if not default_rel:
        for s in cp.sections():
            if s.startswith("Profile") and cp[s].get("Default", "0") == "1":
                default_rel = cp[s].get("Path", "")
                break
    if not default_rel:
        raise SystemExit("Could not determine default profile in profiles.ini")
    p = (ZEN_DIR / default_rel).resolve()
    if not p.exists():
        raise SystemExit(f"Profile dir not found: {p}")
    return p


def read_mozlz4(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != b"mozLz40\0":
        raise SystemExit(f"{path.name} is not a mozLz4 file (bad magic).")
    decompressed_size = struct.unpack("<I", raw[8:12])[0]
    try:
        import lz4.block  # type: ignore
    except ImportError:
        raise SystemExit("Missing dependency. Install with:  pip install lz4")
    return lz4.block.decompress(raw[12:], uncompressed_size=decompressed_size)


def describe(obj, depth: int = 0, max_depth: int = 6, max_list_items: int = 2) -> list[str]:
    """Walk a JSON-ish object and return a list of '  path: type (len=...)' lines."""
    pad = "  " * depth
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                lines.append(f"{pad}{k}: dict (keys={len(v)})")
                if depth < max_depth:
                    lines.extend(describe(v, depth + 1, max_depth, max_list_items))
            elif isinstance(v, list):
                lines.append(f"{pad}{k}: list (len={len(v)})")
                if v and depth < max_depth:
                    lines.append(f"{pad}  [0]:")
                    lines.extend(describe(v[0], depth + 2, max_depth, max_list_items))
                    if len(v) > 1 and depth < max_depth - 1:
                        lines.append(f"{pad}  [1]:")
                        lines.extend(describe(v[1], depth + 2, max_depth, max_list_items))
            else:
                preview = repr(v)
                if len(preview) > 100:
                    preview = preview[:97] + "..."
                lines.append(f"{pad}{k}: {type(v).__name__} = {preview}")
    elif isinstance(obj, list):
        lines.append(f"{pad}(list len={len(obj)})")
        if obj and depth < max_depth:
            lines.extend(describe(obj[0], depth + 1, max_depth, max_list_items))
    else:
        lines.append(f"{pad}{type(obj).__name__} = {obj!r}")
    return lines


def find_keys_recursive(obj, target_keys: set[str]) -> dict[str, list]:
    """Find every occurrence of any of `target_keys` and a short path to it."""
    found: dict[str, list] = {k: [] for k in target_keys}

    def walk(node, path: str = "$"):
        if isinstance(node, dict):
            for k, v in node.items():
                here = f"{path}.{k}"
                if k in target_keys:
                    sample = v
                    if isinstance(sample, (dict, list)):
                        sample = f"({type(sample).__name__} len={len(sample)})"
                    else:
                        s = repr(sample)
                        sample = s if len(s) < 80 else s[:77] + "..."
                    found[k].append((here, sample))
                walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(obj)
    return found


def main() -> None:
    profile = find_profile()
    print(f"Profile: {profile}\n")

    # 1. SQLite tables
    places = profile / "places.sqlite"
    if places.exists():
        con = sqlite3.connect(places)
        print("== places.sqlite tables ==")
        rows = list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ))
        for (name,) in rows:
            try:
                cnt = con.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0]
            except sqlite3.Error:
                cnt = "?"
            marker = "  <- ZEN" if name.startswith("zen") else ""
            print(f"  {name}  (rows={cnt}){marker}")
        con.close()
        print()
    else:
        print(f"!! places.sqlite not found at {places}\n")

    # 2. zen-sessions.jsonlz4
    sess = profile / "zen-sessions.jsonlz4"
    if sess.exists():
        print(f"== zen-sessions.jsonlz4 ({sess.stat().st_size} bytes on disk) ==")
        data = read_mozlz4(sess)
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Decoded but JSON parse failed: {e}")

        # Dump full decompressed JSON next to this script.
        out = Path(__file__).resolve().parent / "zen-sessions.decoded.json"
        out.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        print(f"   (decoded -> {out})")

        # High-level structure (capped).
        print("\n   -- top-level structure --")
        for line in describe(obj, max_depth=3):
            print("   " + line)

        # Targeted scan for likely interesting keys.
        print("\n   -- key scan (paths and small samples) --")
        targets = {
            "workspaces", "workspace", "spaces", "space",
            "essentials", "essential",
            "pinned", "pins", "pin",
            "userContextId", "containerId", "container",
            "defaultContainerId", "containerUserContextId",
            "uuid", "id", "name",
        }
        scan = find_keys_recursive(obj, targets)
        for key in sorted(scan):
            hits = scan[key]
            if not hits:
                continue
            print(f"\n   key: {key}  ({len(hits)} hits)")
            for path, sample in hits[:5]:
                print(f"     {path}  ->  {sample}")
            if len(hits) > 5:
                print(f"     ... +{len(hits)-5} more")
    else:
        print(f"!! zen-sessions.jsonlz4 not found at {sess}")

    # 3. containers.json
    cont = profile / "containers.json"
    if cont.exists():
        c = json.loads(cont.read_text(encoding="utf-8"))
        print(f"\n== containers.json ==")
        print(f"   version: {c.get('version')}   lastUserContextId: {c.get('lastUserContextId')}")
        print(f"   identities: {len(c.get('identities', []))}")
        for ident in c.get("identities", []):
            print(f"     - uctx={ident.get('userContextId')}  "
                  f"name={ident.get('name')!r}  "
                  f"color={ident.get('color')!r}  "
                  f"icon={ident.get('icon')!r}  "
                  f"public={ident.get('public')}")
    else:
        print(f"!! containers.json not found at {cont}")


if __name__ == "__main__":
    main()
