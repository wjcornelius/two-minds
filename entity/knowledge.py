"""
Wikipedia knowledge lookup for Chloe's agent loop.

Zero external dependencies beyond `requests` (already installed).
No API key. No auth. Free forever. 200 req/sec rate limit.

Usage:
    from entity.knowledge import wiki_lookup, wiki_search, wiki_summary

    # Quick factual lookup (search + summary in one call)
    answer = wiki_lookup("what causes bipolar disorder")

    # Direct summary if you know the article title
    info = wiki_summary("Bipolar disorder")

    # Search when you need to find the right article
    results = wiki_search("GPU memory bandwidth")
"""

import requests
import urllib.parse
import logging

logger = logging.getLogger(__name__)

WIKI_ACTION_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST_API = "https://en.wikipedia.org/api/rest_v1"
HEADERS = {
    "User-Agent": "Chloe-CognitiveAgent/1.0 (https://github.com/wjcornelius/Offspring)"
}


def wiki_summary(topic: str) -> dict:
    """
    Fast lookup — returns intro paragraphs + one-line description.
    Uses REST v1 which is CDN-cached (~100ms).

    Returns: {exists, title, description, extract, url}
    """
    encoded = urllib.parse.quote(topic.replace(" ", "_"))
    url = f"{WIKI_REST_API}/page/summary/{encoded}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            return {"exists": False, "topic": topic}
        resp.raise_for_status()
        data = resp.json()
        return {
            "exists": True,
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "extract": data.get("extract", ""),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
        }
    except requests.exceptions.Timeout:
        logger.warning(f"wiki_summary timeout for '{topic}'")
        return {"exists": False, "topic": topic, "error": "timeout"}
    except Exception as e:
        logger.error(f"wiki_summary failed for '{topic}': {e}")
        return {"exists": False, "topic": topic, "error": str(e)}


def wiki_search(query: str, limit: int = 5) -> list:
    """
    Search Wikipedia. Returns list of {title, snippet, wordcount}.
    Use when you don't know the exact article title.
    """
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "utf8": 1,
    }
    try:
        resp = requests.get(
            WIKI_ACTION_API, params=params, headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        results = resp.json().get("query", {}).get("search", [])
        return [
            {
                "title": r["title"],
                "snippet": (
                    r.get("snippet", "")
                    .replace('<span class="searchmatch">', "")
                    .replace("</span>", "")
                ),
                "wordcount": r.get("wordcount", 0),
            }
            for r in results
        ]
    except Exception as e:
        logger.error(f"wiki_search failed for '{query}': {e}")
        return []


def wiki_full_article(topic: str, intro_only: bool = False) -> str:
    """
    Get article text as plain text.
    intro_only=True gets just the lead section (faster, smaller).
    intro_only=False gets the entire article.
    """
    params = {
        "action": "query",
        "format": "json",
        "titles": topic,
        "prop": "extracts",
        "explaintext": True,
        "utf8": 1,
    }
    if intro_only:
        params["exintro"] = True

    try:
        resp = requests.get(
            WIKI_ACTION_API, params=params, headers=HEADERS, timeout=20
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        page = next(iter(pages.values()))
        if page.get("pageid") is None:
            return ""
        return page.get("extract", "")
    except Exception as e:
        logger.error(f"wiki_full_article failed for '{topic}': {e}")
        return ""


def wiki_lookup(query: str, max_chars: int = 2000) -> str:
    """
    Main entry point for Chloe's agent loop.

    Workflow: search to find the best article, then fetch its summary.
    Returns a formatted string ready to inject into context, or empty
    string if nothing found.

    Args:
        query: natural language query ("what causes bipolar disorder")
        max_chars: cap response length for context window management
    """
    # Step 1: Search to find the right article
    results = wiki_search(query, limit=3)
    if not results:
        return ""

    best_title = results[0]["title"]

    # Step 2: Get the summary
    info = wiki_summary(best_title)
    if not info.get("exists"):
        return ""

    # Step 3: Format for context injection
    parts = [f"[Wikipedia: {info['title']}]"]
    if info.get("description"):
        parts.append(f"({info['description']})")
    if info.get("extract"):
        extract = info["extract"]
        if len(extract) > max_chars:
            extract = extract[:max_chars] + "..."
        parts.append(extract)
    if info.get("url"):
        parts.append(f"Source: {info['url']}")

    return "\n".join(parts)


if __name__ == "__main__":
    print("=== Quick Summary ===")
    info = wiki_summary("Bipolar disorder")
    print(f"Title: {info.get('title')}")
    print(f"Description: {info.get('description')}")
    print(f"Extract: {info.get('extract', '')[:300]}...")
    print()

    print("=== Search ===")
    results = wiki_search("EEG neurofeedback consumer")
    for r in results:
        print(f"  - {r['title']} ({r['wordcount']} words)")
    print()

    print("=== Agent Lookup ===")
    answer = wiki_lookup("frontal alpha asymmetry mood regulation")
    print(answer)
