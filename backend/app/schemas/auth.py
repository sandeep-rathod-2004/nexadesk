from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator


class RegisterRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)
    workspace_name: str = Field(default="My Workspace", min_length=1, max_length=100)

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Full name cannot be blank.")
        return value

    @field_validator("workspace_name")
    @classmethod
    def strip_workspace_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name cannot be blank.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str | None = None


class WorkspaceAccess(BaseModel):
    id: UUID
    name: str
    role: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    workspaces: list[WorkspaceAccess]


class RegistrationResponse(BaseModel):
    user: UserResponse
    workspaces: list[WorkspaceAccess]
