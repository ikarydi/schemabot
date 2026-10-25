"""Embed-scope scan: which fields does the ingestion code send to an embedder?

This is a static, best-effort trace over Python source:

1. Find *sinks*: calls that embed text (``embed_documents``, ``embeddings.create``,
   ``add_texts``, ``Document(page_content=...)``, ...), plus module functions
   that pass one of their parameters on to a sink.
2. Collect the field references in each sink's arguments: ``row.body``,
   ``row["body"]``, ``row.get("body")``, ``f"{t.subject}"``, and the columns of
   a ``SELECT`` written in a string literal.
3. Follow local variables and module helper functions, so
   ``texts = [to_text(t) for t in rows]; embed_documents(texts)`` traces back to
   the fields ``to_text`` reads.

Only names that are known columns of the contract count, which keeps noise
down. When code is too dynamic to trace, declare ``rag.embedQuery`` instead.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .snapshot import Snapshot

# Method or function names whose arguments are embedded.
EMBED_CALLS = {
    "embed", "embed_documents", "embed_query", "embed_texts", "embed_text",
    "aembed_documents", "aembed_query", "embed_batch", "create_embeddings",
    "get_text_embedding", "get_text_embedding_batch", "get_query_embedding",
    "encode", "encode_documents", "from_texts", "afrom_texts", "add_texts",
    "aadd_texts", "upsert_texts",
}
# Constructors whose specific keywords hold the text that gets embedded.
DOCUMENT_TEXT_KWARGS = {
    "Document": {"page_content", "text"},
    "TextNode": {"text"},
    "Node": {"text"},
}
# Client calls whose ``input=`` (or first argument) is embedded: OpenAI, Voyage, Cohere style.
EMBED_ATTR_CHAINS = {("embeddings", "create"), ("embed", "create")}
_SINK_KWARGS = {None, "texts", "text", "documents", "input", "inputs", "query"}

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class EmbedRef:
    field: str
    file: str
    line: int
    sink: str


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _attr_chain_tail(func: ast.expr) -> tuple[str, str] | None:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
        return func.value.attr, func.attr
    return None


def _is_star(proj: exp.Expression) -> bool:
    return isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star))


def _walk_scope(body: list[ast.stmt]):
    """Walk statements without descending into nested function or class bodies."""
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(list(ast.iter_child_nodes(node))):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _assignments(body: list[ast.stmt]) -> list[tuple[list[str], ast.AST]]:
    """(target names, value) for every way a local variable can receive data."""
    out: list[tuple[list[str], ast.AST]] = []
    for node in _walk_scope(body):
        if isinstance(node, ast.Assign):
            targets = [t.id for tgt in node.targets for t in ast.walk(tgt) if isinstance(t, ast.Name)]
            out.append((targets, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            if isinstance(node.target, ast.Name):
                out.append(([node.target.id], node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            out.append(([t.id for t in ast.walk(node.target) if isinstance(t, ast.Name)], node.iter))
        elif isinstance(node, ast.NamedExpr):
            out.append(([node.target.id], node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names = [t.id for t in ast.walk(item.optional_vars) if isinstance(t, ast.Name)]
                    out.append((names, item.context_expr))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "extend", "add", "insert", "update", "write")
            and isinstance(node.func.value, ast.Name)
        ):
            # texts.append(row.body) taints ``texts``
            out.append(([node.func.value.id], node))
    return out


def _params(func: FunctionNode) -> list[str]:
    args = func.args
    return [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]


class _ModuleScan:
    def __init__(self, tree: ast.Module, fields: set[str], path: str) -> None:
        self.tree = tree
        self.fields = fields
        self.path = path
        self.functions: dict[str, FunctionNode] = {
            n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.module_body = [
            s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        self._returns: dict[str, set[str]] = {}
        self._in_progress: set[str] = set()
        self.param_sinks = self._find_param_sinks()

    # -- field references -------------------------------------------------

    def _direct_refs(self, node: ast.AST) -> tuple[set[str], set[str], set[str]]:
        """(fields, loaded names, called module functions) referenced under ``node``."""
        fields: set[str] = set()
        names: set[str] = set()
        calls: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr in self.fields:
                fields.add(sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value in self.fields:
                    # row["body"], row.get("body"), getattr(row, "body")
                    fields.add(sub.value)
                else:
                    fields |= self._sql_fields(sub.value)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                names.add(sub.id)
            if isinstance(sub, ast.Call) and _call_name(sub.func) in self.functions:
                calls.add(_call_name(sub.func))
        return fields, names, calls

    def _sql_fields(self, text: str) -> set[str]:
        if "select" not in text.lower():
            return set()
        try:
            parsed = sqlglot.parse_one(text, read="postgres")
        except (ParseError, TokenError, ValueError):
            return set()
        if not isinstance(parsed, exp.Select):
            return set()
        out: set[str] = set()
        for proj in parsed.expressions:
            if _is_star(proj):
                return set(self.fields)
            out |= {c.name for c in proj.find_all(exp.Column) if c.name in self.fields}
        return out

    # -- data flow --------------------------------------------------------

    def _taint(self, body: list[ast.stmt]) -> dict[str, set[str]]:
        """Map each local variable to the fields that may flow into it."""
        resolved = [(targets, *self._direct_refs(value)) for targets, value in _assignments(body)]
        taint: dict[str, set[str]] = {}
        changed = True
        while changed:
            changed = False
            for targets, fields, names, calls in resolved:
                flow = set(fields)
                for n in names:
                    flow |= taint.get(n, set())
                for c in calls:
                    flow |= self.returns_of(c)
                for target in targets:
                    current = taint.setdefault(target, set())
                    if not flow <= current:
                        current |= flow
                        changed = True
        return taint

    def _flow(self, node: ast.AST, taint: dict[str, set[str]]) -> set[str]:
        fields, names, calls = self._direct_refs(node)
        for n in names:
            fields |= taint.get(n, set())
        for c in calls:
            fields |= self.returns_of(c)
        return fields

    def returns_of(self, fname: str) -> set[str]:
        """Fields that may appear in the return value of module function ``fname``."""
        if fname in self._returns:
            return self._returns[fname]
        if fname in self._in_progress:
            return set()
        self._in_progress.add(fname)
        func = self.functions[fname]
        taint = self._taint(func.body)
        out: set[str] = set()
        for node in _walk_scope(func.body):
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and node.value is not None:
                out |= self._flow(node.value, taint)
        self._in_progress.discard(fname)
        self._returns[fname] = out
        return out

    # -- sinks ------------------------------------------------------------

    def _builtin_sink(self, call: ast.Call) -> tuple[str, list[ast.AST]] | None:
        name = _call_name(call.func)
        tail = _attr_chain_tail(call.func)
        if tail in EMBED_ATTR_CHAINS:
            args: list[ast.AST] = [kw.value for kw in call.keywords if kw.arg in ("input", "texts", "inputs")]
            return ".".join(tail), args or list(call.args[:1])
        if name in EMBED_CALLS:
            return name, list(call.args) + [kw.value for kw in call.keywords if kw.arg in _SINK_KWARGS]
        if name in DOCUMENT_TEXT_KWARGS:
            wanted = DOCUMENT_TEXT_KWARGS[name]
            args = [kw.value for kw in call.keywords if kw.arg in wanted]
            return name, args or list(call.args[:1])
        return None

    def _sink(self, call: ast.Call, param_sinks: dict[str, set[str]]) -> tuple[str, list[ast.AST]] | None:
        builtin = self._builtin_sink(call)
        if builtin is not None:
            return builtin
        name = _call_name(call.func)
        if name not in self.functions or not param_sinks.get(name):
            return None
        params = _params(self.functions[name])
        if isinstance(call.func, ast.Attribute) and params and params[0] in ("self", "cls"):
            params = params[1:]
        sink_params = param_sinks[name]
        args = [a for i, a in enumerate(call.args) if i < len(params) and params[i] in sink_params]
        args += [kw.value for kw in call.keywords if kw.arg in sink_params]
        return (f"{name}()", args) if args else None

    def _find_param_sinks(self) -> dict[str, set[str]]:
        """For each module function, the parameters that reach an embed sink."""
        sinks: dict[str, set[str]] = {name: set() for name in self.functions}
        deps = {name: _assignments(func.body) for name, func in self.functions.items()}
        changed = True
        while changed:
            changed = False
            for name, func in self.functions.items():
                params = set(_params(func))
                if not params:
                    continue
                # names each local variable depends on, transitively
                local: dict[str, set[str]] = {}
                inner = True
                while inner:
                    inner = False
                    for targets, value in deps[name]:
                        loaded = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
                        for t in list(loaded):
                            loaded |= local.get(t, set())
                        for target in targets:
                            current = local.setdefault(target, set())
                            if not loaded <= current:
                                current |= loaded
                                inner = True
                for node in _walk_scope(func.body):
                    if not isinstance(node, ast.Call):
                        continue
                    sink = self._sink(node, sinks)
                    if sink is None:
                        continue
                    for arg in sink[1]:
                        reached = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                        for n in list(reached):
                            reached |= local.get(n, set())
                        new = (reached & params) - sinks[name]
                        if new:
                            sinks[name] |= new
                            changed = True
        return sinks

    def scan(self) -> list[EmbedRef]:
        refs: set[EmbedRef] = set()
        scopes: list[list[ast.stmt]] = [self.module_body] + [f.body for f in self.functions.values()]
        for body in scopes:
            taint = self._taint(body)
            for node in _walk_scope(body):
                if not isinstance(node, ast.Call):
                    continue
                sink = self._sink(node, self.param_sinks)
                if sink is None:
                    continue
                sink_name, args = sink
                for arg in args:
                    for field in self._flow(arg, taint):
                        refs.add(EmbedRef(field, self.path, node.lineno, sink_name))
        return sorted(refs, key=lambda r: (r.file, r.line, r.field))


def scan_code(snapshot: Snapshot, paths: list[str], fields: set[str]) -> tuple[list[EmbedRef], list[str]]:
    """Scan the Python files under ``paths`` for contract fields that reach an embedder."""
    refs: list[EmbedRef] = []
    warnings: list[str] = []
    for location in paths:
        files = [f for f in snapshot.list_files(location) if f.endswith(".py")]
        if not files:
            warnings.append(f"rag.code path {location} has no Python files")
        for path in files:
            text = snapshot.read_text(path) or ""
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError as exc:
                warnings.append(f"{path}: could not parse ({exc.msg}, line {exc.lineno})")
                continue
            refs.extend(_ModuleScan(tree, fields, path).scan())
    return sorted(set(refs), key=lambda r: (r.file, r.line, r.field)), warnings


@dataclass(frozen=True)
class QueryScan:
    fields: frozenset[str]
    star: bool
    error: str | None = None


def scan_query(sql: str, dialect: str = "postgres") -> QueryScan:
    """Columns a declared ``rag.embedQuery`` selects."""
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except (ParseError, TokenError) as exc:
        return QueryScan(frozenset(), False, f"could not parse rag.embedQuery: {exc}")
    select = parsed if isinstance(parsed, exp.Select) else (parsed.find(exp.Select) if parsed else None)
    if select is None:
        return QueryScan(frozenset(), False, "rag.embedQuery is not a SELECT")
    out: set[str] = set()
    star = False
    for proj in select.expressions:
        if _is_star(proj):
            star = True
            continue
        out |= {c.name for c in proj.find_all(exp.Column)}
    return QueryScan(frozenset(out), star)
