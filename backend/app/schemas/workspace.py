from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name cannot be blank.")
        return value


class WorkspaceMemberCreate(BaseModel):
    email: EmailStr


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    role: str


class WorkspaceMemberResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    role: str
