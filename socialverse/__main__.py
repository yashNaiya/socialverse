"""Start the Socialverse server:  python -m socialverse  →  http://127.0.0.1:8000"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from . import store
from .api import PORT, create_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Socialverse server (local only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--root", default=str(store.WORLDS_DIR), help="folder that holds the worlds")
    args = p.parse_args(argv)
    if args.host not in LOOPBACK:
        raise SystemExit("Socialverse only listens on this computer (127.0.0.1). Refusing --host "
                         f"{args.host!r}: exposing agents and the human seat to a network is not supported.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "ollama"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"Socialverse running at http://127.0.0.1:{args.port}  (Ctrl+C to stop; worlds are saved)")
    uvicorn.run(create_app(args.root, port=args.port), host=args.host, port=args.port, log_level="warning",
                server_header=False, date_header=False)


if __name__ == "__main__":
    main()
