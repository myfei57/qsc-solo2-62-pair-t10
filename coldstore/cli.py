"""Command line entry point for the control service."""

from __future__ import annotations

import argparse
import logging
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from .console.seed import seed_defaults
from .console.server import Server
from .console.version import version_string
from .segments import SegmentedStore, import_legacy
from .store import Store

LOGGER = logging.getLogger("coldstore")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Serve concurrent control requests from a pool of worker threads."""

    daemon_threads = True


def parse_address(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdigit():
        raise argparse.ArgumentTypeError("address must look like host:port")
    return host or "0.0.0.0", int(port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coldstore", description="control service")
    parser.add_argument(
        "--store-dir",
        default="state",
        help="root directory of the segmented store (default: state/)",
    )
    parser.add_argument(
        "--store",
        default="",
        help="legacy single-file store path; enables legacy mode",
    )
    parser.add_argument(
        "--migrate-from",
        default="",
        help="import a legacy JSON store into the segmented store at startup",
    )
    parser.add_argument("--addr", default=":8080", help="listen address as host:port")
    parser.add_argument("--version", action="store_true", help="print the build version and exit")
    return parser


def open_store(args: argparse.Namespace) -> Store:
    """Open the segmented store by default, the legacy one on request."""

    if args.store:
        store = Store.open(args.store)
        LOGGER.info("legacy single-file store at %s", store.path)
        return store
    segmented = SegmentedStore.open(args.store_dir)
    LOGGER.info("segmented store rooted at %s", segmented.path)
    if args.migrate_from:
        result = import_legacy(segmented, args.migrate_from)
        LOGGER.info("migrated legacy store: %s", result)
    return segmented


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(version_string())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = open_store(args)
    try:
        seed_defaults(store)
        host, port = parse_address(args.addr)
        application = Server(store)
        with make_server(host, port, application, server_class=ThreadingWSGIServer) as httpd:
            LOGGER.info("control console listening on %s:%d", host, port)
            httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("control console stopped")
    finally:
        store.close()
    return 0
