# arc2zen — Migrate Arc Browser to Zen Browser

A set of Python scripts to migrate your tabs, workspaces, pinned sites, and folder structure from [Arc Browser](https://arc.net/) to [Zen Browser](https://zen-browser.app/).

## The Problem

Switching from Arc to Zen means manually recreating your entire workspace setup:
- Arc **Spaces** → Zen **Workspaces**
- Arc **Favorites** (top apps) → Zen **Essentials** (sidebar icons)
- Arc **Pinned Tabs** → Zen **Pinned Tabs**
- Arc **Folders** (tab groups within a Space) → Zen **Folders** (tab groups within a Workspace)
- Arc **Profiles** → Zen **Containers** (for per-workspace cookie isolation)

If you have multiple Spaces, each with folders and dozens of pinned tabs, doing this by hand is painful. This tool automates the entire migration.

## What It Does

The main script (`reconcile_zen_with_arc.py`) performs a single-pass reconciliation:

1. **Reads Arc's data** (`StorableSidebar.json`) to enumerate every pinned tab per Space, every folder, and every top-app (Favorite) per Profile.
2. **Reads Zen's session** (`zen-sessions.jsonlz4`) and container config to see what's already there.
3. **Maps Arc Spaces → Zen Workspaces** by name (case-insensitive, overridable via `--map`).
4. **Creates missing workspaces** in Zen for Arc Spaces that have no match (with `--bootstrap`).
5. **Synthesizes Essentials** for Arc's top-apps (Favorites) in each matched workspace.
6. **Imports pinned tabs** preserving Arc's folder structure as Zen tab groups.
7. **Sets up containers** so each workspace gets cookie isolation (matching Arc's per-Profile separation).
8. **Configures user.js** to enable container-specific Essentials.

## Safety

- **Default is DRY-RUN** — shows you what would change without writing anything.
- **Backs up** `zen-sessions.jsonlz4`, `containers.json`, and `user.js` before any write.
- **Requires Zen to be quit** (Cmd+Q on macOS). Override with `--force-running` if you know what you're doing.
- **Idempotent** — running it again after a successful apply is a no-op.

## Requirements

- **macOS** (paths are hardcoded for macOS; Linux/Windows contributions welcome)
- **Python 3.10+**
- **Arc Browser** installed (reads `~/Library/Application Support/Arc/StorableSidebar.json`)
- **Zen Browser** installed (reads/writes `~/Library/Application Support/zen/`)

## Installation

```bash
git clone https://github.com/yourusername/arc2zen.git
cd arc2zen
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

### 1. Dry-Run (Preview Changes)

```bash
python3 reconcile_zen_with_arc.py
```

This shows you:
- Which Arc Spaces map to which Zen Workspaces
- How many essentials, pinned tabs, and folders would be created
- Any name-matching issues

### 2. Dump Arc Inventory

```bash
python3 reconcile_zen_with_arc.py --dump-arc
```

Shows all Arc Spaces, pinned tabs (with folder groupings), and top-apps per profile. Useful for debugging or seeing what you have before migrating.

### 3. Full Migration (First Time)

If you're starting with a fresh Zen install (default "Space" workspace):

```bash
# Quit Zen first!
python3 reconcile_zen_with_arc.py --bootstrap --apply
```

`--bootstrap` creates Zen workspaces for each Arc Space, renames the default workspace, and sets up containers.

### 4. Apply Without Bootstrap

If you've already created workspaces in Zen with matching names:

```bash
python3 reconcile_zen_with_arc.py --apply
```

### 5. Custom Name Mapping

If your Arc Space names don't match your Zen Workspace names:

```bash
python3 reconcile_zen_with_arc.py --map "My Arc Space=My Zen Workspace,Work=Office" --apply
```

### 6. Include Unpinned Tabs

By default, only pinned tabs are imported. To also bring over unpinned tabs:

```bash
python3 reconcile_zen_with_arc.py --include-unpinned --apply
```

## All Options

| Flag | Description |
|------|-------------|
| `--apply` | Actually write changes (default is dry-run) |
| `--dump-arc` | Print Arc inventory and exit |
| `--bootstrap` | Create Zen workspaces for unmatched Arc Spaces |
| `--map "A=B,C=D"` | Override Space-to-Workspace name mapping |
| `--topapps-target NAME` | Workspace for orphan top-apps (default: `Personal`, use `none` to skip) |
| `--include-unpinned` | Also import Arc's unpinned tabs |
| `--pinned-too` | Re-tag existing non-essential pinned tabs to their workspace's container |
| `--prune` | Delete Zen essentials whose URL is not in Arc |
| `--force-running` | Skip the "Zen must be quit" safety check |

## How Folders Work

Arc folders (tab groups within a Space) are mapped to Zen's native folder system. Zen requires a specific internal structure for folders — each folder needs:

1. An entry in the session's `folders` array (with `workspaceId`, `emptyTabIds`, etc.)
2. A matching entry in the `groups` array (same `id`, with `saved`, `tabs`, `splitViews`)
3. An empty placeholder tab (`zenIsEmpty: true`) referenced by the folder

This script handles all of that automatically. Tabs within a folder are linked via the `groupId` field.

## Helper Scripts

| Script | Purpose |
|--------|---------|
| `inspect_arc.py` / `inspect_arc_v2.py` | Explore Arc's StorableSidebar.json structure |
| `inspect_zen.py` | Explore Zen's session file |
| `list_zen_essentials.py` | List current Zen essentials per workspace |
| `containerize_zen_workspaces.py` | Set up containers for existing workspaces |
| `containerize_zen_essentials_jsonlz4.py` | Tag essentials with container IDs |
| `migrate_arc_to_zen.py` | Earlier migration script (superseded by `reconcile_zen_with_arc.py`) |

## Restoring from Backup

If something goes wrong after `--apply`:

```bash
cd ~/Library/Application\ Support/zen/Profiles/<your-profile>/
cp zen-sessions.jsonlz4.bak-YYYYMMDD-HHMMSS zen-sessions.jsonlz4
cp containers.json.bak-YYYYMMDD-HHMMSS containers.json
cp user.js.bak-YYYYMMDD-HHMMSS user.js
```

Then relaunch Zen.

## Contributing

Contributions are welcome! Some areas that could use help:
- **Linux/Windows support** — path detection for Zen/Arc data directories
- **Better profile detection** — handling multiple Zen profiles
- **Two-way sync** — keeping Arc and Zen in sync during a transition period

## License

MIT
