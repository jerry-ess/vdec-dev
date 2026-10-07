"""Start the VDec BI dashboard (API + web UI + background refresher).

    python run_dashboard.py            # http://127.0.0.1:8050
    python run_dashboard.py --port 9000 --host 0.0.0.0

Production: run the ASGI app behind a reverse proxy with several workers, e.g.
    uvicorn vdash.asgi:app --workers 4 --host 0.0.0.0 --port 8050
and run the refresher as its own process (python -m vdash.refresher).
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from vdash.api import create_app  # noqa: E402
from vdash.config import Settings  # noqa: E402

if __name__ == "__main__":
    s = Settings.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=s.host)
    ap.add_argument("--port", type=int, default=s.port)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"VDec BI dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
