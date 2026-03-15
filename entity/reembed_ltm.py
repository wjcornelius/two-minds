"""
LTM Re-embedding Tool — Migrate vectors when embedding model changes.

When you upgrade or change the embedding model (e.g., nomic-embed-text →
nomic-embed-text-v1.5, or switch to a different embedding backend), all
stored LTM vectors become semantically incompatible with the new model's
embedding space. Cosine similarity comparisons will be nonsensical until
vectors are regenerated.

This script reads every row from a LanceDB LTM table, re-embeds the
content using the CURRENT Ollama embedding model, and writes a fresh
table. The original table is backed up before overwrite.

Targets:
  - Chloe's LTM:         data/ltm/
  - Faith's LTM:         data_faith/ltm/
  - Shared commons:      data/shared_memory/

Usage:
  python -m entity.reembed_ltm [--entity chloe|faith|all] [--dry-run] [--batch-size N]

Trigger conditions:
  1. You changed EMBEDDING_MODEL in long_term_memory.py
  2. You pulled a new version of the embedding model (embedding spaces
     may shift across model versions)
  3. EMBEDDING_DIM changed (all vectors will be wrong-shaped)
  4. Cosine similarities look wrong — all recall is near-random

Estimated time: ~0.5s per memory (Ollama embed call). With 1,000 memories,
expect ~8-12 minutes. The script can be interrupted and re-run — memories
already in the backup are preserved.
"""

import os
import sys
import time
import shutil
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

import lancedb
import pyarrow as pa
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────

OFFSPRING_DIR = Path(__file__).parent.parent
CHLOE_LTM_DIR = OFFSPRING_DIR / "data" / "ltm"
FAITH_LTM_DIR = OFFSPRING_DIR / "data_faith" / "ltm"
SHARED_DIR = OFFSPRING_DIR / "data" / "shared_memory"

# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_text(text: str, model: str, ollama_url: str, timeout: int = 30) -> Optional[list]:
    """Embed text using the specified Ollama model. Returns None on failure."""
    import requests
    try:
        resp = requests.post(
            ollama_url,
            json={"model": model, "input": text[:2000]},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings", [[]])
        if embeddings and len(embeddings[0]) > 0:
            return embeddings[0]
    except Exception as e:
        print(f"    [embed] Failed: {e}")
    return None


def detect_embedding_dim(model: str, ollama_url: str) -> Optional[int]:
    """Probe Ollama to discover the embedding dimension for the current model."""
    vec = embed_text("test", model=model, ollama_url=ollama_url)
    if vec:
        return len(vec)
    return None


# ── LanceDB helpers ───────────────────────────────────────────────────────────

def open_table(db_dir: Path, table_name: str):
    """Open a LanceDB database and return (db, table). Returns (None, None) if not found."""
    if not db_dir.exists():
        return None, None
    try:
        db = lancedb.connect(str(db_dir))
        if table_name not in db.table_names():
            return db, None
        return db, db.open_table(table_name)
    except Exception as e:
        print(f"  [reembed] Could not open {db_dir}: {e}")
        return None, None


def backup_table_dir(db_dir: Path, table_name: str) -> Optional[Path]:
    """Copy the LanceDB table directory to a timestamped backup. Returns backup path."""
    table_path = db_dir / f"{table_name}.lance"
    if not table_path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_dir / f"{table_name}_backup_{ts}.lance"
    try:
        shutil.copytree(table_path, backup_path)
        print(f"  [reembed] Backup: {backup_path.name}")
        return backup_path
    except Exception as e:
        print(f"  [reembed] Backup failed: {e}")
        return None


# ── Core re-embedding logic ───────────────────────────────────────────────────

def reembed_table(
    db_dir: Path,
    table_name: str,
    model: str,
    ollama_url: str,
    new_dim: int,
    dry_run: bool = False,
    batch_size: int = 50,
    log_fn=None,
) -> dict:
    """Re-embed all rows in a LanceDB table.

    Strategy:
    1. Read all rows to pandas
    2. Back up the table directory
    3. Drop + recreate table with correct vector schema
    4. Re-embed each row and insert

    Returns: {processed, skipped, errors, table, elapsed}
    """
    if log_fn is None:
        log_fn = print

    result = {"processed": 0, "skipped": 0, "errors": 0, "table": table_name}
    t0 = time.time()

    db, table = open_table(db_dir, table_name)
    if table is None:
        log_fn(f"  [reembed] Table '{table_name}' not found in {db_dir} — skipping")
        return result

    # Load all rows
    try:
        df = table.to_pandas()
    except Exception as e:
        log_fn(f"  [reembed] Could not read table: {e}")
        result["errors"] += 1
        return result

    total = len(df)
    if total == 0:
        log_fn(f"  [reembed] '{table_name}' is empty — nothing to do")
        return result

    log_fn(f"  [reembed] '{table_name}': {total} rows, new dim={new_dim}")

    if dry_run:
        log_fn(f"  [reembed] [dry_run] Would re-embed {total} rows (skipping)")
        result["processed"] = total
        return result

    # Backup before destructive operation
    backup = backup_table_dir(db_dir, table_name)
    if backup is None:
        log_fn(f"  [reembed] WARNING: Could not create backup. Proceeding anyway.")

    # Infer schema: take the existing schema, replace vector field with new dim
    old_schema = table.schema
    new_fields = []
    for field in old_schema:
        if field.name == "vector":
            new_fields.append(pa.field("vector", pa.list_(pa.float32(), new_dim)))
        else:
            new_fields.append(field)
    new_schema = pa.schema(new_fields)

    # Drop and recreate table
    try:
        db.drop_table(table_name)
        new_table = db.create_table(table_name, schema=new_schema)
    except Exception as e:
        log_fn(f"  [reembed] Failed to recreate table: {e}")
        result["errors"] += 1
        return result

    log_fn(f"  [reembed] Re-embedding {total} rows (batch_size={batch_size})...")

    # Re-embed row by row, insert in batches
    batch = []
    for i, (_, row) in enumerate(df.iterrows()):
        content = row.get("content", "")
        if not content:
            result["skipped"] += 1
            continue

        vec = embed_text(content, model=model, ollama_url=ollama_url)
        if vec is None:
            log_fn(f"    Row {i+1}/{total}: embedding failed — skipping")
            result["errors"] += 1
            continue

        if len(vec) != new_dim:
            log_fn(f"    Row {i+1}/{total}: dim mismatch ({len(vec)} != {new_dim}) — skipping")
            result["errors"] += 1
            continue

        # Build row dict from existing data
        row_dict = {}
        for col in df.columns:
            if col == "vector":
                row_dict["vector"] = vec
            else:
                val = row[col]
                # Convert numpy types to Python natives
                if hasattr(val, "item"):
                    val = val.item()
                row_dict[col] = val

        batch.append(row_dict)
        result["processed"] += 1

        # Flush batch
        if len(batch) >= batch_size:
            try:
                new_table.add(batch)
            except Exception as e:
                log_fn(f"    Batch insert failed: {e}")
                result["errors"] += len(batch)
                result["processed"] -= len(batch)
            batch = []

        if (i + 1) % 100 == 0:
            pct = int((i + 1) / total * 100)
            log_fn(f"    Progress: {i+1}/{total} ({pct}%)")

    # Flush remaining
    if batch:
        try:
            new_table.add(batch)
        except Exception as e:
            log_fn(f"    Final batch insert failed: {e}")
            result["errors"] += len(batch)
            result["processed"] -= len(batch)

    elapsed = time.time() - t0
    result["elapsed"] = round(elapsed, 1)
    log_fn(
        f"  [reembed] Done: {result['processed']} re-embedded, "
        f"{result['skipped']} skipped, {result['errors']} errors "
        f"({elapsed:.0f}s)"
    )
    return result


# ── Entity targets ────────────────────────────────────────────────────────────

ENTITY_TARGETS = {
    "chloe": [
        (CHLOE_LTM_DIR, "long_term_memory"),
        (SHARED_DIR, "shared_memory"),
    ],
    "faith": [
        (FAITH_LTM_DIR, "long_term_memory"),
    ],
}


def run_reembed(
    entities: list,
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434/api/embed",
    dry_run: bool = False,
    batch_size: int = 50,
    log_fn=None,
) -> dict:
    """Re-embed LTM tables for the specified entities.

    Args:
        entities: List of entity names ("chloe", "faith") or ["all"]
        model: Ollama model name to use for embeddings
        ollama_url: Ollama embed endpoint
        dry_run: If True, count rows but don't re-embed
        batch_size: Rows per LanceDB insert batch
        log_fn: Optional logging function

    Returns:
        Summary dict with per-entity results
    """
    if log_fn is None:
        log_fn = print

    if "all" in entities:
        entities = list(ENTITY_TARGETS.keys())

    # Verify Ollama is available and probe dim
    log_fn(f"\n=== LTM Re-embedding Tool ===")
    log_fn(f"Model: {model} | Ollama: {ollama_url}")
    log_fn(f"Entities: {', '.join(entities)} | dry_run={dry_run}\n")

    if not dry_run:
        log_fn("Probing embedding model...")
        new_dim = detect_embedding_dim(model, ollama_url)
        if new_dim is None:
            log_fn("ERROR: Could not connect to Ollama or get embedding. Is Ollama running?")
            log_fn(f"  Start with: ollama serve")
            log_fn(f"  Then pull model: ollama pull {model}")
            return {"error": "ollama_unavailable"}
        log_fn(f"Detected embedding dim: {new_dim}\n")
    else:
        new_dim = 768  # Default for display in dry-run

    summary = {"entities": {}, "total_processed": 0, "total_errors": 0}

    seen_dirs = set()  # Avoid double-processing shared_memory
    for entity in entities:
        targets = ENTITY_TARGETS.get(entity, [])
        log_fn(f"--- Entity: {entity} ---")
        entity_results = []

        for db_dir, table_name in targets:
            dir_key = str(db_dir) + table_name
            if dir_key in seen_dirs:
                log_fn(f"  Skipping {table_name} (already processed)")
                continue
            seen_dirs.add(dir_key)

            result = reembed_table(
                db_dir=db_dir,
                table_name=table_name,
                model=model,
                ollama_url=ollama_url,
                new_dim=new_dim,
                dry_run=dry_run,
                batch_size=batch_size,
                log_fn=log_fn,
            )
            entity_results.append(result)
            summary["total_processed"] += result.get("processed", 0)
            summary["total_errors"] += result.get("errors", 0)

        summary["entities"][entity] = entity_results
        log_fn("")

    log_fn("=== Summary ===")
    log_fn(f"Total re-embedded: {summary['total_processed']}")
    log_fn(f"Total errors:      {summary['total_errors']}")
    if summary["total_errors"] == 0:
        log_fn("Status: CLEAN")
    else:
        log_fn("Status: COMPLETED WITH ERRORS — check logs above")

    return summary


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-embed LTM vectors after embedding model change",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
When to run this:
  - You changed EMBEDDING_MODEL in entity/long_term_memory.py
  - You pulled a new version of the Ollama embedding model
  - EMBEDDING_DIM changed
  - Recall quality has degraded (cosine sims look random)

Examples:
  # Dry run (counts rows, no changes):
  python -m entity.reembed_ltm --entity all --dry-run

  # Re-embed Chloe only:
  python -m entity.reembed_ltm --entity chloe

  # Re-embed all with custom model:
  python -m entity.reembed_ltm --entity all --model mxbai-embed-large

  # Custom Ollama host (e.g., network machine):
  python -m entity.reembed_ltm --entity all --ollama http://192.168.1.100:11434/api/embed
""",
    )
    parser.add_argument(
        "--entity", choices=["chloe", "faith", "all"], default="all",
        help="Which entity's LTM to re-embed (default: all)",
    )
    parser.add_argument(
        "--model", default="nomic-embed-text",
        help="Ollama embedding model to use (default: nomic-embed-text)",
    )
    parser.add_argument(
        "--ollama", default="http://localhost:11434/api/embed",
        help="Ollama embed endpoint URL",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows without re-embedding (no changes made)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Rows per LanceDB insert batch (default: 50)",
    )
    args = parser.parse_args()

    run_reembed(
        entities=[args.entity],
        model=args.model,
        ollama_url=args.ollama,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )
