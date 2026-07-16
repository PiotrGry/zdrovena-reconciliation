#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema deterministically."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    os.environ.setdefault("APP_ENV", "development")
    os.environ.setdefault("AZURE_AUTH_DISABLED", "true")
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from zdrovena.api.main import app

    schema = app.openapi()
    out = Path("contracts/openapi.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
