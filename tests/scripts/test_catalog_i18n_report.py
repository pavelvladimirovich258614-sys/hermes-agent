"""Behavior tests for scripts/catalog_i18n_report.py (offline, temp repo only)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "catalog_i18n_report.py"
spec = importlib.util.spec_from_file_location("catalog_i18n_report", SCRIPT)
report_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_mod)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "skills" / "tools" / "alpha").mkdir(parents=True)
    (tmp_path / "skills" / "tools" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Does alpha things.\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "optional-skills" / "beta").mkdir(parents=True)
    (tmp_path / "optional-skills" / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: Does beta things.\n---\n", encoding="utf-8"
    )
    (tmp_path / "optional-mcps" / "gamma").mkdir(parents=True)
    (tmp_path / "optional-mcps" / "gamma" / "manifest.yaml").write_text(
        "name: gamma\ndescription: Gamma server.\n", encoding="utf-8"
    )
    (tmp_path / "plugin-catalog").mkdir()
    (tmp_path / "plugin-catalog" / "delta.yaml").write_text(
        "name: delta\ndescription: Delta plugin.\n", encoding="utf-8"
    )
    (tmp_path / "plugin-catalog" / "removed.yaml").write_text(
        "name: nope\n", encoding="utf-8"
    )
    return tmp_path


def _write_dict(repo: Path, lang: str, data: dict, lock: dict | None = None) -> None:
    folder = repo / report_mod.DICT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{lang}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    if lock is not None:
        (folder / f"{lang}.sources.json").write_text(json.dumps(lock), encoding="utf-8")


def test_inventory_reads_every_local_catalog_and_skips_removed(tmp_path):
    inv = report_mod.collect_inventory(_repo(tmp_path))
    assert set(inv["skill"]) == {"alpha", "beta"}
    assert set(inv["mcp"]) == {"gamma"}
    assert set(inv["plugin"]) == {"delta"}


def test_missing_stale_and_orphaned_are_reported(tmp_path):
    repo = _repo(tmp_path)
    inv = report_mod.collect_inventory(repo)
    old_hash = report_mod.source_hash("alpha", "Does alpha things (old).")
    dictionary = {"skill": {"alpha": {"name": "Альфа"}, "gone": {"name": "Ушедший"}}}
    out = report_mod.diff(inv, dictionary, {"skill": {"alpha": old_hash}})
    assert out["skill"]["missing"] == ["beta"]
    assert out["skill"]["stale"] == ["alpha"]
    assert out["skill"]["orphaned_count"] == 1


def test_unchanged_translation_is_not_reprocessed_and_lock_records_it(tmp_path):
    repo = _repo(tmp_path)
    inv = report_mod.collect_inventory(repo)
    dictionary = {"skill": {"alpha": {"name": "Альфа"}}}
    lock = report_mod.update_lock(inv, dictionary, {})
    out = report_mod.diff(inv, dictionary, lock)
    assert out["skill"]["stale"] == [] and out["skill"]["missing"] == ["beta"]


def test_limit_caps_listing_but_not_counts(tmp_path):
    inv = report_mod.collect_inventory(_repo(tmp_path))
    out = report_mod.diff(inv, {}, {}, limit=1)
    assert len(out["skill"]["missing"]) == 1 and out["skill"]["missing_count"] == 2


def test_format_problems_catch_empty_names_and_broken_unicode():
    dictionary = {
        "skill": {
            "ok": {"name": "Норма"},
            "empty": {"name": " "},
            "bad": {"name": "x�"},
        }
    }
    problems = report_mod.format_problems(dictionary)
    assert any("empty" in p for p in problems) and any(
        "broken Unicode" in p for p in problems
    )
    assert not any(p.startswith("skill:ok") for p in problems)


def test_update_lock_refuses_a_broken_dictionary(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write_dict(repo, "ru", {"skill": {"alpha": {"name": ""}}})
    assert report_mod.main(["--lang", "ru", "--repo", str(repo), "--update-lock"]) == 2
    assert not (repo / report_mod.DICT_DIR / "ru.sources.json").exists()


def test_cli_never_deletes_or_edits_the_dictionary(tmp_path):
    repo = _repo(tmp_path)
    _write_dict(repo, "ru", {"skill": {"alpha": {"name": "Альфа"}}})
    path = repo / report_mod.DICT_DIR / "ru.json"
    before = path.read_bytes()
    assert (
        report_mod.main(
            ["--lang", "ru", "--repo", str(repo), "--out", str(tmp_path / "r.json")]
        )
        == 0
    )
    assert path.read_bytes() == before
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["skill"][
        "missing"
    ] == ["beta"]


def test_extra_inventory_adds_tools_and_toolsets(tmp_path):
    repo = _repo(tmp_path)
    extra = tmp_path / "extra.json"
    extra.write_text(
        json.dumps(
            {"tool": {"web_search": {"description": "Search"}}, "toolset": {"web": {}}}
        ),
        encoding="utf-8",
    )
    inv = report_mod.collect_inventory(repo, extra)
    assert "web_search" in inv["tool"] and "web" in inv["toolset"]
