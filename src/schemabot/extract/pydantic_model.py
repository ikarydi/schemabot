"""Read a Pydantic (or dataclass-style) model's fields statically with ``ast``.

User code is never imported: the base revision of a pull request is not
installed, and importing arbitrary code in CI is a needless risk.
"""

from __future__ import annotations

import ast

from ..snapshot import Snapshot
from ..types import ARRAY, UNKNOWN, normalize_type
from . import Column, Extraction, TableSchema

_WRAPPERS = {"Annotated", "Required", "NotRequired", "Final", "ReadOnly"}
_OPTIONAL = {"Optional"}
_UNION = {"Union"}
_SKIP_NAMES = {"model_config", "Config"}


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _is_none(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or (
        isinstance(node, ast.Name) and node.id == "None"
    )


def _resolve(node: ast.expr) -> tuple[str, bool]:
    """Return (raw type name, nullable) for an annotation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _resolve(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return node.value, False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        members = _flatten_union(node)
        return _union(members)
    if isinstance(node, ast.Subscript):
        outer = _name(node.value)
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if outer in _WRAPPERS:
            return _resolve(args[0])
        if outer in _OPTIONAL:
            raw, _ = _resolve(args[0])
            return raw, True
        if outer in _UNION:
            return _union(list(args))
        if outer == "Literal":
            return "literal", False
        return outer, False
    return _name(node), False


def _flatten_union(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flatten_union(node.left) + _flatten_union(node.right)
    return [node]


def _union(members: list[ast.expr]) -> tuple[str, bool]:
    nullable = any(_is_none(m) for m in members)
    rest = [m for m in members if not _is_none(m)]
    if len(rest) == 1:
        raw, inner_nullable = _resolve(rest[0])
        return raw, nullable or inner_nullable
    return "union", nullable


def _alias(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Call) and _name(value.func) == "Field":
        for kw in value.keywords:
            if kw.arg in ("alias", "validation_alias") and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    return kw.value.value
    return None


def _class_fields(
    classes: dict[str, ast.ClassDef], name: str, seen: set[str]
) -> dict[str, Column]:
    cls = classes[name]
    columns: dict[str, Column] = {}
    seen.add(name)
    for base in cls.bases:
        base_name = _name(base)
        if base_name in classes and base_name not in seen:
            columns.update(_class_fields(classes, base_name, seen))
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        field_name = stmt.target.id
        if field_name.startswith("_") or field_name in _SKIP_NAMES:
            continue
        if _name(stmt.annotation) == "ClassVar" or (
            isinstance(stmt.annotation, ast.Subscript) and _name(stmt.annotation.value) == "ClassVar"
        ):
            continue
        raw, nullable = _resolve(stmt.annotation)
        norm = normalize_type(raw)
        if norm == UNKNOWN and raw in ("List", "Sequence", "Set", "Tuple"):
            norm = ARRAY
        key = _alias(stmt.value) or field_name
        columns[key] = Column(name=key, type=norm, raw_type=ast.unparse(stmt.annotation), nullable=nullable)
    return columns


def extract_model(snapshot: Snapshot, location: str, model: str) -> Extraction:
    """Find class ``model`` in the ``.py`` file(s) at ``location`` and return its fields."""
    files = [f for f in snapshot.list_files(location) if f.endswith(".py")]
    if not files:
        return Extraction(None, [f"no .py files found at {location}"])
    warnings: list[str] = []
    for path in files:
        text = snapshot.read_text(path) or ""
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            warnings.append(f"{path}: could not parse ({exc.msg}, line {exc.lineno})")
            continue
        classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        if model in classes:
            columns = _class_fields(classes, model, set())
            return Extraction(TableSchema(name=model, columns=columns, origin=path), warnings)
    return Extraction(None, warnings)
