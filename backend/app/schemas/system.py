from pydantic import BaseModel


class ModelInfoResponse(BaseModel):
    provider: str
    model: str
