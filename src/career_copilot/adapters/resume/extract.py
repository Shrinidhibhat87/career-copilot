"""Resume text extraction. PDF via pypdf, DOCX via the stdlib zip/XML pair."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeExtractionError(RuntimeError):
    """The document opened but no usable text came out of it."""


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    try:
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except (FileNotDecryptedError, DependencyError) as error:
        raise ResumeExtractionError("the PDF is encrypted") from error


def _docx_text(data: bytes) -> str:
    # ponytail: sweeps every <w:t> in the main document part, which also picks up
    # table cells that python-docx's paragraph walk misses. Ceiling: headers,
    # footers, and text boxes live in other parts and are not read. Upgrade path
    # is to sweep every word/*.xml part the same way.
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            document = archive.read("word/document.xml")
    except KeyError as error:
        raise ResumeExtractionError("the DOCX has no main document part") from error
    root = ElementTree.fromstring(document)
    return "\n".join(node.text or "" for node in root.iter(f"{_WORD_NS}t"))


def extract(filename: str, data: bytes) -> str:
    """Return the plain text of a PDF or DOCX resume.

    Raises `ResumeExtractionError` when the file opens but yields nothing usable.
    Anything else it raises is a genuine failure and is reported as one.
    """
    suffix = Path(filename).suffix.lower()
    text = _pdf_text(data) if suffix == ".pdf" else _docx_text(data)
    if not text.strip():
        raise ResumeExtractionError("no text could be read from the file")
    return text
