"""psd.ai desktop — a native Qt GUI for the psd.ai workspace.

This package replaces the browser-on-localhost workflow. Nothing here opens a
TCP port, prints a URL, or launches a web browser: the FastAPI application
object is driven *in process* over an ASGI transport (:mod:`gui.backend`), and
every screen is a real Qt widget.

Layout
------
``backend.py``      in-process ASGI bridge + SSE streaming (no sockets)
``api.py``          typed client for the psd.ai HTTP API surface
``workers.py``      Qt worker plumbing (calls run off the GUI thread)
``theme.py``        psd.ai palette, fonts and the application stylesheet
``markdown_html.py`` Markdown -> Qt rich text renderer for chat/documents
``logbus.py``       live backend log capture for the Logs view
``shell.py``        the main window: navigation rail + view stack + status bar
``views/``          one module per workspace screen
``widgets/``        reusable pieces (composer, message cards, tables, dialogs)
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME", "APP_TITLE"]

__version__ = "1.0.0"
APP_NAME = "psd.ai"
APP_TITLE = "psd.ai"
