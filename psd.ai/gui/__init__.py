"""gui — the psd.ai desktop (GUI) application package.

Contains the thin API client (drives the same FastAPI backend as the web UI)
and the backend runner (embedded in-process app, or attached/serve uvicorn).
The window itself lives in ``psd_gui.py`` at the psd.ai package root.
"""
