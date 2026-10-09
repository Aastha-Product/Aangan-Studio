"""Run the web app locally: http://localhost:8000/dashboard

    .venv/Scripts/python scripts/dev_server.py            # your local call log (out/local_store.json)
    .venv/Scripts/python scripts/dev_server.py --demo     # synthetic data from scripts/demo_data.py
"""
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, web  # noqa: E402
from backend.store import LocalStore  # noqa: E402


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True   # one slow/open browser connection must not block the rest


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8000
    if "--demo" in sys.argv:
        web._store = LocalStore(config.ROOT / "out" / "demo_store.json")
    print(f"http://localhost:{port}/dashboard" + (f"?token={config.DASHBOARD_TOKEN}" if config.DASHBOARD_TOKEN else ""))
    make_server("127.0.0.1", port, web.app, server_class=ThreadingWSGIServer).serve_forever()
