#!/usr/bin/env python
"""List the classified routes an API reference does not mention (projects §9.6).

    cd backend && uv run python scripts/check_api_docs.py              # docs/api.md
    uv run python scripts/check_api_docs.py ../docs/api.zh-CN.md       # any reference

Every `(method, path)` in `ROUTE_POLICY` should appear somewhere in the reference:
as a table row (`| `GET` | `/api/...` | ...`, `…/suffix` cells counting against the
row's first path) or inline (`POST /api/...`, `PATCH/DELETE /api/...`). Paths are
compared with their parameter names and query strings dropped, so `{id}` matches
`{agent_id}`. Prints the missing routes with their role, one per line.

Exit code: 0 when every route is mentioned, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_METHOD_ALT = "|".join(METHODS)
_ROW = re.compile(rf"^\|\s*`((?:{_METHOD_ALT})(?:\s*/\s*(?:{_METHOD_ALT}))*)`\s*\|(.*)$", re.M)
_CELL_PATH = re.compile(r"`(/(?:api|v1|share)(?:/[^`\s]*)?)`")
_CELL_SUFFIX = re.compile(r"`…(/[^`\s]*)`")
_INLINE = re.compile(
    rf"\b((?:{_METHOD_ALT})(?:/(?:{_METHOD_ALT}))*)\s+`?(/(?:api|v1|share)[^\s`,)|]*)"
)
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def normalize(path: str) -> str:
    """`/api/agents/{agent_id}?x=1` -> `/api/agents/{}`."""
    path = path.split("?", 1)[0].rstrip("`.;:").rstrip("/") or "/"
    return re.sub(r"\{[^}]*\}", "{}", path)


def mentioned_routes(text: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for match in _ROW.finditer(text):
        methods = re.split(r"\s*/\s*", match.group(1))
        # the path cell ends at the first unescaped pipe (`up\|down` stays inside)
        cell = _UNESCAPED_PIPE.split(match.group(2), 1)[0]
        paths = _CELL_PATH.findall(cell)
        for path in paths:
            found.update((method, normalize(path)) for method in methods)
        if paths:
            parent = normalize(paths[0]).rsplit("/", 1)[0]
            for suffix in _CELL_SUFFIX.findall(cell):
                found.update((method, parent + normalize(suffix)) for method in methods)
    for match in _INLINE.finditer(text):
        for method in match.group(1).split("/"):
            found.add((method, normalize(match.group(2))))
    return found


def missing_routes(text: str, policy: dict[tuple[str, str], str]) -> list[tuple[str, str]]:
    have = mentioned_routes(text)
    return sorted(
        (key for key in policy if (key[0], normalize(key[1])) not in have),
        key=lambda key: (key[1], key[0]),
    )


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import app.main  # noqa: F401 — routers append their route-policy blocks on import
    from app.core.route_policy import ROUTE_POLICY

    doc = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[2] / "docs/api.md"
    missing = missing_routes(doc.read_text(encoding="utf-8"), ROUTE_POLICY)
    for method, path in missing:
        print(f"{method} {path} {ROUTE_POLICY[(method, path)]}")
    print(f"{len(missing)} of {len(ROUTE_POLICY)} routes not mentioned in {doc}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
