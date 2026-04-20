"""GET /files/{key} — RBAC-authenticated blob streaming."""

from __future__ import annotations

import mimetypes
import urllib.parse
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from zdrovena.api.auth import Principal, require_viewer_or_above
from zdrovena.api.deps import StorageDep

router = APIRouter(prefix="/files", tags=["files"])


# NOTE: "" must be registered BEFORE "/{key:path}".
# Starlette's `path` converter can match an empty string, so if the download
# route comes first it will consume GET /files/ before the list route runs.


@router.get(
    "",
    summary="List files",
    responses={403: {"description": "Insufficient role"}},
    include_in_schema=True,
)
@router.get(
    "/",
    summary="List files (trailing slash)",
    responses={403: {"description": "Insufficient role"}},
    include_in_schema=False,
)
def list_files(
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    prefix: Annotated[str, Query(description="Key prefix filter")] = "",
) -> list[dict]:
    """List files in storage under an optional prefix."""
    return [
        {
            "key": f.key,
            "size": f.size,
            "last_modified": f.last_modified.isoformat(),
        }
        for f in storage.list_files(prefix)
    ]


@router.get(
    "/{key:path}",
    summary="Download a stored file",
    responses={
        200: {"description": "File stream"},
        400: {"description": "Invalid key"},
        403: {"description": "Insufficient role"},
        404: {"description": "File not found"},
    },
)
def download_file(
    key: str,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> StreamingResponse:
    """Stream a file from storage. Requires viewer role or above.

    ``key`` is the full storage path, e.g. ``invoices/sales/2026/03/faktura-001.pdf``.
    """
    normalised = urllib.parse.unquote(key)
    if not normalised or ".." in normalised or normalised.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid key")

    if not storage.exists(normalised):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Not found: {key!r}")

    media_type, _ = mimetypes.guess_type(key)
    filename = key.rsplit("/", 1)[-1]
    return StreamingResponse(
        storage.stream(normalised),
        media_type=media_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
