"""Which model providers does pipeline code call, where, and does it start training?

A static, best-effort scan over Python source, like the embed-scope scan. It
recognises provider clients by their constructor or module (``OpenAI()``,
``ChatAnthropic()``, ``boto3.client("bedrock-runtime")``, ``openai.embeddings``,
...), reads ``model``, region and endpoint keywords when they are string
literals, and finds calls that start a fine-tuning job.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from .snapshot import Snapshot

# Constructors and factory functions, by name, mapped to a provider id.
PROVIDER_CALLS = {
    "OpenAI": "openai", "AsyncOpenAI": "openai", "OpenAIEmbeddings": "openai", "ChatOpenAI": "openai",
    "AzureOpenAI": "azure-openai", "AsyncAzureOpenAI": "azure-openai",
    "AzureOpenAIEmbeddings": "azure-openai", "AzureChatOpenAI": "azure-openai",
    "Anthropic": "anthropic", "AsyncAnthropic": "anthropic", "ChatAnthropic": "anthropic",
    "AnthropicBedrock": "bedrock", "AnthropicVertex": "vertex",
    "CohereEmbeddings": "cohere", "ChatCohere": "cohere",
    "VoyageAIEmbeddings": "voyage",
    "BedrockEmbeddings": "bedrock", "ChatBedrock": "bedrock", "ChatBedrockConverse": "bedrock",
    "VertexAIEmbeddings": "vertex", "ChatVertexAI": "vertex",
    "GoogleGenerativeAIEmbeddings": "google", "ChatGoogleGenerativeAI": "google",
    "Mistral": "mistral", "MistralAIEmbeddings": "mistral", "ChatMistralAI": "mistral",
    "HuggingFaceInferenceAPIEmbeddings": "huggingface",
    # Models that run on your own infrastructure.
    "HuggingFaceEmbeddings": "local", "OllamaEmbeddings": "local", "ChatOllama": "local",
    "SentenceTransformer": "local",
}
# ``module.Client(...)`` style constructors.
MODULE_CLIENTS = {("cohere", "Client"): "cohere", ("cohere", "ClientV2"): "cohere", ("voyageai", "Client"): "voyage"}
# Module-level API calls such as ``openai.embeddings.create(...)``.
MODULE_APIS = {"openai": "openai", "anthropic": "anthropic", "cohere": "cohere", "voyageai": "voyage"}
# ``boto3.client("<service>")`` services that are model endpoints.
BOTO_SERVICES = {"bedrock-runtime": "bedrock", "bedrock-agent-runtime": "bedrock", "sagemaker-runtime": "sagemaker"}

TRAINER_CALLS = {"Trainer", "SFTTrainer", "Seq2SeqTrainer", "DPOTrainer"}

_MODEL_KW = ("model", "model_name", "model_id", "deployment")
_REGION_KW = ("region_name", "region", "location")
_ENDPOINT_KW = ("base_url", "azure_endpoint", "endpoint_url", "api_base", "endpoint")


@dataclass(frozen=True)
class ProviderRef:
    provider: str
    file: str
    line: int
    via: str
    model: str | None = None
    region: str | None = None
    endpoint: str | None = None


@dataclass(frozen=True)
class TrainingRef:
    file: str
    line: int
    via: str


def _literal(call: ast.Call, names: tuple[str, ...]) -> str | None:
    for kw in call.keywords:
        if kw.arg in names and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _chain(func: ast.expr) -> list[str]:
    """``a.b.c`` -> ["a", "b", "c"]; empty when the root is not a plain name."""
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return []
    parts.append(func.id)
    return parts[::-1]


def _provider(call: ast.Call) -> tuple[str, str] | None:
    chain = _chain(call.func)
    if not chain:
        return None
    name = chain[-1]
    if name in PROVIDER_CALLS:
        return PROVIDER_CALLS[name], name
    if len(chain) == 2 and tuple(chain) in MODULE_CLIENTS:
        return MODULE_CLIENTS[tuple(chain)], ".".join(chain)  # type: ignore[index]
    if chain == ["boto3", "client"] and call.args and isinstance(call.args[0], ast.Constant):
        service = call.args[0].value
        if service in BOTO_SERVICES:
            return BOTO_SERVICES[service], f'boto3.client("{service}")'
    if chain == ["vertexai", "init"]:
        return "vertex", "vertexai.init"
    if len(chain) >= 3 and chain[0] in MODULE_APIS:
        return MODULE_APIS[chain[0]], ".".join(chain)
    return None


def _training(call: ast.Call) -> str | None:
    chain = _chain(call.func) or [getattr(call.func, "attr", "")]
    if chain[-3:] == ["fine_tuning", "jobs", "create"]:
        return "fine_tuning.jobs.create"
    if chain[-1] in TRAINER_CALLS:
        return chain[-1]
    # Attribute calls on an expression, e.g. ``client().fine_tuning.jobs.create``.
    func = call.func
    tail: list[str] = []
    while isinstance(func, ast.Attribute) and len(tail) < 3:
        tail.append(func.attr)
        func = func.value
    if tail[::-1] == ["fine_tuning", "jobs", "create"]:
        return "fine_tuning.jobs.create"
    return None


def scan_file(tree: ast.AST, path: str) -> tuple[list[ProviderRef], list[TrainingRef]]:
    providers: list[ProviderRef] = []
    training: list[TrainingRef] = []
    seen_modules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        via = _training(node)
        if via:
            training.append(TrainingRef(path, node.lineno, via))
        found = _provider(node)
        if found is None:
            continue
        provider, via = found
        if via.count(".") >= 2:
            # One finding per module-level API per file is enough.
            if provider in seen_modules:
                continue
            seen_modules.add(provider)
        providers.append(
            ProviderRef(
                provider=provider,
                file=path,
                line=node.lineno,
                via=via,
                model=_literal(node, _MODEL_KW),
                region=_literal(node, _REGION_KW),
                endpoint=_literal(node, _ENDPOINT_KW),
            )
        )
    return providers, training


def scan_providers(snapshot: Snapshot, paths: list[str]) -> tuple[list[ProviderRef], list[TrainingRef], list[str]]:
    """Provider clients and fine-tuning calls in the Python files under ``paths``."""
    providers: set[ProviderRef] = set()
    training: set[TrainingRef] = set()
    warnings: list[str] = []
    files = sorted({f for p in paths for f in snapshot.list_files(p) if f.endswith(".py")})
    for path in files:
        try:
            tree = ast.parse(snapshot.read_text(path) or "", filename=path)
        except SyntaxError as exc:
            warnings.append(f"{path}: could not parse ({exc.msg}, line {exc.lineno})")
            continue
        p, t = scan_file(tree, path)
        providers.update(p)
        training.update(t)
    key = lambda r: (r.file, r.line)  # noqa: E731
    return sorted(providers, key=key), sorted(training, key=key), warnings


def region_zone(region: str) -> str:
    """Coarse jurisdiction of a cloud region name: ``eu-west-1``, ``europe-west4`` and ``westeurope`` are all ``eu``."""
    r = region.strip().lower()
    if r.startswith(("eu", "europe")) or "europe" in r or r in ("uksouth", "ukwest", "francecentral", "germanywestcentral", "swedencentral", "switzerlandnorth", "norwayeast"):
        return "eu"
    if r.startswith(("us", "northamerica")) or r in ("eastus", "eastus2", "westus", "westus2", "westus3", "centralus", "southcentralus", "northcentralus"):
        return "us"
    if r.startswith(("ap", "asia", "australia", "japan", "korea", "india")):
        return "apac"
    if r.startswith(("ca", "canada")):
        return "ca"
    if r.startswith(("sa", "southamerica", "brazil")):
        return "sa"
    return r.split("-")[0]


def region_allowed(region: str, residency: list[str]) -> bool:
    allowed = {r.strip().lower() for r in residency}
    return region.strip().lower() in allowed or region_zone(region) in allowed
