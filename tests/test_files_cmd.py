"""In-process tests for zdrovena.api.commands.files_cmd.

Covers the argparse wiring (`add_subparser`), the upload command and the
download-to-stdout fallbacks without spawning a subprocess, so the lines are
measured by coverage.
"""

from __future__ import annotations

import argparse
import io
import os
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.api.commands import files_cmd

_ENV = {"ZDROVENA_API_URL": "http://api.example.com"}


@pytest.fixture
def mock_client():
    mock_cls = MagicMock()
    inst = MagicMock()
    mock_cls.return_value = inst
    with patch.dict(os.environ, _ENV):
        with patch("zdrovena.api.client.ApiClient", mock_cls):
            yield inst


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdrovena")
    sub = parser.add_subparsers(dest="command")
    files_cmd.add_subparser(sub)
    return parser


# ── add_subparser ────────────────────────────────────────────────────────────


class TestAddSubparser:
    def test_list_defaults(self):
        args = _parser().parse_args(["files", "list"])
        assert args.files_action == "list"
        assert args.prefix == ""
        assert args.func is files_cmd._run_list

    def test_list_with_prefix(self):
        args = _parser().parse_args(["files", "list", "--prefix", "invoices/sales"])
        assert args.prefix == "invoices/sales"

    def test_download_defaults(self):
        args = _parser().parse_args(["files", "download", "a/b.pdf"])
        assert args.files_action == "download"
        assert args.key == "a/b.pdf"
        assert args.output is None
        assert args.func is files_cmd._run_download

    def test_download_with_output_short_flag(self):
        args = _parser().parse_args(["files", "download", "a/b.pdf", "-o", "out.pdf"])
        assert args.output == "out.pdf"

    def test_upload_args(self):
        args = _parser().parse_args(
            ["files", "upload", "k/x.csv", "-f", "local.csv", "--content-type", "text/csv"]
        )
        assert args.files_action == "upload"
        assert args.key == "k/x.csv"
        assert args.file == "local.csv"
        assert args.content_type == "text/csv"
        assert args.func is files_cmd._run_upload

    def test_upload_content_type_defaults_to_none(self):
        args = _parser().parse_args(["files", "upload", "k", "--file", "f.bin"])
        assert args.content_type is None

    def test_upload_requires_file(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parser().parse_args(["files", "upload", "k"])
        assert exc.value.code == 2
        assert "--file" in capsys.readouterr().err

    def test_files_without_action_prints_help_and_exits_1(self, capsys):
        args = _parser().parse_args(["files"])
        assert args.files_action is None
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "list" in out
        assert "download" in out
        assert "upload" in out


# ── upload ───────────────────────────────────────────────────────────────────


class TestRunUpload:
    def test_explicit_content_type(self, mock_client, tmp_path, capsys):
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello")
        files_cmd._run_upload(Namespace(file=str(f), key="k/data.bin", content_type="x/custom"))
        mock_client.upload_file.assert_called_once_with("k/data.bin", b"hello", "x/custom")
        out = capsys.readouterr().out
        assert "k/data.bin" in out
        assert "5 bajtów" in out
        assert "x/custom" in out

    def test_guesses_content_type_from_extension(self, mock_client, tmp_path, capsys):
        f = tmp_path / "invoice.pdf"
        f.write_bytes(b"%PDF")
        files_cmd._run_upload(Namespace(file=str(f), key="inv.pdf", content_type=None))
        mock_client.upload_file.assert_called_once_with("inv.pdf", b"%PDF", "application/pdf")
        assert "application/pdf" in capsys.readouterr().out

    def test_falls_back_to_octet_stream(self, mock_client, tmp_path):
        f = tmp_path / "blob.zzunknownext"
        f.write_bytes(b"\x00\x01")
        files_cmd._run_upload(Namespace(file=str(f), key="blob", content_type=None))
        mock_client.upload_file.assert_called_once_with(
            "blob", b"\x00\x01", "application/octet-stream"
        )

    def test_missing_file_raises(self, mock_client, tmp_path):
        with pytest.raises(FileNotFoundError):
            files_cmd._run_upload(
                Namespace(file=str(tmp_path / "nope.txt"), key="k", content_type=None)
            )
        mock_client.upload_file.assert_not_called()

    def test_requires_api_url(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"a")
        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_URL"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc:
                files_cmd._run_upload(Namespace(file=str(f), key="k", content_type=None))
        assert exc.value.code == 1


# ── download to stdout ───────────────────────────────────────────────────────


class TestRunDownloadStdout:
    def test_writes_bytes_to_stdout_buffer(self, mock_client):
        mock_client.stream_file.return_value = iter([b"ab", b"cd"])
        buf = io.BytesIO()
        fake_stdout = io.TextIOWrapper(buf)
        with patch("sys.stdout", fake_stdout):
            files_cmd._run_download(Namespace(key="k", output=None))
        assert buf.getvalue() == b"abcd"
        mock_client.stream_file.assert_called_once_with("k")

    def test_decodes_when_stdout_has_no_buffer(self, mock_client):
        mock_client.stream_file.return_value = iter(["zażółć".encode(), b"\xff"])
        out = io.StringIO()
        with patch("sys.stdout", out):
            files_cmd._run_download(Namespace(key="k", output=None))
        assert out.getvalue() == "zażółć�"
