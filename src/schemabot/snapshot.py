"""Read-only views of the repository at one point in time.

A check compares two snapshots: the pull request's head (the working tree) and
its base (a git revision). Both expose the same tiny interface so extractors
never care which one they are reading.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath


class SnapshotError(RuntimeError):
    pass


class Snapshot:
    """Files under a root directory, addressed by POSIX paths relative to it."""

    label: str = "snapshot"

    def read_text(self, path: str) -> str | None:
        raise NotImplementedError

    def list_files(self, directory: str) -> list[str]:
        """All files under ``directory`` (recursively), sorted, relative to the root."""
        raise NotImplementedError

    def is_dir(self, path: str) -> bool:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.is_dir(path) or self.read_text(path) is not None


def _clean(path: str) -> str:
    cleaned = str(PurePosixPath(path.replace("\\", "/")))
    return "" if cleaned == "." else cleaned.lstrip("/")


class WorkingTree(Snapshot):
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.label = "working tree"

    def _abs(self, path: str) -> Path:
        return self.root / _clean(path)

    def read_text(self, path: str) -> str | None:
        target = self._abs(path)
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def is_dir(self, path: str) -> bool:
        return self._abs(path).is_dir()

    def list_files(self, directory: str) -> list[str]:
        base = self._abs(directory)
        if base.is_file():
            return [_clean(directory)]
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.root).parts
        )


class GitRevision(Snapshot):
    """A git revision, read with ``git show`` without touching the working tree.

    ``prefix`` is the snapshot root inside the repository, so a check can run
    on a sub-project (``examples/rag-demo``) with the same relative paths as
    the working tree.
    """

    def __init__(self, repo: str | Path, rev: str, prefix: str = "") -> None:
        self.repo = Path(repo).resolve()
        self.rev = rev
        self.prefix = _clean(prefix)
        self.label = f"git {rev}"
        self._files: list[str] | None = None
        try:
            self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        except SnapshotError as exc:
            raise SnapshotError(
                f"git revision {rev!r} not found; fetch it first "
                f"(for example: git fetch --depth=1 origin {rev})"
            ) from exc

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise SnapshotError(proc.stderr.strip() or f"git {' '.join(args)} failed")
        return proc.stdout

    def _full(self, path: str) -> str:
        path = _clean(path)
        return f"{self.prefix}/{path}" if self.prefix and path else (self.prefix or path)

    def _all_files(self) -> list[str]:
        if self._files is None:
            args = ["ls-tree", "-r", "--name-only", self.rev]
            if self.prefix:
                args += ["--", self.prefix]
            out = self._git(*args)
            strip = len(self.prefix) + 1 if self.prefix else 0
            self._files = sorted(line[strip:] for line in out.splitlines() if line)
        return self._files

    def read_text(self, path: str) -> str | None:
        if _clean(path) not in self._all_files():
            return None
        return self._git("show", f"{self.rev}:{self._full(path)}")

    def is_dir(self, path: str) -> bool:
        path = _clean(path)
        if not path:
            return True
        return any(f.startswith(path + "/") for f in self._all_files())

    def list_files(self, directory: str) -> list[str]:
        directory = _clean(directory)
        files = self._all_files()
        if directory in files:
            return [directory]
        if not directory:
            return list(files)
        return [f for f in files if f.startswith(directory + "/")]


def git_toplevel(path: str | Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()
