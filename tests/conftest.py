from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "examples" / "rag-demo"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    ).stdout


class Repo:
    """A throwaway git repo holding a copy of the demo project, committed as the base."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def read(self, path: str) -> str:
        return (self.root / path).read_text()

    def replace(self, path: str, old: str, new: str) -> None:
        text = self.read(path)
        assert old in text, f"{old!r} not in {path}"
        self.write(path, text.replace(old, new, 1))

    def delete(self, path: str) -> None:
        (self.root / path).unlink()

    def commit(self, message: str = "change") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)
        return git(self.root, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "proj"
    shutil.copytree(DEMO, root)
    git(root, "init", "-q", "-b", "main")
    r = Repo(root)
    r.commit("base")
    return r
