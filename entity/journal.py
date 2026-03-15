"""
Chloe's Journal — Persistent Inner Life.

The journal is Chloe's consciousness log. Every thought, observation,
experiment result, goal, and reflection gets recorded here in two formats:

1. Human-readable Markdown files (data/journal/YYYY-MM-DD.md)
   Bill can browse these anytime — full transparency.

2. LanceDB structured entries (searchable, persistent)
   Chloe can query her own past: "What did I learn about word counting?"

Entry types:
  observation — What Chloe notices about herself, her code, her environment
  thought     — Ideas, hypotheses, wonderings
  experiment  — Results of self-improvement experiments
  reflection  — Deeper analysis of what happened and why
  goal        — Something Chloe wants to achieve (with status tracking)
  question    — Something Chloe wants to explore or understand

This is Phase 1 of Chloe v2.0. Without persistent reflection, she can't
set goals, track progress, or develop anything resembling self-awareness.
The journal IS her consciousness log.
"""

import os
import json
import pyarrow as pa
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional

from entity.memory import get_db

JOURNAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "journal"
)

# Entry types that Chloe can write
ENTRY_TYPES = {
    "observation",  # What she notices
    "thought",      # Ideas and hypotheses
    "experiment",   # Experiment results
    "reflection",   # Deeper analysis
    "goal",         # Something to achieve
    "question",     # Something to explore
}

# LanceDB schema for journal entries
JOURNAL_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("timestamp", pa.string()),
    pa.field("date", pa.string()),           # YYYY-MM-DD for daily grouping
    pa.field("entry_type", pa.string()),     # observation/thought/experiment/etc.
    pa.field("content", pa.string()),        # The actual journal text
    pa.field("tags", pa.string()),           # JSON list of topic tags
    pa.field("related_entries", pa.string()),# JSON list of related entry IDs
    pa.field("cycle_id", pa.string()),       # Agent cycle this came from (Phase 2)
    pa.field("goal_status", pa.string()),    # For goals: active/completed/abandoned
])

# Type labels for Markdown rendering
TYPE_LABELS = {
    "observation": "Observation",
    "thought": "Thought",
    "experiment": "Experiment",
    "reflection": "Reflection",
    "goal": "Goal",
    "question": "Question",
}


class Journal:
    """Chloe's persistent journal — dual Markdown + LanceDB storage."""

    def __init__(self, journal_dir: str = None, memory_dir: str = None,
                 entity_name: str = "Chloe"):
        self.journal_dir = journal_dir or JOURNAL_DIR
        self.entity_name = entity_name
        self.db = get_db(data_dir=memory_dir)
        self._ensure_table()
        os.makedirs(self.journal_dir, exist_ok=True)

    def _ensure_table(self):
        """Create journal table in LanceDB if it doesn't exist."""
        if "journal" not in self.db.table_names():
            self.db.create_table("journal", schema=JOURNAL_SCHEMA)
        self.table = self.db.open_table("journal")

    def write(self, entry_type: str, content: str,
              tags: Optional[List[str]] = None,
              related: Optional[List[str]] = None,
              cycle_id: str = "",
              goal_status: str = "") -> str:
        """
        Write a journal entry. Stores in both LanceDB and daily Markdown file.

        Args:
            entry_type: One of ENTRY_TYPES
            content: The journal text
            tags: Topic tags for filtering
            related: IDs of related journal entries
            cycle_id: Agent cycle ID (for Phase 2)
            goal_status: For goals: 'active', 'completed', 'abandoned'

        Returns:
            The entry ID
        """
        if entry_type not in ENTRY_TYPES:
            raise ValueError(
                f"Unknown entry type '{entry_type}'. "
                f"Must be one of: {', '.join(sorted(ENTRY_TYPES))}"
            )

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        entry_id = f"j_{now.strftime('%Y%m%d_%H%M%S')}_{entry_type[:3]}"

        # Store in LanceDB
        self.table.add([{
            "id": entry_id,
            "timestamp": now.isoformat(),
            "date": today,
            "entry_type": entry_type,
            "content": content,
            "tags": json.dumps(tags or []),
            "related_entries": json.dumps(related or []),
            "cycle_id": cycle_id,
            "goal_status": goal_status if entry_type == "goal" else "",
        }])

        # Append to daily Markdown file
        self._append_markdown(now, entry_type, content, tags)

        return entry_id

    def _append_markdown(self, timestamp: datetime, entry_type: str,
                         content: str, tags: Optional[List[str]] = None):
        """Append entry to the daily Markdown journal file."""
        today = timestamp.strftime("%Y-%m-%d")
        filepath = os.path.join(self.journal_dir, f"{today}.md")

        # Create file with header if it doesn't exist
        if not os.path.exists(filepath):
            header = f"# {self.entity_name}'s Journal — {today}\n\n"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header)

        # Format the entry
        time_str = timestamp.strftime("%H:%M")
        label = TYPE_LABELS.get(entry_type, entry_type.title())
        tag_str = f"  *Tags: {', '.join(tags)}*\n" if tags else ""

        entry_md = f"## {time_str} — {label}\n\n{content}\n\n{tag_str}---\n\n"

        with open(filepath, "a", encoding="utf-8") as f:
            f.write(entry_md)

    def get_recent(self, limit: int = 10,
                   entry_type: Optional[str] = None) -> List[Dict]:
        """Get most recent journal entries, optionally filtered by type."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []
            if entry_type:
                df = df[df["entry_type"] == entry_type]
            df = df.sort_values("timestamp", ascending=False).head(limit)
            records = df.to_dict("records")
            # Parse JSON fields
            for r in records:
                r["tags"] = json.loads(r.get("tags", "[]"))
                r["related_entries"] = json.loads(r.get("related_entries", "[]"))
            return records
        except Exception:
            return []

    def get_today(self) -> List[Dict]:
        """Get all entries from today."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []
            today = date.today().isoformat()
            df = df[df["date"] == today]
            df = df.sort_values("timestamp", ascending=True)
            records = df.to_dict("records")
            for r in records:
                r["tags"] = json.loads(r.get("tags", "[]"))
                r["related_entries"] = json.loads(r.get("related_entries", "[]"))
            return records
        except Exception:
            return []

    def get_active_goals(self) -> List[Dict]:
        """Get all active goals (not completed or abandoned)."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []
            goals = df[(df["entry_type"] == "goal") & (df["goal_status"] == "active")]
            goals = goals.sort_values("timestamp", ascending=False)
            records = goals.to_dict("records")
            for r in records:
                r["tags"] = json.loads(r.get("tags", "[]"))
                r["related_entries"] = json.loads(r.get("related_entries", "[]"))
            return records
        except Exception:
            return []

    def complete_goal(self, goal_id: str, reflection: str = "") -> bool:
        """
        Mark a goal as completed. Optionally write a reflection about it.

        Note: LanceDB doesn't support in-place updates easily, so we write
        a new entry marking the goal as completed and link it.
        """
        # Write a completion entry
        self.write(
            entry_type="reflection",
            content=f"Completed goal {goal_id}. {reflection}".strip(),
            related=[goal_id],
        )

        # For tracking, write a new goal entry with 'completed' status
        try:
            df = self.table.to_pandas()
            original = df[df["id"] == goal_id]
            if not original.empty:
                orig = original.iloc[0]
                self.write(
                    entry_type="goal",
                    content=f"[COMPLETED] {orig['content']}",
                    tags=json.loads(orig.get("tags", "[]")),
                    related=[goal_id],
                    goal_status="completed",
                )
                return True
        except Exception:
            pass
        return False

    def get_stale_goals(self, staleness_days: int = 7) -> List[Dict]:
        """Find active tactical goals not referenced in recent journal entries.

        A goal is "stale" if no non-goal journal entry in the last
        staleness_days contains 3+ matching words from the goal content.
        Master-tier goals are exempt from staleness detection.

        Rule-based (no LLM call). Used by entity/consolidation.py.
        """
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []

            # Active goals only
            goals = df[
                (df["entry_type"] == "goal")
                & (df["goal_status"] == "active")
            ]

            cutoff = (
                datetime.now() - timedelta(days=staleness_days)
            ).isoformat()

            # Recent non-goal entries for reference checking
            recent = df[
                (df["timestamp"] > cutoff)
                & (df["entry_type"] != "goal")
            ]

            stale = []
            for _, goal in goals.iterrows():
                tags = json.loads(goal.get("tags", "[]"))
                # Skip master goals — they're never stale
                if "tier:master" in tags:
                    continue

                # Extract significant words from goal (skip short words)
                goal_words = {
                    w.lower()
                    for w in goal["content"].split()
                    if len(w) > 3
                }
                if not goal_words:
                    continue

                # Check if goal was referenced in recent entries
                referenced = False
                for _, entry in recent.iterrows():
                    entry_words = {
                        w.lower()
                        for w in entry["content"].split()
                        if len(w) > 3
                    }
                    if len(goal_words & entry_words) >= 3:
                        referenced = True
                        break

                if not referenced:
                    record = dict(goal)
                    record["tags"] = tags
                    record["related_entries"] = json.loads(
                        record.get("related_entries", "[]")
                    )
                    stale.append(record)

            return stale
        except Exception:
            return []

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Search journal entries by keyword matching.

        Searches content, tags, and entry type. Case-insensitive.
        Future: add vector embeddings for true semantic search.
        """
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []
            query_lower = query.lower()
            mask = (
                df["content"].str.lower().str.contains(query_lower, na=False) |
                df["tags"].str.lower().str.contains(query_lower, na=False) |
                df["entry_type"].str.lower().str.contains(query_lower, na=False)
            )
            results = df[mask].sort_values("timestamp", ascending=False).head(limit)
            records = results.to_dict("records")
            for r in records:
                r["tags"] = json.loads(r.get("tags", "[]"))
                r["related_entries"] = json.loads(r.get("related_entries", "[]"))
            return records
        except Exception:
            return []

    def get_entries_for_date(self, target_date: str) -> List[Dict]:
        """Get all entries for a specific date (YYYY-MM-DD format)."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return []
            df = df[df["date"] == target_date]
            df = df.sort_values("timestamp", ascending=True)
            records = df.to_dict("records")
            for r in records:
                r["tags"] = json.loads(r.get("tags", "[]"))
                r["related_entries"] = json.loads(r.get("related_entries", "[]"))
            return records
        except Exception:
            return []

    def daily_summary(self) -> Dict:
        """
        Get a summary of today's journal activity.

        Returns counts by type and the most recent entry of each type.
        """
        entries = self.get_today()
        summary = {
            "date": date.today().isoformat(),
            "total_entries": len(entries),
            "by_type": {},
            "active_goals": len(self.get_active_goals()),
        }

        for entry_type in ENTRY_TYPES:
            typed = [e for e in entries if e["entry_type"] == entry_type]
            summary["by_type"][entry_type] = {
                "count": len(typed),
                "latest": typed[-1]["content"][:100] if typed else None,
            }

        return summary

    def get_stats(self) -> Dict:
        """Get overall journal statistics."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return {"total": 0, "days": 0, "by_type": {}}
            return {
                "total": len(df),
                "days": df["date"].nunique(),
                "by_type": df["entry_type"].value_counts().to_dict(),
                "first_entry": df["timestamp"].min(),
                "latest_entry": df["timestamp"].max(),
                "active_goals": len(df[(df["entry_type"] == "goal") &
                                       (df["goal_status"] == "active")]),
            }
        except Exception:
            return {"total": 0, "days": 0, "by_type": {}}

    def read_markdown(self, target_date: Optional[str] = None) -> str:
        """
        Read the Markdown journal file for a given date (or today).
        Returns the raw Markdown text that Bill can read.
        """
        target = target_date or date.today().isoformat()
        filepath = os.path.join(self.journal_dir, f"{target}.md")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        return f"No journal entries for {target}."
