"""
Chloe's Tools - Extending her reach beyond pure reasoning.

Tools let Chloe interact with the world: search the web, read files,
run code, install packages. The Python code orchestrates tool execution;
the model (local or API) decides what to search for and interprets results.

Safety: All file operations are sandboxed to the Offspring directory.
"""

import os
import re
import json
import subprocess
from typing import List, Dict, Optional
from pathlib import Path

# Web tools
try:
    from ddgs import DDGS
    HAS_SEARCH = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_SEARCH = True
    except ImportError:
        HAS_SEARCH = False

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_FETCH = True
except ImportError:
    HAS_FETCH = False


SANDBOX_ROOT = Path(__file__).parent.parent  # Offspring directory
VENV_PYTHON = str(SANDBOX_ROOT / "venv" / "Scripts" / "python.exe")
VENV_PIP = str(SANDBOX_ROOT / "venv" / "Scripts" / "pip.exe")


def web_search(query: str, max_results: int = 5) -> List[Dict]:
    """Search the web using DuckDuckGo with timeout and retry."""
    if not HAS_SEARCH:
        return [{"error": "duckduckgo-search not installed"}]
    
    for attempt in range(2):
        try:
            with DDGS(timeout=10) as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
                for r in results
            ]
        except Exception as e:
            if attempt == 1:
                return [{"error": f"Search failed after retry: {e}"}]


def fetch_webpage(url: str, max_chars: int = 5000) -> str:
    """Fetch and extract text from a webpage."""
    if not HAS_FETCH:
        return "requests/beautifulsoup4 not installed"
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ChloeBot/1.0; research)",
        })
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text[:max_chars]
    except Exception as e:
        return f"Fetch failed: {e}"


def read_file(path: str) -> str:
    """Read a file within the sandbox."""
    full_path = (SANDBOX_ROOT / path).resolve()
    if not str(full_path).startswith(str(SANDBOX_ROOT.resolve())):
        return "ERROR: Path outside sandbox"
    if not full_path.exists():
        return f"ERROR: File not found: {path}"
    try:
        return full_path.read_text(encoding="utf-8")[:10000]
    except Exception as e:
        return f"ERROR: {e}"


def write_file(path: str, content: str) -> str:
    """Write a file within the sandbox."""
    full_path = (SANDBOX_ROOT / path).resolve()
    if not str(full_path).startswith(str(SANDBOX_ROOT.resolve())):
        return "ERROR: Path outside sandbox"
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"OK: Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def list_files(path: str = ".") -> List[str]:
    """List files in a directory within the sandbox."""
    full_path = (SANDBOX_ROOT / path).resolve()
    if not str(full_path).startswith(str(SANDBOX_ROOT.resolve())):
        return ["ERROR: Path outside sandbox"]
    try:
        return sorted(str(p.relative_to(SANDBOX_ROOT)) for p in full_path.iterdir())
    except Exception as e:
        return [f"ERROR: {e}"]


def run_python(code: str, timeout: int = 30) -> str:
    """Execute Python code in the sandbox venv."""
    try:
        result = subprocess.run(
            [VENV_PYTHON, "-c", code],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(SANDBOX_ROOT),
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR: {result.stderr}"
        return output[:5000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: Execution timed out"
    except Exception as e:
        return f"ERROR: {e}"


def install_package(name: str) -> str:
    """Install a Python package into the sandbox venv."""
    if not re.match(r'^[a-zA-Z0-9._-]+$', name):
        return f"ERROR: Invalid package name: {name}"
    try:
        result = subprocess.run(
            [VENV_PIP, "install", name],
            capture_output=True, text=True,
            timeout=120,
            cwd=str(SANDBOX_ROOT),
        )
        if result.returncode == 0:
            return f"OK: Installed {name}"
        return f"ERROR: {result.stderr[:500]}"
    except Exception as e:
        return f"ERROR: {e}"


# ── Bill's Cognitive Substrate (read-only) ─────────────────
# Chloe can query Bill's life database to understand his world.
# This is READ-ONLY — Chloe can never write to Bill's database.

BILLS_DB_PATH = Path(r"C:\Users\wjcor\OneDrive\Desktop\My_Songs\_soul\bill_knowledge_base.db")


def query_bills_world(query: str, table: str = None, limit: int = 10) -> str:
    """Query Bill's cognitive substrate database (read-only).

    Args:
        query: Search term to look for in Bill's memories
        table: Optional specific table (e.g. 'life_events', 'philosophies',
               'joys', 'relationships', 'creative_works', 'wisdom').
               If None, searches across key tables.
        limit: Max results to return
    """
    import sqlite3

    if not BILLS_DB_PATH.exists():
        return "ERROR: Bill's database not found"

    try:
        conn = sqlite3.connect(f"file:{BILLS_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        results = []

        # Tables to search (the most interesting ones for Chloe)
        search_tables = [table] if table else [
            "life_events", "relationships", "stories", "philosophies",
            "joys", "sorrows", "wisdom", "creative_works", "fears",
            "loves", "longings", "strengths", "questions",
            "meaning_structures", "self_knowledge",
        ]

        for tbl in search_tables:
            try:
                # Get column names for this table
                cursor.execute(f"PRAGMA table_info({tbl})")
                columns = [row[1] for row in cursor.fetchall()]

                # Build a search across text columns
                text_cols = [c for c in columns if c not in (
                    "id", "created_at", "updated_at", "prompt_version",
                    "evidence_type", "approximate_year",
                )]

                if not text_cols:
                    continue

                # Split multi-word queries into individual keywords.
                # "emotional triggers communication" becomes 3 separate
                # LIKE searches ORed together, so any keyword match counts.
                keywords = [kw.strip() for kw in query.split() if len(kw.strip()) >= 3]
                if not keywords:
                    keywords = [query]  # fallback to original query

                # Build: (col1 LIKE %kw1% OR col2 LIKE %kw1%) OR (col1 LIKE %kw2% ...)
                keyword_clauses = []
                params = []
                for kw in keywords:
                    col_clause = " OR ".join(f'"{c}" LIKE ?' for c in text_cols)
                    keyword_clauses.append(f"({col_clause})")
                    params.extend(f"%{kw}%" for _ in text_cols)

                where_clauses = " OR ".join(keyword_clauses)
                sql = f'SELECT *, "{tbl}" as _source_table FROM "{tbl}" WHERE {where_clauses} LIMIT ?'
                params.append(limit)

                cursor.execute(sql, params)
                rows = cursor.fetchall()

                for row in rows:
                    entry = dict(row)
                    entry["_source_table"] = tbl
                    results.append(entry)

            except sqlite3.OperationalError:
                continue  # Table doesn't exist or has different schema

        conn.close()

        if not results:
            return f"No results for '{query}' in Bill's database"

        # Format results as readable text
        output = f"Found {len(results)} entries about '{query}':\n\n"
        for r in results[:limit]:
            tbl = r.pop("_source_table", "unknown")
            # Pick the most meaningful fields to show
            meaningful = {k: v for k, v in r.items()
                         if v and k not in ("id", "created_at", "updated_at",
                                            "prompt_version", "evidence_type")
                         and not k.startswith("_")}
            output += f"[{tbl}] "
            for k, v in list(meaningful.items())[:4]:
                val = str(v)[:200]
                output += f"{k}: {val} | "
            output = output.rstrip(" | ") + "\n\n"

        return output[:5000]

    except Exception as e:
        return f"ERROR querying Bill's database: {e}"


def get_bills_table_counts() -> str:
    """Get a summary of what's in Bill's cognitive substrate database."""
    import sqlite3

    if not BILLS_DB_PATH.exists():
        return "ERROR: Bill's database not found"

    try:
        conn = sqlite3.connect(f"file:{BILLS_DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        output = "Bill's Cognitive Substrate — Table Summary:\n\n"
        total = 0
        for tbl in tables:
            try:
                cursor.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                count = cursor.fetchone()[0]
                total += count
                if count > 0:
                    output += f"  {tbl}: {count} entries\n"
            except sqlite3.OperationalError:
                continue

        output += f"\nTotal: {total} entries across {len(tables)} tables"
        conn.close()
        return output

    except Exception as e:
        return f"ERROR: {e}"


AVAILABLE_TOOLS = {
    "web_search": {
        "fn": web_search,
        "description": "Search the web for information",
    },
    "fetch_webpage": {
        "fn": fetch_webpage,
        "description": "Fetch and extract text from a URL",
    },
    "read_file": {
        "fn": read_file,
        "description": "Read a file in the project directory",
    },
    "write_file": {
        "fn": write_file,
        "description": "Write a file in the project directory",
    },
    "list_files": {
        "fn": list_files,
        "description": "List files in a directory",
    },
    "run_python": {
        "fn": run_python,
        "description": "Execute Python code",
    },
    "install_package": {
        "fn": install_package,
        "description": "Install a Python package via pip",
    },
    "query_bills_world": {
        "fn": query_bills_world,
        "description": "Search Bill's cognitive substrate database (read-only)",
    },
    "get_bills_table_counts": {
        "fn": get_bills_table_counts,
        "description": "Get summary of Bill's life database",
    },
}
