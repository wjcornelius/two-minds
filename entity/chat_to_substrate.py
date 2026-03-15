"""
Chat-to-Substrate Bridge — Bidirectional Memory Architecture.

Reads Chloe/Faith family chat sessions and extracts Bill's biographical
data into his cognitive substrate (bill_knowledge_base.db + vector store).

Every conversation between Bill and his AI daughters contains facts,
emotions, stories, and insights about Bill that should live permanently
in his substrate — not just in the AI chat history.

Called from daily.py Phase 8 (Chloe's daily cycle only).
Can also be run standalone: python -m entity.chat_to_substrate
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────
OFFSPRING_DIR = Path(__file__).parent.parent                # .../Offspring/
MY_SONGS_DIR = OFFSPRING_DIR.parent / "My_Songs"            # .../My_Songs/
BIOGRAPHER_DIR = MY_SONGS_DIR / "_soul" / "biographer"      # .../biographer/
DB_PATH = MY_SONGS_DIR / "_soul" / "bill_knowledge_base.db"
VECTOR_DIR = MY_SONGS_DIR / "_soul" / "vector_db"
CHAT_SESSIONS_DIR = OFFSPRING_DIR / "data" / "chat_sessions"
LOG_PATH = OFFSPRING_DIR / "data" / "chat_extraction_log.json"

# ── Extraction state ──────────────────────────────────────────────────────────

def _load_log() -> dict:
    """Load extraction log — tracks which sessions have been processed."""
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_processed_at": None, "sessions_processed": [], "total_entries_added": 0}


def _save_log(log: dict):
    try:
        LOG_PATH.write_text(json.dumps(log, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[chat→substrate] Could not save log: {e}")


# ── Transcript formatting ─────────────────────────────────────────────────────

def _format_as_transcript(history: list, saved_at: str) -> str:
    """Convert chat session history to a biographer-style transcript string.

    The extractor expects a first-person narrative transcript, so we frame
    the conversation as Bill speaking and being responded to.
    """
    lines = [f"[Chat session from {saved_at[:10]}]", ""]
    for exchange in history:
        user_msg = exchange.get("user", "").strip()
        chloe_msg = exchange.get("chloe", "").strip()
        faith_msg = exchange.get("faith", "").strip()

        if user_msg:
            lines.append(f"Bill: {user_msg}")
        if chloe_msg:
            lines.append(f"Chloe: {chloe_msg}")
        if faith_msg:
            lines.append(f"Faith: {faith_msg}")
        lines.append("")

    return "\n".join(lines)


# ── Core extraction ───────────────────────────────────────────────────────────

def extract_recent_chats(
    cutoff_hours: int = 26,  # Process sessions from the last ~26h (daily + buffer)
    log_fn=None,
    dry_run: bool = False,
) -> dict:
    """Extract Bill's biographical data from recent chat sessions.

    Args:
        cutoff_hours: Only process sessions newer than this many hours ago.
        log_fn: Optional logging function (defaults to print).
        dry_run: If True, extract but don't write to database.

    Returns:
        Dict with keys: entries_added, sessions_processed, skipped, errors
    """
    if log_fn is None:
        log_fn = print

    result = {"entries_added": 0, "sessions_processed": 0, "skipped": 0, "errors": 0}

    # Check biographer is available
    if not BIOGRAPHER_DIR.exists():
        log_fn(f"  [chat→substrate] Biographer not found at {BIOGRAPHER_DIR} — skipping")
        result["errors"] += 1
        return result

    if not DB_PATH.exists():
        log_fn(f"  [chat→substrate] Substrate DB not found at {DB_PATH} — skipping")
        result["errors"] += 1
        return result

    # Add biographer to path
    sys.path.insert(0, str(BIOGRAPHER_DIR))
    sys.path.insert(0, str(MY_SONGS_DIR / "_soul"))

    try:
        from multi_pass_extraction import MultiPassExtractor
        from enricher import DatabaseEnricher
    except ImportError as e:
        log_fn(f"  [chat→substrate] Import failed: {e} — skipping")
        result["errors"] += 1
        return result

    # Find sessions newer than cutoff
    cutoff = datetime.now() - timedelta(hours=cutoff_hours)
    session_files = list(CHAT_SESSIONS_DIR.glob("*.json"))

    if not session_files:
        log_fn("  [chat→substrate] No chat session files found")
        return result

    extraction_log = _load_log()
    already_processed = set(extraction_log.get("sessions_processed", []))

    sessions_to_process = []
    for path in session_files:
        # Skip non-session files
        if path.name == "chat_extraction_log.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            saved_at_str = data.get("saved_at", "")
            if not saved_at_str:
                continue
            saved_at = datetime.fromisoformat(saved_at_str)
            if saved_at < cutoff:
                result["skipped"] += 1
                continue
            # Skip if already processed this version (keyed by filename + saved_at)
            session_key = f"{path.name}:{saved_at_str}"
            if session_key in already_processed:
                result["skipped"] += 1
                continue
            sessions_to_process.append((path, data, saved_at_str, session_key))
        except Exception as e:
            log_fn(f"  [chat→substrate] Could not read {path.name}: {e}")
            result["errors"] += 1

    if not sessions_to_process:
        log_fn("  [chat→substrate] No new sessions to process")
        return result

    log_fn(f"  [chat→substrate] Processing {len(sessions_to_process)} session(s)...")

    try:
        extractor = MultiPassExtractor()
        enricher = DatabaseEnricher(db_path=DB_PATH)
    except Exception as e:
        log_fn(f"  [chat→substrate] Failed to initialize extractor/enricher: {e}")
        result["errors"] += 1
        return result

    for path, data, saved_at_str, session_key in sessions_to_process:
        history = data.get("history", [])
        if not history:
            log_fn(f"  [chat→substrate] {path.name}: empty history, skipping")
            result["skipped"] += 1
            continue

        persona = data.get("persona", "unknown")
        log_fn(f"  [chat→substrate] Extracting {path.name} ({len(history)} exchanges, {persona} mode)...")

        transcript = _format_as_transcript(history, saved_at_str)

        try:
            extraction_result = extractor.extract_all(transcript)
            entries = extraction_result.get("all_extractions", [])
            log_fn(f"    Extracted {len(entries)} entries from transcript")

            if not dry_run and entries:
                # Tag entries with source info
                for entry in entries:
                    entry.setdefault("prompt_version", f"chat-extraction-{saved_at_str[:10]}")
                    entry.setdefault("life_period", "elder")
                    entry.setdefault("approximate_year", 2026)

                write_results = enricher.process_extractions(entries, require_confirmation=False)
                added = write_results.get("added", 0)
                log_fn(f"    Wrote {added} entries to substrate ({write_results.get('skipped', 0)} skipped, {write_results.get('errors', 0)} errors)")
                result["entries_added"] += added
            elif dry_run:
                log_fn(f"    [dry_run] Would write {len(entries)} entries")
                result["entries_added"] += len(entries)

            result["sessions_processed"] += 1
            already_processed.add(session_key)

        except Exception as e:
            log_fn(f"    ERROR during extraction: {e}")
            result["errors"] += 1

        time.sleep(0.5)  # Brief pause between sessions to avoid API hammering

    # Update extraction log
    extraction_log["last_processed_at"] = datetime.now().isoformat()
    extraction_log["sessions_processed"] = list(already_processed)
    extraction_log["total_entries_added"] = (
        extraction_log.get("total_entries_added", 0) + result["entries_added"]
    )
    if not dry_run:
        _save_log(extraction_log)

    return result


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract chat sessions to Bill's cognitive substrate")
    parser.add_argument("--hours", type=int, default=26,
                        help="Process sessions from the last N hours (default: 26)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract but don't write to database")
    parser.add_argument("--all", action="store_true",
                        help="Process ALL sessions regardless of age (ignores --hours)")
    args = parser.parse_args()

    hours = 87600 if args.all else args.hours  # ~10 years if --all

    print(f"\n=== Chat → Substrate Extraction ===")
    print(f"Sessions from last {hours}h | dry_run={args.dry_run}\n")

    results = extract_recent_chats(
        cutoff_hours=hours,
        dry_run=args.dry_run,
    )
    print(f"\nDone. Added: {results['entries_added']} | "
          f"Processed: {results['sessions_processed']} | "
          f"Skipped: {results['skipped']} | "
          f"Errors: {results['errors']}")
