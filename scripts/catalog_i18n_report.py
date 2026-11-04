#!/usr/bin/env python3
"""Report which catalog entries still need a translation for the Desktop content layer.

Read-only and offline: it reads the repository's own catalogs (skills, optional skills, plugin
manifests, the plugin catalog, optional MCP manifests), compares them with a language dictionary
(`apps/desktop/src/i18n/catalog-content/<lang>.json`) and its source-hash lock
(`<lang>.sources.json`), and prints three lists:

- missing: no translation yet;
- stale:   translated, but the English name/description changed since the translation was reviewed;
- orphaned: a translation whose id no longer exists in the catalogs (kept, never deleted).

Nothing is translated here and no network or model is used. A human (or an approved, separate
process) writes translations; ``--update-lock`` then records the hashes of the reviewed sources so
unchanged entries are never reprocessed.

Usage:
    python scripts/catalog_i18n_report.py --lang ru [--limit 200] [--out report.json]
    python scripts/catalog_i18n_report.py --lang ru --update-lock
    python scripts/catalog_i18n_report.py --lang ru --extra-inventory tools.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

KINDS = ("skill", "plugin", "mcp", "tool", "toolset")
DICT_DIR = Path("apps/desktop/src/i18n/catalog-content")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


def source_hash(name: str, description: str) -> str:
    """Stable fingerprint of the English text a translation was written for."""
    payload = json.dumps([_squash(name), _squash(description)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _squash(value: object) -> str:
    return " ".join(str(value or "").split())


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _skill_rows(root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for path in sorted(root.rglob("SKILL.md")):
        match = _FRONTMATTER.match(path.read_text(encoding="utf-8", errors="replace"))
        meta = yaml.safe_load(match.group(1)) if match else {}
        meta = meta if isinstance(meta, dict) else {}
        name = _squash(meta.get("name") or path.parent.name)
        rows.setdefault(
            name, {"name": name, "description": _squash(meta.get("description"))}
        )
    return rows


def _manifest_rows(paths, id_key: str = "name") -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for path in sorted(paths):
        meta = _load_yaml(path)
        name = _squash(meta.get(id_key) or path.parent.name)
        rows.setdefault(
            name, {"name": name, "description": _squash(meta.get("description"))}
        )
    return rows


def collect_inventory(
    repo: Path, extra: Path | None = None
) -> dict[str, dict[str, dict]]:
    """Id -> English text for every catalog this repository itself carries."""
    inventory: dict[str, dict[str, dict]] = {kind: {} for kind in KINDS}
    for folder in ("skills", "optional-skills"):
        if (repo / folder).is_dir():
            inventory["skill"].update(_skill_rows(repo / folder))
    plugins = inventory["plugin"]
    catalog = (
        sorted((repo / "plugin-catalog").glob("*.yaml"))
        if (repo / "plugin-catalog").is_dir()
        else []
    )
    plugins.update(_manifest_rows(p for p in catalog if p.name != "removed.yaml"))
    plugins.update(
        _manifest_rows((repo / "plugins").rglob("plugin.yaml"))
        if (repo / "plugins").is_dir()
        else {}
    )
    if (repo / "optional-mcps").is_dir():
        inventory["mcp"].update(
            _manifest_rows((repo / "optional-mcps").glob("*/manifest.yaml"))
        )
    if extra is not None:  # tools/toolsets need the live registry; supply them as JSON
        data = json.loads(extra.read_text(encoding="utf-8"))
        for kind in KINDS:
            for key, row in (data.get(kind) or {}).items():
                inventory[kind][key] = {
                    "name": _squash(row.get("name") or key),
                    "description": _squash(row.get("description")),
                }
    return inventory


def diff(
    inventory: dict, dictionary: dict, lock: dict, limit: int | None = None
) -> dict:
    """Missing / stale / orphaned ids per kind; never mutates anything."""
    report: dict[str, dict] = {}
    for kind in KINDS:
        known = dictionary.get(kind) or {}
        locked = lock.get(kind) or {}
        missing, stale = [], []
        for key, row in sorted(inventory.get(kind, {}).items()):
            if key not in known:
                missing.append(key)
            elif key in locked and locked[key] != source_hash(
                row["name"], row["description"]
            ):
                stale.append(key)
        orphaned = sorted(set(known) - set(inventory.get(kind, {})))
        report[kind] = {
            "total": len(inventory.get(kind, {})),
            "translated": len(inventory.get(kind, {}).keys() & known.keys()),
            "missing": missing[:limit] if limit else missing,
            "missing_count": len(missing),
            "stale": stale[:limit] if limit else stale,
            "stale_count": len(stale),
            "orphaned_count": len(orphaned),
        }
    return report


def format_problems(dictionary: dict) -> list[str]:
    """Structural problems that make a dictionary unsafe to ship."""
    problems: list[str] = []
    for kind in KINDS:
        for key, row in (dictionary.get(kind) or {}).items():
            if key != key.strip() or re.search(r"\s", key):
                problems.append(f"{kind}:{key!r}: id has whitespace")
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{kind}:{key}: empty or missing name")
            elif "�" in name or "�" in str(row.get("description", "")):
                problems.append(f"{kind}:{key}: broken Unicode")
    return problems


def update_lock(inventory: dict, dictionary: dict, lock: dict) -> dict:
    """Record the current English hash for every translated entry (marks the translation reviewed)."""
    updated = {kind: dict(lock.get(kind) or {}) for kind in KINDS}
    for kind in KINDS:
        for key, row in inventory.get(kind, {}).items():
            if key in (dictionary.get(kind) or {}):
                updated[kind][key] = source_hash(row["name"], row["description"])
    return updated


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--lang", required=True)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="entries listed per kind (counts stay exact)",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--extra-inventory", type=Path)
    parser.add_argument("--update-lock", action="store_true")
    parser.add_argument("--fail-on-stale", action="store_true")
    args = parser.parse_args(argv)

    folder = args.repo / DICT_DIR
    dictionary = _read_json(folder / f"{args.lang}.json")
    lock_path = folder / f"{args.lang}.sources.json"
    lock = _read_json(lock_path)
    inventory = collect_inventory(args.repo, args.extra_inventory)

    problems = format_problems(dictionary)
    if args.update_lock:
        if problems:
            print(
                "Refusing to update the lock; fix the dictionary first:\n  "
                + "\n  ".join(problems),
                file=sys.stderr,
            )
            return 2
        lock_path.write_text(
            json.dumps(
                update_lock(inventory, dictionary, lock),
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {lock_path}")
        return 0

    report = diff(inventory, dictionary, lock, args.limit)
    report["format_problems"] = problems
    if args.out:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    for kind in KINDS:
        row = report[kind]
        print(
            f"{kind:8} {row['translated']:5}/{row['total']:<5} missing={row['missing_count']:<5} stale={row['stale_count']:<4} orphaned={row['orphaned_count']}"
        )
    for problem in problems[:20]:
        print("format:", problem, file=sys.stderr)
    failed = bool(problems) or (
        args.fail_on_stale and any(report[k]["stale_count"] for k in KINDS)
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
