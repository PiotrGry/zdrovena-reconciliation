"""Transport helpers shared by provider emulators.

Only HTTP-level mechanics live here. Provider validation remains in each
provider module so one fake cannot accidentally inherit another API's rules.
"""

from __future__ import annotations

import io
import os
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from pypdf import PdfWriter


def _blank_label_pdf() -> bytes:
    """One valid, empty A6-ish page.

    The portal runs every label through pypdf to merge parcels and set the
    document title. A hand-written byte string does not parse, so the emulator
    would only ever exercise the fallback path.
    """
    writer = PdfWriter()
    writer.add_blank_page(width=298, height=420)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


PDF_BYTES = _blank_label_pdf()


async def form_body(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def require_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not authorization.removeprefix("Bearer ").strip():
        raise HTTPException(status_code=401, detail="Bearer token required")


def require_basic(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Basic auth required")


def require_fields(body: dict[str, Any], fields: tuple[str, ...], path: str = "") -> None:
    missing = [field for field in fields if field not in body]
    if missing:
        prefix = f"{path}." if path else ""
        raise HTTPException(
            status_code=422,
            detail=f"Missing required fields: {', '.join(prefix + field for field in missing)}",
        )


def require_non_empty(body: dict[str, Any], fields: tuple[str, ...], path: str) -> None:
    missing = [
        field
        for field in fields
        if body.get(field) is None
        or (isinstance(body.get(field), str) and not str(body[field]).strip())
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required fields: {', '.join(f'{path}.{field}' for field in missing)}",
        )


def apply_http_scenario(provider: str, operation: str, mode: str | None) -> None:
    if mode in {"validation_error", "400"}:
        raise HTTPException(status_code=400, detail=f"{provider} {operation} validation error")
    if mode == "422":
        raise HTTPException(status_code=422, detail=f"{provider} {operation} validation error")
    if mode in {"server_error", "500"}:
        raise HTTPException(status_code=500, detail=f"{provider} {operation} server error")
    if mode == "timeout":
        time.sleep(float(os.environ.get("FAKE_PROVIDER_TIMEOUT_SECONDS", "2")))
