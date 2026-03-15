"""
Chloe's Research Capability.

Uses web search + local model to find and synthesize information
relevant to her experiments and growth. The Python code orchestrates
the tools; the model interprets and synthesizes results.
"""

from typing import Dict, Optional, List
from .tools import web_search, fetch_webpage
from .knowledge import wiki_lookup, wiki_search


class Researcher:
    """Chloe's web research capability."""

    def __init__(self, brain):
        self.brain = brain

    def research_for_experiment(
        self,
        strategy_name: str,
        target_category: str,
        hypothesis: str,
        tier: str = "local",
    ) -> Optional[Dict]:
        """Research to inform an upcoming experiment."""
        query = f"AI prompt engineering {strategy_name} {target_category}"
        return self.research_topic(
            topic=query,
            context=(
                f"Experiment: {strategy_name} targeting {target_category}. "
                f"Hypothesis: {hypothesis[:200]}"
            ),
            tier=tier,
        )

    def research_topic(
        self,
        topic: str,
        context: str = "",
        max_sources: int = 3,
        tier: str = "local",
    ) -> Dict:
        """
        Research a topic: search -> fetch -> synthesize.

        Returns dict with query, sources, synthesis.
        """
        # Step 0: Check Wikipedia first (free, fast, reliable)
        sources = []
        try:
            wiki_result = wiki_lookup(topic, max_chars=3000)
            if wiki_result:
                sources.append({
                    "title": "Wikipedia",
                    "url": "https://en.wikipedia.org",
                    "snippet": "",
                    "content": wiki_result,
                })
        except Exception:
            pass  # Wikipedia is a bonus, not a requirement

        # Step 1: Search with retry logic
        results = None
        for attempt in range(3):
            results = web_search(topic, max_results=5)
            if results and not (len(results) == 1 and "error" in results[0]):
                break

        if not results or (len(results) == 1 and "error" in results[0]):
            # If web search failed but we have Wikipedia, still synthesize
            if sources:
                pass  # Fall through to synthesis with Wikipedia alone
            else:
                error_msg = results[0].get("error", "unknown") if results else "no response"
                return {
                    "query": topic,
                    "sources": [],
                    "synthesis": f"Web search failed ({error_msg}). Retried 3 times.",
                }

        # Step 2: Fetch top web results
        if results:
            for r in results[:max_sources]:
                url = r.get("url", "")
                if not url:
                    continue
                content = fetch_webpage(url, max_chars=3000)
                if not content.startswith("Fetch failed"):
                    sources.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": r.get("snippet", ""),
                        "content": content,
                    })

        # Fall back to snippets if fetching failed (but keep Wikipedia if present)
        if not sources and results:
            sources = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                    "content": r.get("snippet", ""),
                }
                for r in results if not r.get("error")
            ]

        if not sources:
            return {
                "query": topic,
                "sources": [],
                "synthesis": "Could not fetch any sources.",
            }

        # Step 3: Synthesize with model (local = free)
        source_text = "\n\n".join(
            f"--- {s['title']} ---\n{s['content'][:2000]}"
            for s in sources
        )

        response = self.brain.think(
            prompt=(
                f"Research topic: {topic}\n"
                f"{'Context: ' + context if context else ''}\n\n"
                f"Sources:\n{source_text}\n\n"
                "Synthesize the key findings. Focus on:\n"
                "1. Specific techniques or approaches\n"
                "2. What's directly applicable\n"
                "3. Implementation ideas\n"
                "Be concise and practical. 3-5 bullet points max."
            ),
            tier=tier,
            max_tokens=400,
            temperature=0.3,
        )

        return {
            "query": topic,
            "sources": [{"title": s["title"], "url": s["url"]} for s in sources],
            "synthesis": response["text"],
        }

    def explore_interest(self, interest: str, tier: str = "local") -> Dict:
        """
        Chloe-directed research -- she picks what to learn about.
        Used during free research phases.
        """
        query_response = self.brain.think(
            prompt=(
                f"You want to learn about: {interest}\n"
                "Generate a specific web search query that will find "
                "the most useful, practical information. Just the query, "
                "nothing else."
            ),
            tier=tier,
            max_tokens=50,
            temperature=0.5,
        )
        query = query_response["text"].strip().strip('"')
        return self.research_topic(topic=query, context=interest, tier=tier)
