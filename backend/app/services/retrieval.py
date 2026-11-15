import json
import math
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeChunk, KnowledgeDocument


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_name: str
    content: str
    source_refs: list[str]
    similarity: float


class EmbeddingCompatibilityError(Exception):
    pass


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def retrieve_chunks(
    db: Session,
    workspace_id: UUID,
    query_embedding: list[float],
    limit: int,
    min_similarity: float,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[RetrievedChunk]:
    rows = db.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(
            KnowledgeDocument,
            (KnowledgeChunk.document_id == KnowledgeDocument.id)
            & (KnowledgeChunk.workspace_id == KnowledgeDocument.workspace_id),
        )
        .where(
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeDocument.workspace_id == workspace_id,
            KnowledgeDocument.status == "ready",
        )
    )
    matches: list[RetrievedChunk] = []
    for chunk, document in rows:
        if embedding_provider is not None:
            if document.embedding_provider is None:
                if embedding_provider != "ollama":
                    raise EmbeddingCompatibilityError(
                        "Legacy workspace embeddings must be re-created before changing embedding providers."
                    )
            elif (
                document.embedding_provider != embedding_provider
                or document.embedding_model != embedding_model
            ):
                raise EmbeddingCompatibilityError(
                    "Workspace embeddings use a different provider or model."
                )
            if (
                document.embedding_dimensions is not None
                and document.embedding_dimensions != len(query_embedding)
            ):
                raise EmbeddingCompatibilityError(
                    "Workspace embeddings use a different vector dimension."
                )
        similarity = _cosine_similarity(query_embedding, chunk.embedding)
        if similarity < min_similarity:
            continue
        matches.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=document.id,
                document_name=document.filename,
                content=chunk.content,
                source_refs=chunk.source_refs,
                similarity=similarity,
            )
        )
    matches.sort(key=lambda item: (-item.similarity, str(item.chunk_id)))
    unique_matches: list[RetrievedChunk] = []
    seen_content: set[tuple[UUID, str]] = set()
    for match in matches:
        normalized_content = " ".join(match.content.split()).casefold()
        identity = (match.document_id, normalized_content)
        if identity in seen_content:
            continue
        seen_content.add(identity)
        unique_matches.append(match)
        if len(unique_matches) >= limit:
            break
    return unique_matches


def build_rag_context(chunks: list[RetrievedChunk]) -> tuple[str, list[dict[str, object]]]:
    excerpts: list[str] = []
    sources: list[dict[str, object]] = []
    for number, chunk in enumerate(chunks, start=1):
        location = ", ".join(chunk.source_refs) or "Document"
        excerpts.append(
            json.dumps(
                {"label": f"[{number}]", "document": chunk.document_name,
                 "location": location, "text": chunk.content},
                ensure_ascii=False,
            )
        )
        sources.append(
            {
                "id": chunk.chunk_id,
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "document_name": chunk.document_name,
                "location": location,
                "excerpt": chunk.content,
                "similarity": round(chunk.similarity, 4),
                "citation_label": f"[{number}]",
                "claims": [],
            }
        )
    return "\n".join(excerpts), sources


_CITATION_PATTERN = re.compile(r"\[(\d+)\]")
_ABSTENTION_PATTERNS = (
    re.compile(
        r"^(?:the )?(?:available |provided |retrieved )?documents? "
        r"(?:do not|don't|does not|doesn't) contain enough information\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^i (?:could not|couldn't|cannot|can't) find enough information "
        r"(?:in|from) the (?:available )?documents?\b",
        re.IGNORECASE,
    ),
)


def _is_abstention(reply: str) -> bool:
    without_citations = _CITATION_PATTERN.sub("", reply).strip()
    return any(pattern.search(without_citations) for pattern in _ABSTENTION_PATTERNS)


def _remove_invalid_spacing(reply: str) -> str:
    return re.sub(r"[ \t]+([.,!?;:])", r"\1", reply)


def _citation_number(match: re.Match[str]) -> int | None:
    digits = match.group(1)
    if len(digits) > 3:
        return None
    return int(digits)


def associate_citations(
    reply: str,
    sources: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    if _is_abstention(reply):
        return _remove_invalid_spacing(
            _CITATION_PATTERN.sub("", reply)
        ).strip(), []

    by_label = {
        int(str(source["citation_label"])[1:-1]): source
        for source in sources
        if isinstance(source.get("citation_label"), str)
        and re.fullmatch(r"\[\d{1,3}\]", str(source["citation_label"]))
    }
    used_labels = {
        label
        for match in _CITATION_PATTERN.finditer(reply)
        if (label := _citation_number(match)) is not None and label in by_label
    }
    sanitized = _CITATION_PATTERN.sub(
        lambda match: (
            match.group(0)
            if _citation_number(match) in used_labels
            else ""
        ),
        reply,
    )
    sanitized = _remove_invalid_spacing(sanitized)
    claims_by_label: dict[int, list[str]] = {}
    for match in _CITATION_PATTERN.finditer(sanitized):
        label = int(match.group(1))
        boundary_start = max(
            sanitized.rfind("\n", 0, match.start()),
            sanitized.rfind(".", 0, match.start()),
            sanitized.rfind("!", 0, match.start()),
            sanitized.rfind("?", 0, match.start()),
        ) + 1
        boundary_end = len(sanitized)
        for delimiter in ("\n", ".", "!", "?"):
            found = sanitized.find(delimiter, match.end())
            if found >= 0:
                boundary_end = min(
                    boundary_end, found + (1 if delimiter != "\n" else 0)
                )
        claim = sanitized[boundary_start:boundary_end].strip()
        if claim:
            claims = claims_by_label.setdefault(label, [])
            if claim not in claims:
                claims.append(claim)

    associated = []
    for label in sorted(used_labels):
        source = dict(by_label[label])
        source["claims"] = claims_by_label.get(label, [])
        associated.append(source)
    return sanitized, associated


def validate_citations(
    db: Session,
    workspace_id: UUID,
    reply: str,
    sources: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    def parse_chunk_id(source: dict[str, object]) -> UUID | None:
        raw_id = source.get("chunk_id") or source.get("id")
        if not isinstance(raw_id, (str, UUID)):
            return None
        try:
            return UUID(str(raw_id))
        except ValueError:
            return None

    chunk_ids = {
        chunk_id
        for source in sources
        if (chunk_id := parse_chunk_id(source)) is not None
    }
    valid_chunks: dict[UUID, tuple[KnowledgeChunk, KnowledgeDocument]] = {}
    if chunk_ids:
        rows = db.execute(
            select(KnowledgeChunk, KnowledgeDocument)
            .join(
                KnowledgeDocument,
                (KnowledgeChunk.document_id == KnowledgeDocument.id)
                & (KnowledgeChunk.workspace_id == KnowledgeDocument.workspace_id),
            )
            .where(
                KnowledgeChunk.id.in_(chunk_ids),
                KnowledgeChunk.workspace_id == workspace_id,
                KnowledgeDocument.workspace_id == workspace_id,
                KnowledgeDocument.status == "ready",
            )
        )
        valid_chunks = {
            chunk.id: (chunk, document) for chunk, document in rows
        }

    authorized_sources = []
    for index, source in enumerate(sources, start=1):
        chunk_id = parse_chunk_id(source)
        if chunk_id is None:
            continue
        chunk, document = valid_chunks.get(chunk_id, (None, None))
        if chunk is None or document is None:
            continue
        label = source.get("citation_label")
        if not isinstance(label, str):
            label = f"[{index}]"
        authorized_sources.append(
            {
                "id": chunk.id,
                "chunk_id": chunk.id,
                "document_id": document.id,
                "document_name": document.filename,
                "location": ", ".join(chunk.source_refs) or "Document",
                "excerpt": chunk.content,
                "similarity": source.get("similarity", 0.0),
                "citation_label": label,
                "claims": source.get("claims", []),
            }
        )
    return associate_citations(reply, authorized_sources)
