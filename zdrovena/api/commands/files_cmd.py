"""zdrovena.api.commands.files_cmd — `zdrovena files list/download` subcommands."""

from __future__ import annotations

import argparse
import os
import sys


def _get_client():
    from zdrovena.api.client import ApiClient

    url = os.environ.get("ZDROVENA_API_URL")
    if not url:
        print("❌ ZDROVENA_API_URL is not set.", file=sys.stderr)
        sys.exit(1)
    token = os.environ.get("ZDROVENA_API_TOKEN") or None
    return ApiClient(url, token=token)


def _run_list(args: argparse.Namespace) -> None:
    client = _get_client()
    prefix = getattr(args, "prefix", "") or ""
    files = client.list_files(prefix=prefix)
    for f in files:
        print(f["key"])


def _run_upload(args: argparse.Namespace) -> None:
    import mimetypes

    client = _get_client()
    path = args.file
    key = args.key
    content_type = args.content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        data = fh.read()
    client.upload_file(key, data, content_type)
    print(f"✓ Wgrano: {key} ({len(data)} bajtów, {content_type})")


def _run_download(args: argparse.Namespace) -> None:
    client = _get_client()
    key = args.key
    output = getattr(args, "output", None)

    if output:
        with open(output, "wb") as fh:
            for chunk in client.stream_file(key):
                fh.write(chunk)
    else:
        buf = getattr(sys.stdout, "buffer", None)
        for chunk in client.stream_file(key):
            if buf is not None:
                buf.write(chunk)
            else:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    files_parser = subparsers.add_parser(
        "files",
        help="Operacje na plikach przechowywanych w Azure Storage",
    )
    files_sub = files_parser.add_subparsers(
        title="akcje",
        dest="files_action",
    )

    # list
    list_parser = files_sub.add_parser("list", help="Wylistuj pliki")
    list_parser.add_argument(
        "--prefix",
        default="",
        metavar="PREFIX",
        help="Filtruj po prefiksie klucza (np. invoices/sales/2025)",
    )
    list_parser.set_defaults(func=_run_list)

    # download
    dl_parser = files_sub.add_parser("download", help="Pobierz plik")
    dl_parser.add_argument("key", metavar="KEY", help="Klucz pliku w Storage")
    dl_parser.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="Ścieżka docelowa (domyślnie: stdout)",
    )
    dl_parser.set_defaults(func=_run_download)

    # upload
    up_parser = files_sub.add_parser("upload", help="Wgraj plik do Storage")
    up_parser.add_argument("key", metavar="KEY", help="Docelowy klucz w Storage")
    up_parser.add_argument(
        "--file",
        "-f",
        required=True,
        metavar="PATH",
        help="Ścieżka do pliku do wgrania",
    )
    up_parser.add_argument(
        "--content-type",
        default=None,
        metavar="TYPE",
        help="Content-Type (domyślnie: wykrywany z rozszerzenia)",
    )
    up_parser.set_defaults(func=_run_upload)

    def _files_default(args: argparse.Namespace) -> None:
        files_parser.print_help()
        sys.exit(1)

    files_parser.set_defaults(func=_files_default)
