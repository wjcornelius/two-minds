"""
Offspring Memory System - LanceDB-backed persistent memory.

The entity's memory is its identity. Everything it learns, attempts,
reflects on, and improves is stored here. LanceDB gives us both
structured metadata and vector similarity search in one embedded store.
"""

import os
import json
import lancedb
import pyarrow as pa
from datetime import datetime
from typing import List, Dict, Optional

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "memory")


def get_db(data_dir: str = None) -> lancedb.DBConnection:
    """Get LanceDB connection, creating directory if needed.

    Args:
        data_dir: Override memory directory. Defaults to data/memory.
    """
    db_dir = data_dir or DB_DIR
    os.makedirs(db_dir, exist_ok=True)
    return lancedb.connect(db_dir)


# Schema definitions
REFLECTIONS_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("timestamp", pa.string()),
    pa.field("task_id", pa.string()),
    pa.field("content", pa.string()),         # The reflection text
    pa.field("lesson", pa.string()),          # Distilled lesson
    pa.field("improvement_type", pa.string()), # prompt, code, strategy, memory
    pa.field("applied", pa.bool_()),          # Was this reflection acted on?
])

VERSIONS_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("timestamp", pa.string()),
    pa.field("component", pa.string()),       # Which part changed
    pa.field("version", pa.int64()),
    pa.field("description", pa.string()),
    pa.field("content", pa.string()),         # The actual prompt/code
    pa.field("benchmark_score", pa.float64()),
    pa.field("active", pa.bool_()),           # Is this the current version?
    pa.field("rolled_back", pa.bool_()),
])

TASKS_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("timestamp", pa.string()),
    pa.field("task_type", pa.string()),       # benchmark, user_task, self_improvement
    pa.field("description", pa.string()),
    pa.field("input_data", pa.string()),
    pa.field("output", pa.string()),
    pa.field("score", pa.float64()),
    pa.field("duration_seconds", pa.float64()),
    pa.field("model_used", pa.string()),
    pa.field("tokens_used", pa.int64()),
    pa.field("cost_usd", pa.float64()),
])

IDENTITY_SCHEMA = pa.schema([
    pa.field("key", pa.string()),
    pa.field("value", pa.string()),
    pa.field("updated", pa.string()),
])


def _ensure_table(db, name: str, schema: pa.Schema):
    """Create table if it doesn't exist, validating schema match."""
    if name not in db.table_names():
        db.create_table(name, schema=schema)
    else:
        # Validate existing table schema matches expected
        table = db.open_table(name)
        if table.schema != schema:
            raise ValueError(
                f"Table '{name}' schema mismatch. Expected {schema.names}, "
                f"got {table.schema.names}"
            )
    return db.open_table(name)


class Memory:
    """The entity's persistent memory system."""

    def __init__(self, data_dir: str = None):
        self.db = get_db(data_dir=data_dir)
        # Ensure all tables exist
        self.reflections = _ensure_table(self.db, "reflections", REFLECTIONS_SCHEMA)
        self.versions = _ensure_table(self.db, "versions", VERSIONS_SCHEMA)
        self.tasks = _ensure_table(self.db, "tasks", TASKS_SCHEMA)
        self.identity = _ensure_table(self.db, "identity", IDENTITY_SCHEMA)

    def add_reflection(self, task_id: str, content: str, lesson: str,
                       improvement_type: str = "strategy") -> str:
        """Store a reflection from a task attempt."""
        rid = f"ref_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.reflections.add([{
            "id": rid,
            "timestamp": datetime.now().isoformat(),
            "task_id": task_id,
            "content": content,
            "lesson": lesson,
            "improvement_type": improvement_type,
            "applied": False,
        }])
        return rid

    def get_recent_reflections(self, limit: int = 5) -> List[dict]:
        """Get most recent reflections."""
        try:
            df = self.reflections.to_pandas()
            if df.empty:
                return []
            df = df.sort_values("timestamp", ascending=False).head(limit)
            return df.to_dict("records")
        except Exception:
            return []

    def add_version(self, component: str, content: str,
                    description: str, benchmark_score: float = 0.0) -> int:
        """Store a new version of a component (prompt, code, strategy)."""
        # Get next version number
        try:
            df = self.versions.to_pandas()
            existing = df[df["component"] == component]
            version = int(existing["version"].max()) + 1 if not existing.empty else 1
            # Deactivate previous versions
            if not existing.empty:
                # LanceDB doesn't support in-place updates easily,
                # so we track active status via version number
                pass
        except Exception:
            version = 1

        vid = f"v_{component}_{version}"
        self.versions.add([{
            "id": vid,
            "timestamp": datetime.now().isoformat(),
            "component": component,
            "version": version,
            "description": description,
            "content": content,
            "benchmark_score": benchmark_score,
            "active": True,
            "rolled_back": False,
        }])
        return version

    def get_active_version(self, component: str) -> Optional[dict]:
        """Get the current active version of a component."""
        try:
            df = self.versions.to_pandas()
            comp_versions = df[df["component"] == component]
            if comp_versions.empty:
                return None
            # Latest version is active
            latest = comp_versions.sort_values("version", ascending=False).iloc[0]
            return latest.to_dict()
        except Exception:
            return None

    def log_task(self, task_type: str, description: str, input_data: str,
                 output: str, score: float, duration: float,
                 model: str, tokens: int, cost: float) -> str:
        """Log a completed task."""
        tid = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.tasks.add([{
            "id": tid,
            "timestamp": datetime.now().isoformat(),
            "task_type": task_type,
            "description": description,
            "input_data": input_data,
            "output": output,
            "score": score,
            "duration_seconds": duration,
            "model_used": model,
            "tokens_used": tokens,
            "cost_usd": cost,
        }])
        return tid

    def get_task_history(self, task_type: Optional[str] = None,
                         limit: int = 20) -> List[dict]:
        """Get task history, optionally filtered by type."""
        try:
            df = self.tasks.to_pandas()
            if df.empty:
                return []
            if task_type:
                df = df[df["task_type"] == task_type]
            df = df.sort_values("timestamp", ascending=False).head(limit)
            return df.to_dict("records")
        except Exception:
            return []

    def set_identity(self, key: str, value: str):
        """Set an identity attribute."""
        self.identity.add([{
            "key": key,
            "value": value,
            "updated": datetime.now().isoformat(),
        }])

    def get_identity(self, key: str) -> Optional[str]:
        """Get an identity attribute."""
        try:
            df = self.identity.to_pandas()
            matches = df[df["key"] == key]
            if matches.empty:
                return None
            return matches.sort_values("updated", ascending=False).iloc[0]["value"]
        except Exception:
            return None

    def get_full_identity(self) -> Dict[str, str]:
        """Get all identity attributes."""
        try:
            df = self.identity.to_pandas()
            if df.empty:
                return {}
            # Latest value for each key
            latest = df.sort_values("updated").drop_duplicates("key", keep="last")
            return dict(zip(latest["key"], latest["value"]))
        except Exception:
            return {}

    def get_stats(self) -> dict:
        """Get memory statistics."""
        stats = {}
        try:
            stats["reflections"] = len(self.reflections.to_pandas())
        except Exception:
            stats["reflections"] = 0
        try:
            stats["versions"] = len(self.versions.to_pandas())
        except Exception:
            stats["versions"] = 0
        try:
            stats["tasks"] = len(self.tasks.to_pandas())
        except Exception:
            stats["tasks"] = 0
        try:
            stats["identity_keys"] = len(
                self.identity.to_pandas()["key"].unique()
            )
        except Exception:
            stats["identity_keys"] = 0
        return stats
