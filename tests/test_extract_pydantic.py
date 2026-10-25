from schemabot.extract.pydantic_model import extract_model
from schemabot.snapshot import WorkingTree

SOURCE = '''
from typing import Annotated, ClassVar, Optional, Union
from pydantic import BaseModel, Field

class Base(BaseModel):
    id: str
    tenant_id: "UUID"

class Doc(Base):
    model_config = {"frozen": True}
    VERSION: ClassVar[int] = 1
    _cache: dict = {}
    title: Annotated[str, Field(max_length=200)]
    body: str | None = None
    score: Optional[float] = None
    meta: Union[dict, None] = None
    tags: list[str] = []
    when: "datetime"
    email: str = Field(alias="emailAddress")
'''


def test_fields_types_nullability_and_inheritance(tmp_path):
    (tmp_path / "models.py").write_text(SOURCE)
    schema = extract_model(WorkingTree(tmp_path), "models.py", "Doc").schema
    cols = schema.columns
    assert list(cols) == ["id", "tenant_id", "title", "body", "score", "meta", "tags", "when", "emailAddress"]
    assert cols["title"].type == "string" and not cols["title"].nullable
    assert cols["body"].nullable and cols["score"].nullable and cols["meta"].nullable
    assert cols["score"].type == "float" and cols["meta"].type == "json"
    assert cols["tags"].type == "array"
    assert cols["when"].type == "timestamp"
    assert cols["tenant_id"].type == "uuid"


def test_model_not_found(tmp_path):
    (tmp_path / "models.py").write_text("class Other: pass\n")
    assert extract_model(WorkingTree(tmp_path), "models.py", "Doc").schema is None


def test_syntax_error_is_a_warning(tmp_path):
    (tmp_path / "models.py").write_text("class Doc(:\n")
    result = extract_model(WorkingTree(tmp_path), "models.py", "Doc")
    assert result.schema is None and result.warnings
