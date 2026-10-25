"""Command line: ``schemabot validate`` and ``schemabot check``.

Exit codes: 0 passed, 1 a finding at or above ``--fail-on``, 2 usage or setup error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .check import run_check
from .contract import load_contracts
from .findings import Severity
from .report import to_json, to_markdown, to_sarif, to_text
from .snapshot import GitRevision, SnapshotError, WorkingTree, git_toplevel


def _threshold(value: str) -> Severity | None:
    if value.lower() == "never":
        return None
    try:
        return Severity.parse(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _write(path: str | None, content: str) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _prefix(root: Path) -> tuple[Path | None, str]:
    top = git_toplevel(root)
    if top is None:
        return None, ""
    rel = root.resolve().relative_to(top).as_posix()
    return top, "" if rel == "." else rel


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root)
    head = WorkingTree(root)
    if not head.is_dir(args.contracts):
        print(f"schemabot: contracts directory {args.contracts!r} not found under {root}", file=sys.stderr)
        return 2
    top, prefix = _prefix(root)
    base = None
    if args.base:
        if top is None:
            print("schemabot: --base needs a git repository", file=sys.stderr)
            return 2
        try:
            base = GitRevision(top, args.base, prefix)
        except SnapshotError as exc:
            print(f"schemabot: {exc}", file=sys.stderr)
            return 2

    result = run_check(head, args.contracts, base)

    if args.explain:
        from .explain import explain

        contracts = {c.id: c for c in load_contracts(head, args.contracts)[0]}
        summary, error = explain(result, contracts, args.model)
        result.explanation = summary
        if error:
            print(f"schemabot: {error}", file=sys.stderr)

    threshold = args.fail_on
    renderers = {
        "text": lambda: to_text(result, threshold),
        "markdown": lambda: to_markdown(result, threshold),
        "json": lambda: to_json(result, threshold),
        "sarif": lambda: to_sarif(result, prefix),
    }
    sys.stdout.write(renderers[args.format]())
    _write(args.markdown, to_markdown(result, threshold))
    _write(args.json, to_json(result, threshold))
    _write(args.sarif, to_sarif(result, prefix))
    return 1 if result.fails(threshold) else 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.root)
    head = WorkingTree(root)
    if not head.is_dir(args.contracts):
        print(f"schemabot: contracts directory {args.contracts!r} not found under {root}", file=sys.stderr)
        return 2
    result = run_check(head, args.contracts, None)
    sys.stdout.write(to_text(result, args.fail_on))
    return 1 if result.fails(args.fail_on) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabot",
        description="Data contracts for RAG and ingestion pipelines, checked on every pull request.",
    )
    parser.add_argument("--version", action="version", version=f"schemabot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="project root; contract paths are relative to it (default: .)")
    common.add_argument(
        "--contracts",
        default=os.environ.get("SCHEMABOT_CONTRACTS", "contracts"),
        help="directory of contract YAML files, relative to --root (default: contracts)",
    )
    common.add_argument(
        "--fail-on",
        type=_threshold,
        default=Severity.BREAKING,
        metavar="LEVEL",
        help="fail at this severity or worse: breaking (default), reindex, warning, info, never",
    )

    check = sub.add_parser(
        "check",
        parents=[common],
        help="check contracts against the working tree, and against a base revision when given",
    )
    check.add_argument("--base", help="git revision to compare with, e.g. origin/main")
    check.add_argument("--format", choices=["text", "markdown", "json", "sarif"], default="text")
    check.add_argument("--markdown", metavar="PATH", help="also write the PR comment Markdown here")
    check.add_argument("--json", metavar="PATH", help="also write the JSON report here")
    check.add_argument("--sarif", metavar="PATH", help="also write a SARIF report here")
    check.add_argument("--explain", action="store_true", help="add an AI-written summary (needs schemabot[llm])")
    check.add_argument("--model", help="Claude model for --explain (default: $SCHEMABOT_MODEL or claude-opus-5-5)")
    check.set_defaults(func=cmd_check)

    validate = sub.add_parser("validate", parents=[common], help="check contracts and sources at the working tree only")
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
