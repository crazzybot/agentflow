"""Tests for agentflow.tools.arxiv_search.

Structure
---------
Unit tests  — mock the HTTP layer with ``unittest.mock.patch``; no network I/O.
Integration — guarded by the ``ARXIV_INTEGRATION`` env-var (or ``--integration``
              CLI flag added via the custom pytest option below).  They are
              *skipped* by default so ``uv run pytest`` always passes offline.
              If the network is unreachable they are also skipped (not failed).
"""

from __future__ import annotations

import os
import textwrap
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import httpx
import pytest

import agentflow.tools.arxiv_search as pkg
from agentflow.tools.arxiv_search import arxiv_search

_PATCH_TARGET = "agentflow.tools.arxiv_search.httpx.get"

# ---------------------------------------------------------------------------
# pytest hook — adds --integration CLI flag
# ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit the real arXiv API.",
    )


# ---------------------------------------------------------------------------
# Shared fixtures & helpers
# ---------------------------------------------------------------------------

_ATOM_NS = "http://www.w3.org/2005/Atom"


def _build_atom_feed(
    abstract_urls: list[str],
    *,
    titles: list[str] | None = None,
    summaries: list[str] | None = None,
) -> str:
    """Build a minimal but valid arXiv Atom XML feed string."""
    n = len(abstract_urls)
    titles = titles or [f"Title {i}" for i in range(n)]
    summaries = summaries or [f"Summary {i}" for i in range(n)]

    entries = "".join(
        textwrap.dedent(f"""\
            <entry>
              <id>{url}</id>
              <title>{title}</title>
              <summary>{summary}</summary>
            </entry>
            """)
        for url, title, summary in zip(abstract_urls, titles, summaries)
    )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<feed xmlns="{_ATOM_NS}">\n'
        "  <id>https://arxiv.org/api/query?search_query=all:test&amp;max_results=10</id>\n"
        '  <title type="html">ArXiv Query</title>\n'
        f"{entries}"
        "</feed>\n"
    )


def _mock_httpx_get(body: str, *, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.is_error = status_code >= 400
    mock_response.status_code = status_code
    mock_response.text = body
    mock_response.content = body.encode()

    mock_get = MagicMock(return_value=mock_response)
    return mock_get


# ---------------------------------------------------------------------------
# Unit tests — public API & exports
# ---------------------------------------------------------------------------

class TestPublicAPI:
    def test_function_importable_at_top_level(self) -> None:
        assert hasattr(pkg, "arxiv_search"), "pkg.arxiv_search not found"
        assert callable(pkg.arxiv_search)

    def test_all_declares_arxiv_search(self) -> None:
        assert "arxiv_search" in pkg.__all__


# ---------------------------------------------------------------------------
# Unit tests — input validation (no HTTP involved)
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            arxiv_search("")

    def test_whitespace_only_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            arxiv_search("   \t\n")

    def test_zero_max_results_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            arxiv_search("transformers", max_results=0)

    def test_negative_max_results_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            arxiv_search("transformers", max_results=-1)


# ---------------------------------------------------------------------------
# Unit tests — HTTP request construction
# ---------------------------------------------------------------------------

class TestRequestConstruction:
    def test_search_query_uses_all_prefix(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("neural networks")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["search_query"] == "all:neural networks"

    def test_max_results_forwarded(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("diffusion models", max_results=5)
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["max_results"] == 5

    def test_default_max_results_is_10(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("llm")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["max_results"] == 10

    def test_query_whitespace_stripped_before_sending(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("  attention mechanism  ")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["search_query"] == "all:attention mechanism"

    def test_timeout_is_set(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("gpt")
        _, kwargs = mock_get.call_args
        assert "timeout" in kwargs
        assert kwargs["timeout"] > 0

    def test_follow_redirects_is_enabled(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("bert")
        _, kwargs = mock_get.call_args
        assert kwargs.get("follow_redirects") is True

    def test_uses_https_endpoint(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get(_build_atom_feed([]))) as mock_get:
            arxiv_search("gpt")
        args, _ = mock_get.call_args
        url = args[0]
        assert url.startswith("https://"), f"Expected HTTPS URL, got: {url!r}"


# ---------------------------------------------------------------------------
# Unit tests — response parsing (happy path)
# ---------------------------------------------------------------------------

class TestResponseParsing:
    def test_returns_list_of_abstract_urls(self) -> None:
        expected = [
            "https://arxiv.org/abs/2301.00001",
            "https://arxiv.org/abs/2301.00002",
            "https://arxiv.org/abs/2301.00003",
        ]
        atom = _build_atom_feed(expected)
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("quantum computing", max_results=3)
        assert result == expected

    def test_result_order_matches_feed_order(self) -> None:
        urls = [f"https://arxiv.org/abs/2401.{i:05d}" for i in range(1, 6)]
        atom = _build_atom_feed(urls)
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("ordering test", max_results=5)
        assert result == urls

    def test_empty_feed_returns_empty_list(self) -> None:
        atom = _build_atom_feed([])
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("definitely_no_results_xyz987")
        assert result == []

    def test_feed_level_id_not_included_in_results(self) -> None:
        paper_url = "https://arxiv.org/abs/2301.11111"
        atom = _build_atom_feed([paper_url])
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("single paper")
        assert result == [paper_url]

    def test_whitespace_around_id_text_is_stripped(self) -> None:
        atom = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<feed xmlns="{_ATOM_NS}">\n'
            "  <entry>\n"
            "    <id>   https://arxiv.org/abs/2301.99999   </id>\n"
            "    <title>T</title>\n"
            "  </entry>\n"
            "</feed>\n"
        )
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("whitespace")
        assert result == ["https://arxiv.org/abs/2301.99999"]

    def test_single_result(self) -> None:
        url = "https://arxiv.org/abs/1706.03762"
        atom = _build_atom_feed([url], titles=["Attention Is All You Need"])
        with patch(_PATCH_TARGET, _mock_httpx_get(atom)):
            result = arxiv_search("attention is all you need", max_results=1)
        assert result == [url]


# ---------------------------------------------------------------------------
# Unit tests — error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_http_500_raises_runtime_error(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get("Internal Server Error", status_code=500)):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                arxiv_search("ml")

    def test_http_503_raises_runtime_error(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get("Service Unavailable", status_code=503)):
            with pytest.raises(RuntimeError, match="HTTP 503"):
                arxiv_search("ml")

    def test_http_404_raises_runtime_error(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get("Not Found", status_code=404)):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                arxiv_search("ml")

    def test_connect_error_raises_runtime_error(self) -> None:
        mock_get = MagicMock(side_effect=httpx.ConnectError("Connection refused"))
        with patch(_PATCH_TARGET, mock_get):
            with pytest.raises(RuntimeError, match="Network error"):
                arxiv_search("ml")

    def test_timeout_error_raises_runtime_error(self) -> None:
        mock_get = MagicMock(side_effect=httpx.TimeoutException("Timed out"))
        with patch(_PATCH_TARGET, mock_get):
            with pytest.raises(RuntimeError, match="Network error"):
                arxiv_search("ml")

    def test_malformed_xml_raises_runtime_error(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get("<<this is not xml>>")):
            with pytest.raises(RuntimeError, match="parse"):
                arxiv_search("ml")

    def test_runtime_error_chains_original_cause_for_network(self) -> None:
        original = httpx.ConnectError("refused")
        mock_get = MagicMock(side_effect=original)
        with patch(_PATCH_TARGET, mock_get):
            with pytest.raises(RuntimeError) as exc_info:
                arxiv_search("ml")
        assert exc_info.value.__cause__ is original

    def test_runtime_error_chains_original_cause_for_parse(self) -> None:
        with patch(_PATCH_TARGET, _mock_httpx_get("<bad")):
            with pytest.raises(RuntimeError) as exc_info:
                arxiv_search("ml")
        assert isinstance(exc_info.value.__cause__, ET.ParseError)


# ---------------------------------------------------------------------------
# Integration smoke test — skipped unless opted in
# ---------------------------------------------------------------------------

def _integration_enabled(config: pytest.Config | None = None) -> bool:
    env_flag = os.environ.get("ARXIV_INTEGRATION", "0") not in ("0", "", "false", "False")
    cli_flag = config.getoption("--integration", default=False) if config else False
    return env_flag or cli_flag


@pytest.mark.skipif(
    not _integration_enabled(),
    reason=(
        "Integration test skipped by default. "
        "Run with `ARXIV_INTEGRATION=1 pytest` or `pytest --integration` to enable."
    ),
)
def test_integration_attention_is_all_you_need(request: pytest.FixtureRequest) -> None:
    """Hit the real arXiv API and verify results for a well-known paper."""
    if not _integration_enabled(request.config):
        pytest.skip("Integration tests not enabled.")

    try:
        results = arxiv_search("attention is all you need", max_results=5)
    except RuntimeError as exc:
        pytest.skip(f"arXiv API unreachable (network restricted?): {exc}")

    assert isinstance(results, list), "Result must be a list"
    assert len(results) > 0, "Expected at least one result from arXiv"

    for url in results:
        assert isinstance(url, str), f"Each result must be a str, got {type(url)}"
        assert url.startswith("https://arxiv.org/abs/"), (
            f"URL does not start with expected prefix: {url!r}"
        )

    assert any("1706.03762" in url for url in results), (
        f"Expected 1706.03762 in results, got: {results}"
    )
