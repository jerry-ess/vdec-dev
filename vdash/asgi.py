"""ASGI entry point for production servers: ``uvicorn vdash.asgi:app --workers 4``.

With several workers, set refresh.enabled=false in settings.json and run
``python -m vdash.refresher`` as one separate process so only one refresher runs.
"""
from .api import create_app

app = create_app()
