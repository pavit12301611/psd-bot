# services/__init__.py
"""
Service layer — plug-in capabilities for the chat core.

Each service:
- Does one thing well
- Exposes a clean async interface
- Can run in-process or as a standalone HTTP service
"""

try:
    from .search import SearchService, SearchResult, SearchResponse
except ImportError:
    SearchService = SearchResult = SearchResponse = None

try:
    from .docs import DocsService, DocChunk, IndexResult
except ImportError:
    DocsService = DocChunk = IndexResult = None

try:
    from .research import ResearchService, ResearchResult, ResearchSource
except ImportError:
    ResearchService = ResearchResult = ResearchSource = None

try:
    from .memory import MemoryService, Memory, MemorySearchResult
except ImportError:
    MemoryService = Memory = MemorySearchResult = None

try:
    from .shell import ShellService, ShellResult
except ImportError:
    ShellService = ShellResult = None

from .browser import EmbeddedBrowserService, get_browser_service, SEARCH_ENGINES

__all__ = [
    # Search
    "SearchService",
    "SearchResult",
    "SearchResponse",
    # Docs
    "DocsService",
    "DocChunk",
    "IndexResult",
    # Research
    "ResearchService",
    "ResearchResult",
    "ResearchSource",
    # Memory
    "MemoryService",
    "Memory",
    "MemorySearchResult",
    # Shell
    "ShellService",
    "ShellResult",
    # Browser
    "EmbeddedBrowserService",
    "get_browser_service",
    "SEARCH_ENGINES",
]
