from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SourceReference(BaseModel):
    id: UUID
    chunk_id: UUID | None = None
    document_id: UUID
    document_name: str
    location: str
    excerpt: str
    similarity: float
    citation_label: str | None = None
    claims: list[str] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    workspace_id: UUID
    title: str = Field(default="New conversation", min_length=1, max_length=200)
    chat_type: Literal["normal", "knowledge"] = "normal"
    document_ids: list[UUID] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be blank.")
        return value


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be blank.")
        return value


class ConversationDocumentsUpdate(BaseModel):
    document_ids: list[UUID]


class ConversationResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str
    chat_type: Literal["normal", "knowledge"]
    document_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime
    sources: list[SourceReference] = Field(default_factory=list)
    status: str = "completed"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    workspace_id: UUID | None = None


class StreamChatRequest(ChatRequest):
    request_id: UUID


class ChatResponse(BaseModel):
    reply: str
    model: str
    conversation_id: UUID
    grounded: bool = False
    sources: list[SourceReference] = Field(default_factory=list)
