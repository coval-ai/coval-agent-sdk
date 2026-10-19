"""Preserve primary exceptions while retaining secondary cleanup diagnostics."""

from __future__ import annotations


def add_cleanup_note(exception: BaseException, note: str) -> None:
    """Attach secondary cleanup context where the runtime supports notes.

    Args:
        exception: Primary failure whose identity must be preserved.
        note: Diagnostic describing a secondary cleanup failure.
    """
    add_note = getattr(exception, "add_note", None)
    if callable(add_note):
        add_note(note)
