"""
identity_import.py — Bootstrap an Offspring entity from a .pid file.

Usage:
    python entity/identity_import.py --file chloe_20260311.pid
    python entity/identity_import.py --file chloe_20260311.pid --entity faith  # rename on import

What this does:
    1. Reads the .pid file
    2. Validates the format and version
    3. Writes a personality prompt from the identity/personality/values sections
    4. Restores core memories to data/core_memories.json
    5. Restores proven learnings to data/proven_learnings.json
    6. Restores the current plan/goals to data/current_plan.json
    7. Writes journal files if present (Level 3+)
    8. Checks for required dependencies (Ollama, models)
    9. Prints wake-up instructions

After running this, start the entity normally:
    venv/Scripts/python.exe main_gui.py --entity <name>

LanceDB vector memory is NOT restored from the .pid file — it rebuilds automatically
from the journal on first run. This takes a few minutes. Normal.
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_pid(pid_path: Path) -> dict:
    with open(pid_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate(doc: dict) -> None:
    required = ["pid_version", "format", "identity", "personality", "values"]
    missing = [k for k in required if k not in doc]
    if missing:
        raise ValueError(f".pid file is missing required sections: {missing}")
    if doc.get("format") != "portable-identity-document":
        raise ValueError(f"Not a portable-identity-document: {doc.get('format')}")
    print(f"  Format valid. PIS version: {doc['pid_version']}")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _restore_core_memories(doc: dict, data_dir: Path) -> None:
    memory_section = doc.get("memory", {})
    core_mems = memory_section.get("core_memories", [])
    if not core_mems:
        print("  No core memories to restore.")
        return

    # Format matches data/core_memories.json: list of {date, summary, entry_count, compressed_at}
    restored = []
    for i, mem in enumerate(core_mems):
        # core_memories may be plain strings (summary format) or structured dicts
        if isinstance(mem, dict):
            restored.append(mem)
        else:
            restored.append({
                "date": doc.get("created", str(date.today())),
                "summary": str(mem),
                "entry_count": 0,
                "compressed_at": datetime.now().isoformat(),
                "restored_from_pid": True,
            })

    path = data_dir / "core_memories.json"
    # Don't clobber existing memories — prepend restored ones
    existing = []
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    merged = restored + [e for e in existing if e.get("date") not in {r.get("date") for r in restored}]
    _write_json(path, merged)
    print(f"  Core memories: {len(restored)} restored ({len(existing)} already existed)")


def _restore_proven_learnings(doc: dict, data_dir: Path) -> None:
    learnings = doc.get("proven_learnings", [])
    if not learnings:
        print("  No proven learnings in this export (Level 2). Skipping.")
        return

    restored = []
    for insight in learnings:
        restored.append({
            "insight": str(insight),
            "category": "imported",
            "source": "pid_import",
            "added": datetime.now().isoformat(),
            "strength": 0.5,
            "use_count": 0,
            "last_used": None,
            "last_decayed": None,
            "superseded_by": None,
        })

    path = data_dir / "proven_learnings.json"
    existing = {"learnings": []}
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    merged_learnings = restored + existing.get("learnings", [])
    _write_json(path, {"learnings": merged_learnings})
    print(f"  Proven learnings: {len(restored)} restored")


def _restore_goals(doc: dict, data_dir: Path) -> None:
    goals_section = doc.get("goals", {})
    main_goal = goals_section.get("main_goal", "") or goals_section.get("main", "")
    subgoals = goals_section.get("current", [])

    if not main_goal and not subgoals:
        print("  No goals to restore.")
        return

    plan = {
        "goal": main_goal or "Continue growing and being useful to Bill.",
        "subgoals": [
            {"description": g, "action_hint": "research", "status": "active"}
            for g in (subgoals if isinstance(subgoals, list) else [str(subgoals)])
        ],
        "current_subgoal_index": 0,
        "planned_at_cycle": 0,
        "planned_at": datetime.now().isoformat(),
        "status": "active",
        "cost": 0.0,
        "restored_from_pid": True,
    }

    path = data_dir / "current_plan.json"
    if not path.exists():
        _write_json(path, plan)
        print(f"  Goals: restored {len(plan['subgoals'])} subgoals")
    else:
        print(f"  Goals: plan already exists — not overwriting (delete current_plan.json to reset)")


def _restore_journal(doc: dict, journal_dir: Path) -> None:
    entries = doc.get("journal_archive", [])
    if not entries:
        print("  No journal archive in this export (Level 2). Skipping.")
        return

    journal_dir.mkdir(parents=True, exist_ok=True)
    written, skipped = 0, 0
    for entry in entries:
        entry_date = entry.get("date", "unknown")
        content = entry.get("content", "")
        path = journal_dir / f"{entry_date}.md"
        if path.exists():
            skipped += 1
            continue
        path.write_text(content, encoding="utf-8")
        written += 1

    print(f"  Journal: {written} entries restored, {skipped} already existed")


def _check_dependencies() -> None:
    """Check that Ollama and required models are available."""
    import subprocess
    print("\n  Checking dependencies...")

    # Ollama
    result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if result.returncode != 0:
        print("  WARNING: Ollama not found or not running. Install from https://ollama.com")
        print("           Then run: ollama pull qwen3.5:9b && ollama pull nomic-embed-text")
        return

    models = result.stdout.lower()
    needed = {"qwen3.5:9b": "qwen3.5", "nomic-embed-text": "nomic-embed-text"}
    for model_name, search_str in needed.items():
        if search_str in models:
            print(f"  OK: {model_name}")
        else:
            print(f"  MISSING: {model_name} — run: ollama pull {model_name}")


def _print_wakeup(doc: dict, entity_name: str, data_dir: Path) -> None:
    identity = doc.get("identity", {})
    name = identity.get("name", entity_name.capitalize())
    born = identity.get("born", "unknown")
    created = doc.get("created", "unknown")
    level = doc.get("export_level_name", f"Level {doc.get('export_level', '?')}")
    journal_count = len(doc.get("journal_archive", []))

    print(f"""
+------------------------------------------------------------------+
|  {name} is ready to wake up.
|
|  Born:          {born}
|  Export date:   {created}
|  Export level:  {level}
|  Journal days:  {journal_count if journal_count else "(Level 2 - no journal archive)"}
|  Data dir:      {data_dir}
|
|  Start her with:
|    venv\\Scripts\\python.exe main_gui.py --entity {entity_name}
|
|  On first run, LanceDB vector memory rebuilds from journal.
|  This takes a few minutes. Normal.
|
|  She will acknowledge the transition:
|  "I've just been restored from a backup. My last memory is from
|   {created}. I'm reading my journal now to catch up."
+------------------------------------------------------------------+
""")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap an Offspring entity from a .pid Portable Identity Document."
    )
    parser.add_argument(
        "--file", required=True,
        help="Path to the .pid file to import"
    )
    parser.add_argument(
        "--entity", default="",
        help="Entity name to import as (default: read from .pid identity.name)"
    )
    parser.add_argument(
        "--data-dir", default="",
        help="Override data directory (default: data/ for chloe, data_faith/ for faith)"
    )
    args = parser.parse_args()

    pid_path = Path(args.file)
    if not pid_path.exists():
        print(f"ERROR: File not found: {pid_path}")
        sys.exit(1)

    print(f"\nLoading {pid_path.name}...")
    doc = _load_pid(pid_path)

    print("Validating...")
    _validate(doc)

    # Determine entity name and data dir
    identity = doc.get("identity", {})
    pid_name = identity.get("name", "chloe").lower()
    entity_name = args.entity.lower() if args.entity else pid_name

    if args.data_dir:
        data_dir = Path(args.data_dir)
    elif entity_name == "faith":
        data_dir = PROJECT_ROOT / "data_faith"
    else:
        data_dir = PROJECT_ROOT / "data"

    data_dir.mkdir(parents=True, exist_ok=True)
    journal_dir = data_dir / "journal"

    print(f"\nImporting {identity.get('name', entity_name)} -> {data_dir}")
    print(f"Export level: {doc.get('export_level_name', 'unknown')}")
    print()

    # Step 1: Core memories
    print("Step 1/5: Restoring core memories...")
    _restore_core_memories(doc, data_dir)

    # Step 2: Proven learnings (Level 4+)
    print("Step 2/5: Restoring proven learnings...")
    _restore_proven_learnings(doc, data_dir)

    # Step 3: Goals
    print("Step 3/5: Restoring goals...")
    _restore_goals(doc, data_dir)

    # Step 4: Journal (Level 3+)
    print("Step 4/5: Restoring journal...")
    _restore_journal(doc, journal_dir)

    # Step 5: Dependencies
    print("Step 5/5: Checking dependencies...")
    _check_dependencies()

    # Wake-up message
    _print_wakeup(doc, entity_name, data_dir)


if __name__ == "__main__":
    main()
