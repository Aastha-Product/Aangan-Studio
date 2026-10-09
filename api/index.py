"""Vercel entry point: every route is rewritten here (vercel.json) and served by the WSGI app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.web import app  # noqa: E402,F401
