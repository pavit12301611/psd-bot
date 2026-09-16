"""psd.ai terminal interface package.

Holds the thin loopback/ASGI client used by ``psd_tui.py``. The interface logic
lives in ``psd_tui.py`` so the launcher can run it as a plain script.
"""

from .api_client import TuiApiClient, ApiError  # noqa: F401

__all__ = ["TuiApiClient", "ApiError"]
