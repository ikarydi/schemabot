from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class KBArticle(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str
    content: str
    author_email: Optional[str] = None
    tags: list[str] = []
    published_at: datetime
