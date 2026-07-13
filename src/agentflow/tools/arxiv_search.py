"""Lightweight arXiv search client."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

__all__ = ["arxiv_search"]

logger = logging.getLogger(__name__)

_API_URL = "https://export.arxiv.org/api/query"
_ATOM_NS = "http://www.w3.org/2005/Atom"


def arxiv_search(
    query: str,
    max_results: int = 5,
    category: str | None = None,
) -> list[dict[str, str]]:
    """Search arXiv and return paper metadata including title, abstract, and URL.

    Args:
        query: Free-text search query (mapped to the ``all:`` field).
        max_results: Maximum number of results to return (default 5).
        category: Optional arXiv subject category filter, e.g. ``"cs.LG"``,
            ``"q-fin.TR"``, ``"stat.ML"``.  When set, only papers in that
            category are returned, which sharply reduces off-topic results.

    Returns:
        A list of dicts with keys: "title", "abstract", "url".

    Raises:
        ValueError: If *query* is empty or *max_results* is not positive.
        RuntimeError: If the HTTP request fails or returns a non-2xx status.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    if max_results < 1:
        raise ValueError("max_results must be a positive integer")

    search_query = f"all:{query.strip()}"
    if category:
        search_query = f"cat:{category.strip()} AND {search_query}"

    params = {
        "search_query": search_query,
        "max_results": max_results,
    }

    logger.debug("GET %s params=%s", _API_URL, params)

    try:
        response = httpx.get(
            _API_URL,
            params=params,
            timeout=30.0,
            follow_redirects=True,
        )
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Network error while contacting arXiv API: {exc}"
        ) from exc

    if response.is_error:
        raise RuntimeError(
            f"arXiv API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    logger.debug("Response %s, %d bytes", response.status_code, len(response.content))

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse arXiv XML response: {exc}") from exc

    results: list[dict[str, str]] = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        title_el = entry.find(f"{{{_ATOM_NS}}}title")
        summary_el = entry.find(f"{{{_ATOM_NS}}}summary")
        if id_el is None or not id_el.text:
            continue
        results.append({
            "url": id_el.text.strip(),
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "abstract": " ".join((summary_el.text or "").split()) if summary_el is not None else "",
        })

    logger.info("arxiv_search(%r, cat=%r, max_results=%d) → %d results", query, category, max_results, len(results))
    return results
