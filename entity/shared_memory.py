"""
Shared Memory Commons -- Bicameral Cognition Phase 1.

A third memory that contains the sum of both entities' significant
experiences. Every memory that crosses the importance threshold for
individual LTM storage is simultaneously mirrored here.

Both entities recall from the commons during OBSERVE, alongside their
own individual LTM. Shared memories carry a slight identity discount
(0.85x) to preserve individual identity while granting full access.

See Bicameral.txt for the theoretical foundation.
"""

import os
import time
import lancedb
import pyarrow as pa
import numpy as np
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

from entity.long_term_memory import (
    embed_text, _cosine_similarity,
    EMBEDDING_DIM, RELEVANCE_WEIGHT, RECENCY_WEIGHT,
    IMPORTANCE_WEIGHT, ACCESS_BONUS_WEIGHT, RECENCY_DECAY,
)

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_SHARED_DIR = str(PROJECT_ROOT / "data" / "shared_memory")

# Schema: same as LTM + origin tracking
SHARED_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("content", pa.string()),
    pa.field("memory_type", pa.string()),
    pa.field("source", pa.string()),
    pa.field("created_at", pa.float64()),
    pa.field("last_accessed", pa.float64()),
    pa.field("access_count", pa.int64()),
    pa.field("importance", pa.float64()),
    pa.field("tags", pa.string()),
    pa.field("linked_ids", pa.string()),
    pa.field("cycle_id", pa.string()),
    pa.field("origin_entity", pa.string()),
    pa.field("origin_ltm_id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
])

# Default identity discount: shared memories score 0.85x vs individual
IDENTITY_DISCOUNT = 0.85


class SharedMemoryCommons:
    """The third memory -- shared cognitive workspace for both entities."""

    def __init__(self, shared_dir: str = None):
        self._shared_dir = shared_dir or DEFAULT_SHARED_DIR
        os.makedirs(self._shared_dir, exist_ok=True)
        self.db = lancedb.connect(self._shared_dir)
        self._ensure_table()

    def _ensure_table(self):
        if "shared_memories" not in self.db.table_names():
            self.db.create_table("shared_memories", schema=SHARED_SCHEMA)
        self.table = self.db.open_table("shared_memories")

    def store(
        self,
        content: str,
        memory_type: str,
        source: str,
        importance: float,
        tags: str,
        cycle_id: str,
        origin_entity: str,
        origin_ltm_id: str,
        vector: List[float],
    ) -> Optional[str]:
        """Store a memory in the commons using a pre-computed vector.

        The vector is reused from the individual LTM store call --
        zero extra Ollama embedding calls.
        """
        now = time.time()
        mem_id = (
            f"shared_{origin_entity[:1]}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
            f"{memory_type[:3]}"
        )

        try:
            self.table.add([{
                "id": mem_id,
                "content": content[:2000],
                "memory_type": memory_type,
                "source": source,
                "created_at": now,
                "last_accessed": now,
                "access_count": 0,
                "importance": min(10.0, max(1.0, importance)),
                "tags": tags,
                "linked_ids": "",
                "cycle_id": cycle_id,
                "origin_entity": origin_entity,
                "origin_ltm_id": origin_ltm_id,
                "vector": vector,
            }])
            print(f"  [shared] Mirrored to commons: {content[:50]}... "
                  f"(from {origin_entity})")
            return mem_id
        except Exception as e:
            print(f"  [shared] Store failed: {e}")
            return None

    def recall(
        self,
        query: str,
        top_k: int = 5,
        identity_discount: float = IDENTITY_DISCOUNT,
    ) -> List[Dict]:
        """Recall from the shared commons.

        Same three-factor scoring as individual LTM, but all scores
        multiplied by identity_discount (0.85) so individual memories
        naturally rank higher when relevance is equal.
        """
        query_vector = embed_text(query)
        if query_vector is None:
            return []

        try:
            candidates = (
                self.table.search(query_vector)
                .limit(top_k * 3)
                .to_pandas()
            )
            if candidates.empty:
                return []
        except Exception as e:
            print(f"  [shared] Recall search failed: {e}")
            return []

        now = time.time()
        scored = []

        for _, row in candidates.iterrows():
            relevance = _cosine_similarity(query_vector, row["vector"].tolist())
            hours_since = (now - row["last_accessed"]) / 3600
            recency = RECENCY_DECAY ** hours_since
            importance = row["importance"] / 10.0
            access_bonus = min(row["access_count"] / 10.0, 1.0)

            score = (
                RELEVANCE_WEIGHT * relevance +
                RECENCY_WEIGHT * recency +
                IMPORTANCE_WEIGHT * importance +
                ACCESS_BONUS_WEIGHT * access_bonus
            ) * identity_discount

            scored.append({
                "id": row["id"],
                "content": row["content"],
                "memory_type": row["memory_type"],
                "importance": row["importance"],
                "origin_entity": row["origin_entity"],
                "origin_ltm_id": row.get("origin_ltm_id", ""),
                "created_at": row["created_at"],
                "access_count": row["access_count"],
                "score": round(score, 4),
                "relevance": round(relevance, 3),
                "recency": round(recency, 3),
                "tags": row["tags"],
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def count(self) -> int:
        try:
            return len(self.table.to_pandas())
        except Exception:
            return 0

    def get_stats(self) -> Dict:
        """Get contribution stats by entity."""
        try:
            df = self.table.to_pandas()
            if df.empty:
                return {"total": 0, "by_entity": {}}
            by_entity = df["origin_entity"].value_counts().to_dict()
            return {"total": len(df), "by_entity": by_entity}
        except Exception:
            return {"total": 0, "by_entity": {}}
