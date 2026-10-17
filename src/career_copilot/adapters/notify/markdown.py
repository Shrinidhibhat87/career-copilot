"""Notifier that writes the digest to a Markdown file."""

from __future__ import annotations

from pathlib import Path


class MarkdownNotifier:
    """Writes the rendered digest. The title is metadata; the body is the file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def deliver(self, title: str, body: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(body, encoding="utf-8")
