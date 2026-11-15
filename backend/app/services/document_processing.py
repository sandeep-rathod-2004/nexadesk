import codecs
import re
import unicodedata
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from docx import Document as WordDocument
from pypdf import PdfReader


class InvalidDocumentError(Exception):
    def __init__(self, message: str, status_code: int = 415):
        super().__init__(message)
        self.status_code = status_code


class DocumentProcessingError(Exception):
    pass


@dataclass(frozen=True)
class ExtractedSection:
    text: str
    source_ref: str


@dataclass(frozen=True)
class ProcessedChunk:
    content: str
    source_refs: list[str]


def safe_display_filename(filename: str | None) -> str:
    normalized = (filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
    normalized = "".join(
        character for character in normalized
        if character.isprintable() and character not in {"/", "\\"}
    ).strip()
    return normalized[:255] or "document"


def allowed_extension(filename: str | None) -> str:
    suffix = Path(safe_display_filename(filename)).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        raise InvalidDocumentError("Upload a PDF, DOCX, or TXT file.")
    return suffix


DocumentSource = Path | BinaryIO


def _read_prefix(source: DocumentSource, size: int) -> bytes:
    if isinstance(source, Path):
        with source.open("rb") as file_object:
            return file_object.read(size)
    source.seek(0)
    prefix = source.read(size)
    source.seek(0)
    return prefix


def validate_file_content(path: DocumentSource, extension: str) -> str:
    try:
        prefix = _read_prefix(path, 8)
    except OSError as exc:
        raise DocumentProcessingError("The uploaded file could not be read.") from exc

    if extension == ".pdf":
        if not prefix.startswith(b"%PDF-"):
            raise InvalidDocumentError("The file contents are not a valid PDF.")
        return "application/pdf"
    if extension == ".docx":
        if not prefix.startswith(b"PK"):
            raise InvalidDocumentError("The file contents are not a valid DOCX document.")
        try:
            archive_context = path.open("rb") if isinstance(path, Path) else nullcontext(path)
            with archive_context as archive_source:
                with zipfile.ZipFile(archive_source) as archive:
                    entries = archive.infolist()
                    if (
                        len(entries) > 2000
                        or sum(entry.file_size for entry in entries) > 100 * 1024 * 1024
                        or "[Content_Types].xml" not in archive.namelist()
                        or "word/document.xml" not in archive.namelist()
                    ):
                        raise InvalidDocumentError("The DOCX file structure is invalid or too large.")
        except zipfile.BadZipFile as exc:
            raise InvalidDocumentError("The file contents are not a valid DOCX document.") from exc
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    try:
        source_context = path.open("rb") if isinstance(path, Path) else nullcontext(path)
        with source_context as source:
            _validate_text_stream(source)
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("TXT files must use UTF-8 encoding.") from exc
    except OSError as exc:
        raise DocumentProcessingError("The uploaded file could not be read.") from exc
    return "text/plain"


def _validate_text_stream(source: BinaryIO) -> None:
    source.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    while block := source.read(64 * 1024):
        if b"\x00" in block:
            raise InvalidDocumentError("The TXT file contains binary data.")
        decoder.decode(block)
    decoder.decode(b"", final=True)
    source.seek(0)


def clean_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = "".join(
        char for char in normalized
        if char in "\n\t" or unicodedata.category(char) != "Cc"
    )
    normalized = re.sub(r"[^\S\n]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def extract_document(
    path: DocumentSource, extension: str, max_pdf_pages: int
) -> list[ExtractedSection]:
    try:
        source: str | BufferedIOBase = str(path) if isinstance(path, Path) else path
        if extension == ".pdf":
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise DocumentProcessingError("Encrypted PDFs are not supported.")
            if len(reader.pages) > max_pdf_pages:
                raise DocumentProcessingError(
                    f"PDF exceeds the {max_pdf_pages}-page processing limit."
                )
            sections = [
                ExtractedSection(clean_text(page.extract_text() or ""), f"Page {index}")
                for index, page in enumerate(reader.pages, start=1)
            ]
        elif extension == ".docx":
            document = WordDocument(source)
            sections = []
            paragraph_number = 0
            for paragraph in document.paragraphs:
                paragraph_number += 1
                sections.append(
                    ExtractedSection(clean_text(paragraph.text), f"Paragraph {paragraph_number}")
                )
            for table_number, table in enumerate(document.tables, start=1):
                for row_number, row in enumerate(table.rows, start=1):
                    row_text = " | ".join(cell.text for cell in row.cells)
                    sections.append(
                        ExtractedSection(
                            clean_text(row_text), f"Table {table_number}, row {row_number}"
                        )
                    )
        else:
            text = (
                path.read_text(encoding="utf-8-sig")
                if isinstance(path, Path)
                else path.read().decode("utf-8-sig")
            )
            sections = [ExtractedSection(clean_text(text), "Text file")]
    except DocumentProcessingError:
        raise
    except Exception as exc:
        raise DocumentProcessingError(
            "The file could not be parsed. Check that it is not damaged or password-protected."
        ) from exc

    sections = [section for section in sections if section.text]
    if not sections:
        raise DocumentProcessingError("No readable text was found in the document.")
    return sections


def chunk_sections(
    sections: list[ExtractedSection], chunk_size: int, chunk_overlap: int
) -> list[ProcessedChunk]:
    content_parts: list[str] = []
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for section in sections:
        if not section.text:
            continue
        if content_parts:
            content_parts.append("\n")
            cursor += 1
        start = cursor
        content_parts.append(section.text)
        cursor += len(section.text)
        ranges.append((start, cursor, section.source_ref))

    content = "".join(content_parts)
    if not content:
        return []

    chunks: list[ProcessedChunk] = []
    start = 0
    while start < len(content):
        end = min(start + chunk_size, len(content))
        if end < len(content):
            boundary_floor = start + chunk_size // 2
            newline = content.rfind("\n", boundary_floor, end)
            space = content.rfind(" ", boundary_floor, end)
            boundary = max(newline, space)
            if boundary > start:
                end = boundary

        text = content[start:end].strip()
        if text:
            source_refs = list(dict.fromkeys(
                source_ref
                for section_start, section_end, source_ref in ranges
                if section_start < end and section_end > start
            ))
            chunks.append(ProcessedChunk(content=text, source_refs=source_refs))
        if end >= len(content):
            break
        next_start = max(start + 1, end - chunk_overlap)
        start = next_start
    return chunks
