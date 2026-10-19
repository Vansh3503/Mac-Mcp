"""Spotlight file search through ``mdfind``.

The query is always passed as a **single argv element**; nothing is ever interpolated into a
shell. Without ``kind`` the text goes to mdfind unchanged, exactly as if typed into Spotlight.
With ``kind`` AXPilot builds a raw metadata query expression: the kind becomes a
``kMDItemContentTypeTree`` test and the text is escaped into quoted string values (``\\``, ``"``
and the ``*``/``?`` wildcards), so it can never change the structure of the expression.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandRunner, ensure_success, output_records

__all__ = [
    "MAX_LIMIT",
    "SPOTLIGHT_KINDS",
    "build_spotlight_query",
    "normalize_kind",
    "spotlight_argv",
    "spotlight_search",
]

_log = logging.getLogger(__name__)

SPOTLIGHT_KINDS: dict[str, tuple[str, ...]] = {
    "app": ("com.apple.application-bundle",),
    "pdf": ("com.adobe.pdf",),
    "image": ("public.image",),
    "folder": ("public.folder",),
    "document": ("public.text", "public.composite-content", "public.presentation", "public.spreadsheet"),
    "text": ("public.text",),
    "audio": ("public.audio",),
    "movie": ("public.movie",),
    "presentation": ("public.presentation",),
    "spreadsheet": ("public.spreadsheet",),
    "archive": ("public.archive",),
    "code": ("public.source-code",),
    "email": ("com.apple.mail.emlx",),
    "font": ("public.font",),
}
"""Kind name → Uniform Type Identifiers matched against ``kMDItemContentTypeTree``."""

_KIND_ALIASES: dict[str, str] = {
    "application": "app",
    "apps": "app",
    "applications": "app",
    "pdfs": "pdf",
    "images": "image",
    "photo": "image",
    "photos": "image",
    "picture": "image",
    "folders": "folder",
    "directory": "folder",
    "documents": "document",
    "doc": "document",
    "docs": "document",
    "music": "audio",
    "sound": "audio",
    "video": "movie",
    "videos": "movie",
    "movies": "movie",
    "slides": "presentation",
    "zip": "archive",
    "source": "code",
    "mail": "email",
    "fonts": "font",
}

MAX_LIMIT = 1000
MAX_QUERY_CHARS = 1000
DEFAULT_TIMEOUT_S = 15.0
_MAX_PATH_CHARS = 1025  # PATH_MAX (1024) plus the separator
_MAX_OUTPUT_CAP = 2_000_000


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def normalize_kind(kind: str) -> str:
    """Map a kind or alias (``"images"``, ``"application"``) to a key of :data:`SPOTLIGHT_KINDS`."""
    key = kind.strip().lower() if isinstance(kind, str) else ""
    key = _KIND_ALIASES.get(key, key)
    if key not in SPOTLIGHT_KINDS:
        raise _invalid(
            f"Unknown kind {kind!r}.",
            hint=f"Use one of: {', '.join(SPOTLIGHT_KINDS)}.",
        )
    return key


def _query_value(text: str) -> str:
    """Escape ``text`` for use inside a double-quoted metadata query string."""
    out = []
    for ch in text:
        if ch in '\\"*?':
            out.append("\\")
        out.append(ch)
    return "".join(out)


def build_spotlight_query(query: str, *, kind: str | None = None) -> str:
    """The mdfind query for ``query`` (and an optional ``kind``).

    Without ``kind``, the stripped query is returned unchanged. With ``kind``, a raw query
    expression is returned: ``(<content type tests>) && (<name or content matches the text>)``,
    or just the content type tests when ``query`` is empty.
    """
    if not isinstance(query, str):
        raise _invalid("The search query must be text.")
    text = query.strip()
    if len(text) > MAX_QUERY_CHARS:
        raise _invalid(f"The search query is too long ({len(text)} characters, max {MAX_QUERY_CHARS}).")
    if "\x00" in text:
        raise _invalid("The search query cannot contain NUL characters.")
    if kind is None:
        if not text:
            raise _invalid("The search query is empty.", hint="Pass some text, or a kind to list.")
        if text.startswith("-"):
            raise _invalid(
                "The search query cannot start with '-' (mdfind would read it as an option).",
                hint="Rephrase the query, or pass a kind to search names and contents literally.",
            )
        return text
    utis = SPOTLIGHT_KINDS[normalize_kind(kind)]
    kind_expr = " || ".join(f'kMDItemContentTypeTree == "{uti}"' for uti in utis)
    if not text:
        return f"({kind_expr})"
    value = _query_value(text)
    text_expr = f'kMDItemDisplayName == "*{value}*"cd || kMDItemTextContent == "{value}"cdw'
    return f"({kind_expr}) && ({text_expr})"


def _resolve_scope(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip() or "\x00" in scope:
        raise _invalid("scope must be a folder path.")
    path = Path(scope.strip()).expanduser()
    if not path.is_absolute():
        raise _invalid(
            f"scope must be an absolute path or start with ~, got {scope!r}.",
            hint="For example ~/Documents or /Applications.",
        )
    if not path.is_dir():
        raise _invalid(f"The search folder does not exist: {path}")
    return str(path)


def spotlight_argv(expression: str, *, scope: str | None = None) -> list[str]:
    """``mdfind -0 [-onlyin <scope>] <expression>`` (NUL-separated output)."""
    argv = ["mdfind", "-0"]
    if scope is not None:
        argv += ["-onlyin", _resolve_scope(scope)]
    argv.append(expression)
    return argv


def _parse_paths(stdout: str) -> list[str]:
    """Absolute paths from NUL-separated mdfind output (a stray newline between records is ignored)."""
    paths = (record.lstrip("\n") for record in output_records(stdout, "\x00"))
    return [path for path in paths if path.startswith("/")]


def spotlight_search(
    runner: CommandRunner,
    query: str,
    *,
    kind: str | None = None,
    scope: str | None = None,
    limit: int = 20,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[str]:
    """Search Spotlight and return up to ``limit`` absolute paths, in mdfind's order.

    ``kind`` filters by type (see :data:`SPOTLIGHT_KINDS`; aliases such as ``"images"`` work),
    ``scope`` restricts the search to a folder (``-onlyin``; ``~`` is expanded). With a kind,
    ``query`` may be empty to list every item of that kind. If mdfind runs past ``timeout_s``,
    the paths found so far are returned, or ``TIMEOUT`` is raised when there are none.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _invalid(f"limit must be an integer between 1 and {MAX_LIMIT}, got {limit!r}.")
    expression = build_spotlight_query(query, kind=kind)
    argv = spotlight_argv(expression, scope=scope)
    max_output = min(limit * _MAX_PATH_CHARS + 1024, _MAX_OUTPUT_CAP)
    result = runner.run(argv, timeout_s=timeout_s, max_output=max_output)

    if "failed to create query" in result.stderr.lower():
        raise _invalid(
            "Spotlight could not parse the query.",
            hint="Use plain words, or pass a kind so AXPilot builds the query for you.",
        )
    if result.timed_out:
        paths = _parse_paths(result.stdout)[:limit]
        if paths:
            _log.info("mdfind timed out after %g s; returning %d partial results", timeout_s, len(paths))
            return paths
    ensure_success(
        result,
        what="Spotlight search (mdfind)",
        timeout_s=timeout_s,
        hint="Spotlight may be indexing. Retry, or narrow the search with kind or scope.",
    )
    return _parse_paths(result.stdout)[:limit]
