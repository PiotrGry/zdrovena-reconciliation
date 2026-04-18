"""zdrovena.api.commands.health_cmd — `zdrovena health` subcommand."""

from __future__ import annotations

import argparse
import os
import sys


def _run(args: argparse.Namespace) -> None:
    from zdrovena.api.client import ApiClient, ApiError

    url = os.environ.get("ZDROVENA_API_URL")
    if not url:
        print("❌ ZDROVENA_API_URL is not set.", file=sys.stderr)
        sys.exit(1)

    token = os.environ.get("ZDROVENA_API_TOKEN") or None
    client = ApiClient(url, token=token)

    try:
        result = client.health()
        print(f"status: {result.get('status')}")
        print(f"version: {result.get('version')}")
        sys.exit(0)
    except ApiError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    sp = subparsers.add_parser(
        "health",
        help="Sprawdź dostępność API",
    )
    sp.set_defaults(func=_run)
