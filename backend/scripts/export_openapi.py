"""Dump the FastAPI OpenAPI schema to a file.

`app.openapi()` reflects route and Pydantic model definitions only; it never
runs the app's lifespan (where the DB/checkpointer connections happen), so
this is safe to run with no Postgres reachable. That is what makes it usable
as the frontend's type-generation input in CI without a database service.

Usage: uv run python scripts/export_openapi.py [output-path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    spec = app.openapi()
    out_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({len(spec['paths'])} paths)")  # noqa: T201


if __name__ == "__main__":
    main()
