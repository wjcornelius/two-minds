"""
Evolving Curiosity — Experience-driven topic generation.

Each entity starts with seed topics from EntityConfig.curiosity_topics.
As the entity researches, successful research spawns new "experience"
topics via a local LLM question. Over time, experience topics dominate
sampling and seeds fade into the background.

Weight formula:
    weight = base * freshness
    base:       3.0 for experience, 1/(1 + total_research_cycles/20) for seeds
    freshness:  1/(1 + times_chosen)

Seeds lose half their weight after ~20 research cycles (~1 day).
By day 3, experience topics account for ~90% of sampling probability.

JSON file per entity: data/curiosity.json or data_faith/curiosity.json.
"""

import json
import random
from datetime import date
from pathlib import Path
from typing import List, Optional


class CuriosityTracker:
    """Manages an entity's evolving research interests."""

    def __init__(self, path: Path, seed_topics: List[str]):
        self.path = Path(path)
        self.seed_topics = seed_topics
        self.data = self._load()

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> dict:
        """Load from JSON, seeding on first run."""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Merge any new seed topics added to config since last run
                existing_texts = {t["text"] for t in data.get("topics", [])}
                for seed in self.seed_topics:
                    if seed not in existing_texts:
                        data["topics"].append(self._make_entry(seed, "seed"))
                return data
            except Exception:
                pass
        # First run — bootstrap from seed topics
        return {
            "topics": [self._make_entry(t, "seed") for t in self.seed_topics],
            "total_research_cycles": 0,
        }

    def _save(self):
        """Persist to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)

    def _make_entry(self, text: str, source: str,
                    parent: Optional[str] = None) -> dict:
        return {
            "text": text,
            "source": source,
            "times_suggested": 0,
            "times_chosen": 0,
            "added_date": date.today().isoformat(),
            "last_chosen": None,
            "spawned_from": parent,
        }

    # ── Core API ─────────────────────────────────────────────

    def sample(self, n: int = 5) -> List[str]:
        """Weighted random sample of n topics.

        Experience topics get 3x base weight. Seeds fade as total
        research cycles grow. Over-researched topics fade via freshness.
        """
        topics = self.data["topics"]
        if not topics:
            return self.seed_topics[:n]

        cycle_count = self.data["total_research_cycles"]
        seed_fade = 1.0 / (1.0 + cycle_count / 20.0)

        weights = []
        for t in topics:
            base = 3.0 if t["source"] == "experience" else seed_fade
            freshness = 1.0 / (1.0 + t["times_chosen"])
            weights.append(base * freshness)

        # Weighted sample without replacement
        chosen = []
        available = list(range(len(topics)))
        avail_weights = list(weights)

        for _ in range(min(n, len(available))):
            if not available:
                break
            total = sum(avail_weights)
            if total <= 0:
                break
            r = random.uniform(0, total)
            cumulative = 0
            for idx_pos, idx in enumerate(available):
                cumulative += avail_weights[idx_pos]
                if cumulative >= r:
                    chosen.append(idx)
                    available.pop(idx_pos)
                    avail_weights.pop(idx_pos)
                    break

        # Mark as suggested
        for idx in chosen:
            topics[idx]["times_suggested"] += 1
        self._save()

        return [topics[idx]["text"] for idx in chosen]

    def record_chosen(self, topic_text: str):
        """Mark a topic as chosen for research this cycle."""
        self.data["total_research_cycles"] += 1
        for t in self.data["topics"]:
            if t["text"].lower() == topic_text.lower():
                t["times_chosen"] += 1
                t["last_chosen"] = date.today().isoformat()
                self._save()
                return
        # Topic was invented by the LLM (not in our list) — add as experience
        entry = self._make_entry(topic_text, "experience")
        entry["times_chosen"] = 1
        entry["last_chosen"] = date.today().isoformat()
        self.data["topics"].append(entry)
        self._save()

    def spawn_topic(self, new_text: str, parent_topic: str):
        """Add an experience-generated topic spawned from research."""
        # Deduplicate: skip if very similar topic already exists
        new_lower = new_text.lower().strip()
        for t in self.data["topics"]:
            if t["text"].lower() == new_lower:
                return  # Exact duplicate
        entry = self._make_entry(new_text.strip(), "experience", parent=parent_topic)
        self.data["topics"].append(entry)
        self._save()

    # ── Stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return summary stats for logging/debugging."""
        topics = self.data["topics"]
        seeds = [t for t in topics if t["source"] == "seed"]
        experience = [t for t in topics if t["source"] == "experience"]
        return {
            "total": len(topics),
            "seeds": len(seeds),
            "experience": len(experience),
            "total_research_cycles": self.data["total_research_cycles"],
            "experience_ratio": len(experience) / max(1, len(topics)),
        }
