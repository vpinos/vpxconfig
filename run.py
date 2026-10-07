#!/usr/bin/env python3
"""Start VPXConfig: ./run.py [--host 127.0.0.1] [--port 1111]   (also the entry point of the built executable)"""
import argparse

from vpxconfig import __version__
from vpxconfig.server import serve

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: localhost only)")
ap.add_argument("--port", type=int, default=1111)
ap.add_argument("--open-chrome", action="store_true",
                 help="open google-chrome on the server once it's up, and stop (closing the window too) once "
                      "either Chrome or the page's own Quit button closes it -- off by default, so a plain "
                      "./run.py still just serves the page for you to open in any browser yourself")
ap.add_argument("--version", action="version", version=f"VPXConfig {__version__}")
args = ap.parse_args()
serve(args.host, args.port, open_chrome=args.open_chrome)
