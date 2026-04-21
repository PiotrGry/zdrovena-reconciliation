"""zdrovena.api.client — HTTP client for the Zdrovena REST API."""

from __future__ import annotations

from collections.abc import Iterator

import httpx


class ApiError(Exception):
    """Raised on HTTP errors or connection failures."""


class ApiClient:
    """Thin httpx wrapper for the Zdrovena API."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self._base_url = base_url
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def close(
        self,
        year: int = 0,
        month: int = 0,
        *,
        dry_run: bool = False,
        ignore_warnings: bool = False,
        ignore_vendors: list[str] | None = None,
    ) -> dict:
        payload = {
            "year": year,
            "month": month,
            "dry_run": dry_run,
            "ignore_warnings": ignore_warnings,
            "ignore_vendors": ignore_vendors or [],
        }
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers) as client:
                resp = client.post("/close", json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ApiError(str(exc)) from exc
        except httpx.ConnectError as exc:
            raise ApiError(str(exc)) from exc

    def list_files(self, *, prefix: str = "") -> list:
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers) as client:
                resp = client.get("/files", params={"prefix": prefix} if prefix else {})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ApiError(str(exc)) from exc
        except httpx.ConnectError as exc:
            raise ApiError(str(exc)) from exc

    def stream_file(self, key: str) -> Iterator[bytes]:
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers) as client:
                resp = client.get(f"/files/{key}")
                resp.raise_for_status()
                yield from resp.iter_bytes()
        except httpx.HTTPStatusError as exc:
            raise ApiError(str(exc)) from exc
        except httpx.ConnectError as exc:
            raise ApiError(str(exc)) from exc

    def upload_file(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers) as client:
                resp = client.put(f"/files/{key}", content=data, headers={"content-type": content_type})
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApiError(str(exc)) from exc
        except httpx.ConnectError as exc:
            raise ApiError(str(exc)) from exc

    def health(self) -> dict:
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers) as client:
                resp = client.get("/health")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ApiError(str(exc)) from exc
        except httpx.ConnectError as exc:
            raise ApiError(str(exc)) from exc
