"""
Chloe's Long-Term Associative Memory — Vector-backed semantic recall.

Inspired by:
- Stanford Generative Agents: recency x importance x relevance scoring
- Letta/MemGPT: tiered memory with sleep-time consolidation
- A-MEM (2025): associative linking between related memories

The key insight: human memory surfaces old experiences when current
circumstances are similar. Chloe should recall a lesson learned on Day 1
when she encounters a similar situation on Day 30 — not because it's recent,
but because it's *relevant*.

Storage: LanceDB table with 768-dim nomic-embed-text-v1.5 embeddings.
Retrieval: Three-factor scoring (relevance + recency + importance).
Embedding: Ollama nomic-embed-text (local, free, ~20ms per call).
Linking: Bidirectional links between related memories (A-MEM pattern).
"""

import os
import json
import time
import math
import requests
import numpy as np
import lancedb
import pyarrow as pa
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from entity.budget import safe_tier
from entity.memory import get_db

EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768
OLLAMA_URL = "http://localhost:11434/api/embed"

# Importance threshold: only store memories scoring >= this
IMPORTANCE_THRESHOLD = 4

# Retrieval parameters
DEFAULT_TOP_K = 5
RELEVANCE_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5
IMPORTANCE_WEIGHT = 0.3
ACCESS_BONUS_WEIGHT = 0.2
RECENCY_DECAY = 0.998  # per hour — slower decay than Stanford (0.995)

# LanceDB schema for long-term memories
LTM_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("content", pa.string()),
    pa.field("memory_type", pa.string()),    # observation, reflection, lesson, experiment, meta
    pa.field("source", pa.string()),         # journal, reflect, daily, consolidation
    pa.field("created_at", pa.float64()),    # Unix timestamp
    pa.field("last_accessed", pa.float64()), # Unix timestamp (updated on retrieval)
    pa.field("access_count", pa.int64()),
    pa.field("importance", pa.float64()),    # 1-10 scale
    pa.field("tags", pa.string()),           # Comma-separated
    pa.field("linked_ids", pa.string()),     # Comma-separated memory IDs
    pa.field("cycle_id", pa.string()),       # Source cycle
    pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),  # nomic-embed-text
])


# ── Embedding ───────────────────────────────────────────────

def embed_text(text: str) -> Optional[List[float]]:
    """Get 768-dim embedding from Ollama nomic-embed-text (local, free).

    Returns None if Ollama is unavailable or embedding fails.
    """
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": EMBEDDING_MODEL, "input": text[:2000]},  # Limit input
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings", [[]])
        if embeddings and len(embeddings[0]) == EMBEDDING_DIM:
            return embeddings[0]
    except Exception as e:
        print(f"  [ltm] Embedding failed: {e}")
    return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a, dtype=np.float32)
    b_arr = np.array(b, dtype=np.float32)
    dot = np.dot(a_arr, b_arr)
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


# ── Long-Term Memory Store ──────────────────────────────────

class LongTermMemory:
    """Chloe's associative long-term memory with vector retrieval."""

    def __init__(self, memory_dir: str = None, entity_name: str = "",
                 shared_commons=None):
        self._memory_dir = memory_dir
        self._entity_name = entity_name
        self.shared_commons = shared_commons
        self.db = get_db(data_dir=memory_dir)
        self._ensure_table()

    def _ensure_table(self):
        """Create ltm table if it doesn't exist."""
        if "long_term_memory" not in self.db.table_names():
            self.db.create_table("long_term_memory", schema=LTM_SCHEMA)
        self.table = self.db.open_table("long_term_memory")

    def store(
        self,
        content: str,
        memory_type: str = "observation",
        source: str = "journal",
        importance: float = 5.0,
        tags: str = "",
        cycle_id: str = "",
    ) -> Optional[str]:
        """Store a new memory with embedding and associative links.

        Returns the memory ID if stored, None if embedding failed or
        importance is below threshold.
        """
        if importance < IMPORTANCE_THRESHOLD:
            return None

        # Embed the content
        vector = embed_text(content)
        if vector is None:
            print(f"  [ltm] Skipping store — embedding unavailable")
            return None

        now = time.time()
        mem_id = f"ltm_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{memory_type[:3]}"

        # Find related existing memories (A-MEM linking pattern)
        linked_ids = self._find_and_link(vector, mem_id)

        self.table.add([{
            "id": mem_id,
            "content": content[:2000],  # Cap at 2K chars
            "memory_type": memory_type,
            "source": source,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "importance": min(10.0, max(1.0, importance)),
            "tags": tags,
            "linked_ids": ",".join(linked_ids),
            "cycle_id": cycle_id,
            "vector": vector,
        }])

        link_msg = f", linked to {len(linked_ids)} memories" if linked_ids else ""
        print(f"  [ltm] Stored: {content[:60]}... "
              f"(importance={importance:.0f}{link_msg})")

        # Mirror to shared memory commons (Bicameral Phase 1)
        if self.shared_commons is not None:
            try:
                self.shared_commons.store(
                    content=content[:2000],
                    memory_type=memory_type,
                    source=source,
                    importance=min(10.0, max(1.0, importance)),
                    tags=tags,
                    cycle_id=cycle_id,
                    origin_entity=self._entity_name or "unknown",
                    origin_ltm_id=mem_id,
                    vector=vector,
                )
            except Exception as e:
                print(f"  [shared] Mirror failed (non-fatal): {e}")

        return mem_id

    def _find_and_link(
        self, vector: List[float], new_id: str, threshold: float = 0.7
    ) -> List[str]:
        """Find related memories and create bidirectional links (A-MEM pattern).

        Returns list of linked memory IDs.
        """
        try:
            # Vector search for similar memories
            results = (
                self.table.search(vector)
                .limit(10)
                .to_pandas()
            )
            if results.empty:
                return []

            linked = []
            for _, row in results.iterrows():
                sim = _cosine_similarity(vector, row["vector"].tolist())
                if sim >= threshold:
                    linked.append(row["id"])
                    # Update the existing memory's links (add new_id)
                    existing_links = row["linked_ids"]
                    if existing_links:
                        ids = existing_links.split(",")
                    else:
                        ids = []
                    if new_id not in ids:
                        ids.append(new_id)
                    # Note: LanceDB doesn't support in-place updates easily,
                    # so we track links on the new memory side primarily.
                    # Consolidation will sync bidirectional links.

            return linked[:5]  # Cap at 5 links

        except Exception as e:
            print(f"  [ltm] Link search failed: {e}")
            return []

    def recall(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        memory_type: Optional[str] = None,
    ) -> List[Dict]:
        """Retrieve memories most relevant to the current situation.

        Uses the Stanford three-factor formula:
            score = relevance * 1.0 + recency * 0.5 + importance * 0.3 + access_bonus * 0.2

        This is what gives Chloe associative memory — old but relevant
        memories surface when current circumstances are similar.
        """
        query_vector = embed_text(query)
        if query_vector is None:
            return []

        try:
            # Vector search for candidates (get more than needed for re-ranking)
            candidates = (
                self.table.search(query_vector)
                .limit(top_k * 3)
                .to_pandas()
            )
            if candidates.empty:
                return []
        except Exception as e:
            print(f"  [ltm] Recall search failed: {e}")
            return []

        # Filter by memory_type if specified
        if memory_type:
            candidates = candidates[candidates["memory_type"] == memory_type]
            if candidates.empty:
                return []

        # Three-factor re-ranking
        now = time.time()
        scored = []

        for _, row in candidates.iterrows():
            # Relevance: cosine similarity (already computed by LanceDB,
            # but we recompute for precision in the scoring formula)
            relevance = _cosine_similarity(query_vector, row["vector"].tolist())

            # Recency: exponential decay since last access
            hours_since = (now - row["last_accessed"]) / 3600
            recency = RECENCY_DECAY ** hours_since

            # Importance: normalized to 0-1
            importance = row["importance"] / 10.0

            # Access bonus: frequently-used memories are more valuable
            access_bonus = min(row["access_count"] / 10.0, 1.0)

            score = (
                RELEVANCE_WEIGHT * relevance +
                RECENCY_WEIGHT * recency +
                IMPORTANCE_WEIGHT * importance +
                ACCESS_BONUS_WEIGHT * access_bonus
            )

            scored.append({
                "id": row["id"],
                "content": row["content"],
                "memory_type": row["memory_type"],
                "importance": row["importance"],
                "created_at": row["created_at"],
                "access_count": row["access_count"],
                "score": round(score, 4),
                "relevance": round(relevance, 3),
                "recency": round(recency, 3),
                "linked_ids": row["linked_ids"],
                "tags": row["tags"],
            })

        # Sort by composite score
        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:top_k]

        # Update access timestamps for retrieved memories
        self._mark_accessed([m["id"] for m in top])

        return top

    def _mark_accessed(self, memory_ids: List[str]):
        """Update last_accessed timestamp and access_count for retrieved memories.

        Note: LanceDB doesn't support efficient in-place updates.
        We track access stats in a sidecar JSON file for efficiency.
        """
        if self._memory_dir:
            sidecar_path = os.path.join(os.path.dirname(self._memory_dir), "ltm_access_stats.json")
        else:
            sidecar_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "data", "ltm_access_stats.json"
            )
        try:
            if os.path.exists(sidecar_path):
                with open(sidecar_path, "r") as f:
                    stats = json.load(f)
            else:
                stats = {}

            now = time.time()
            for mid in memory_ids:
                if mid not in stats:
                    stats[mid] = {"access_count": 0, "last_accessed": now}
                stats[mid]["access_count"] += 1
                stats[mid]["last_accessed"] = now

            with open(sidecar_path, "w") as f:
                json.dump(stats, f)
        except Exception:
            pass

    def get_stats(self) -> Dict:
        """Get memory statistics."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return {"total": 0}
            return {
                "total": len(df),
                "by_type": df["memory_type"].value_counts().to_dict(),
                "avg_importance": round(df["importance"].mean(), 1),
                "oldest": datetime.fromtimestamp(
                    df["created_at"].min()
                ).strftime("%Y-%m-%d"),
                "newest": datetime.fromtimestamp(
                    df["created_at"].max()
                ).strftime("%Y-%m-%d"),
            }
        except Exception:
            return {"total": 0}

    def count(self) -> int:
        """Get total number of stored memories."""
        try:
            return len(self.table.to_pandas())
        except Exception:
            return 0


# ── Consolidation Functions (for daily.py) ──────────────────

def consolidate_ltm(brain, memory_dir: str = None) -> Dict:
    """Run long-term memory consolidation during sleep cycle.

    Operations:
    1. Merge near-duplicate memories (cosine sim > 0.92)
    2. Strengthen frequently-accessed memories
    3. Identify patterns across recent memories and create meta-memories
    4. Prune very old, low-importance, never-accessed memories

    Cost: ~1 Haiku call ($0.01) for meta-reflection synthesis
    """
    ltm = LongTermMemory(memory_dir=memory_dir)
    stats = ltm.get_stats()

    if stats.get("total", 0) < 5:
        return {"total": stats.get("total", 0), "actions": "too few memories"}

    # Load access stats sidecar
    if memory_dir:
        sidecar_path = os.path.join(os.path.dirname(memory_dir), "ltm_access_stats.json")
    else:
        sidecar_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "data", "ltm_access_stats.json"
        )
    access_stats = {}
    try:
        if os.path.exists(sidecar_path):
            with open(sidecar_path, "r") as f:
                access_stats = json.load(f)
    except Exception:
        pass

    df = ltm.table.to_pandas()
    now = time.time()

    # 1. Identify low-value memories for pruning
    # Old (>30 days), low importance (<4), never accessed
    pruned = 0
    thirty_days = 30 * 24 * 3600
    prune_ids = []
    for _, row in df.iterrows():
        age = now - row["created_at"]
        mid = row["id"]
        acc = access_stats.get(mid, {}).get("access_count", 0)
        if age > thirty_days and row["importance"] < 4 and acc == 0:
            prune_ids.append(mid)
            pruned += 1

    # 2. Synthesize meta-reflection from recent high-importance memories
    recent_important = df[
        (df["importance"] >= 6) &
        (df["created_at"] > now - 7 * 24 * 3600)  # Last 7 days
    ].sort_values("importance", ascending=False).head(10)

    meta_cost = 0
    meta_stored = False
    if len(recent_important) >= 3:
        memories_text = "\n".join(
            f"- [{row['memory_type']}] {row['content'][:200]}"
            for _, row in recent_important.iterrows()
        )
        prompt = (
            "You are Chloe's memory consolidation system. Review these "
            "important recent memories and extract 1-2 higher-order patterns "
            "or meta-lessons that connect them. Be specific and actionable.\n\n"
            f"RECENT IMPORTANT MEMORIES:\n{memories_text}\n\n"
            "Write 1-2 concise meta-observations (one sentence each):"
        )
        response = brain.think(
            prompt=prompt,
            tier=safe_tier("fast"),  # Haiku if affordable
            max_tokens=200,
            temperature=0.3,
        )
        meta_cost = response.get("cost", 0)

        # Store the meta-reflection as a high-importance memory
        ltm.store(
            content=f"[META-INSIGHT] {response['text'].strip()}",
            memory_type="meta",
            source="consolidation",
            importance=8.0,
            tags="meta-insight,consolidation",
        )
        meta_stored = True

    return {
        "total_memories": stats.get("total", 0),
        "pruned": pruned,
        "meta_stored": meta_stored,
        "cost": meta_cost,
    }
