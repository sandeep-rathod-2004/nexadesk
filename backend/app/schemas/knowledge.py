from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class KnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    status: Literal["processing", "ready", "failed"]
    error_message: str | None
    created_at: datetime
    updated_at: datetime
