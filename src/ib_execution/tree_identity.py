"""Canonical repository tree identity rules.

This module contains no Git subprocesses and no Gate decisions.  Callers give
it either worktree bytes or exact Git-object bytes and receive the same
identity calculation.  Keeping selection and hashing here prevents provenance
and evidence validators from drifting into separate definitions of "code".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

TREE_IDENTITY_SCHEMA_VERSION = 1

# Everything under these roots must use an explicitly reviewed suffix.  The
# PowerShell fault harnesses affect safety evidence and therefore are code.
SOURCE_ROOTS = ("src", "tests", "scripts")
SOURCE_SUFFIXES = (".py", ".ps1")
SOURCE_EXTRA_FILES = ("pyproject.toml",)

# Generated local build products are not versioned inputs.  Every other file
# type under a source root fails closed until this registry is reviewed.
IGNORED_WORKTREE_DIR_NAMES = ("__pycache__",)
IGNORED_WORKTREE_DIR_SUFFIXES = (".egg-info",)
IGNORED_WORKTREE_SUFFIXES = (".pyc",)

CONFIG_GLOBS = ("config/*.example.yml",)
DEPENDENCY_LOCK_FILE = "uv.lock"


class TreeIdentityError(ValueError):
    """Repository contents cannot be classified by the canonical rules."""


@dataclass(frozen=True)
class TreeIdentity:
    source_tree_sha256: str
    source_files: tuple[str, ...]
    config_tree_sha256: str
    config_files: tuple[str, ...]
    dependency_lock_sha256: str

    def as_state(self) -> dict[str, object]:
        return {
            "source_tree_sha256": self.source_tree_sha256,
            "source_file_count": len(self.source_files),
            "config_tree_sha256": self.config_tree_sha256,
            "config_files": list(self.config_files),
            "dependency_lock_sha256": self.dependency_lock_sha256,
        }


BlobReader = Callable[[str], bytes]


def _normalise_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalised: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        value = path.as_posix()
        if (
            not raw
            or "\\" in raw
            or path.is_absolute()
            or value != raw
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise TreeIdentityError(f"invalid repository path: {raw!r}")
        normalised.append(value)
    if len(normalised) != len(set(normalised)):
        raise TreeIdentityError("repository path list contains duplicates")
    return tuple(sorted(normalised))


def _matches_config(path: str) -> bool:
    candidate = PurePosixPath(path)
    return any(candidate.match(pattern) for pattern in CONFIG_GLOBS)


def select_identity_paths(paths: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Classify versioned paths, rejecting unreviewed behaviour/config inputs."""
    normalised = _normalise_paths(paths)
    available = set(normalised)
    missing_extras = sorted(set(SOURCE_EXTRA_FILES) - available)
    if missing_extras:
        raise TreeIdentityError("missing required source extra files: " + ",".join(missing_extras))
    if DEPENDENCY_LOCK_FILE not in available:
        raise TreeIdentityError(f"missing dependency lock: {DEPENDENCY_LOCK_FILE}")

    source: list[str] = list(SOURCE_EXTRA_FILES)
    config: list[str] = []
    unknown_source: list[str] = []
    unknown_config: list[str] = []
    for path in normalised:
        parts = PurePosixPath(path).parts
        if parts[0] in SOURCE_ROOTS:
            if PurePosixPath(path).suffix in SOURCE_SUFFIXES:
                source.append(path)
            else:
                unknown_source.append(path)
        elif parts[0] == "config":
            if _matches_config(path):
                config.append(path)
            else:
                unknown_config.append(path)
    if unknown_source:
        raise TreeIdentityError(
            "unclassified files under source roots: " + ",".join(unknown_source)
        )
    if unknown_config:
        raise TreeIdentityError("unclassified config files: " + ",".join(unknown_config))
    if not config:
        raise TreeIdentityError("canonical config globs matched no files")
    return tuple(sorted(set(source))), tuple(sorted(set(config)))


def digest_named_blobs(paths: Sequence[str], read_blob: BlobReader) -> str:
    """Hash names and bytes, making renames part of the identity."""
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(read_blob(path))
        digest.update(b"\0")
    return digest.hexdigest()


def derive_from_versioned_paths(paths: Iterable[str], read_blob: BlobReader) -> TreeIdentity:
    source, config = select_identity_paths(paths)
    return TreeIdentity(
        source_tree_sha256=digest_named_blobs(source, read_blob),
        source_files=source,
        config_tree_sha256=digest_named_blobs(config, read_blob),
        config_files=config,
        dependency_lock_sha256=hashlib.sha256(read_blob(DEPENDENCY_LOCK_FILE)).hexdigest(),
    )


def _is_ignored_worktree_file(relative: Path) -> bool:
    return (
        relative.suffix in IGNORED_WORKTREE_SUFFIXES
        or any(part in IGNORED_WORKTREE_DIR_NAMES for part in relative.parts)
        or any(
            part.endswith(suffix)
            for part in relative.parts
            for suffix in IGNORED_WORKTREE_DIR_SUFFIXES
        )
    )


def worktree_versioned_paths(root: Path) -> tuple[str, ...]:
    """Return canonical worktree inputs and fail on unknown source file types.

    Gitignored runtime configuration is intentionally not traversed.  Only
    checked-in example patterns are observable configuration authority.
    """
    paths: list[str] = []
    for extra in SOURCE_EXTRA_FILES:
        path = root / extra
        if not path.is_file():
            raise TreeIdentityError(f"missing required source extra file: {extra}")
        paths.append(extra)
    lock = root / DEPENDENCY_LOCK_FILE
    if not lock.is_file():
        raise TreeIdentityError(f"missing dependency lock: {DEPENDENCY_LOCK_FILE}")
    paths.append(DEPENDENCY_LOCK_FILE)

    for source_root in SOURCE_ROOTS:
        folder = root / source_root
        if not folder.is_dir():
            raise TreeIdentityError(f"missing source root: {source_root}")
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if _is_ignored_worktree_file(relative):
                continue
            paths.append(relative.as_posix())

    config_paths: set[str] = set()
    for pattern in CONFIG_GLOBS:
        config_paths.update(
            path.relative_to(root).as_posix()
            for path in root.glob(pattern)
            if path.is_file()
        )
    config_root = root / "config"
    if config_root.is_dir():
        for path in config_root.iterdir():
            if path.is_file() and ".example." in path.name:
                config_paths.add(path.relative_to(root).as_posix())
    paths.extend(sorted(config_paths))
    return tuple(sorted(paths))


def derive_from_worktree(root: Path) -> TreeIdentity:
    paths = worktree_versioned_paths(root)
    return derive_from_versioned_paths(paths, lambda path: (root / path).read_bytes())


def drift_components(current: TreeIdentity, candidate: Mapping[str, object]) -> tuple[str, ...]:
    mapping = {
        "source": "source_tree_sha256",
        "config": "config_tree_sha256",
        "dependency_lock": "dependency_lock_sha256",
    }
    return tuple(
        component
        for component, key in mapping.items()
        if candidate.get(key) != getattr(current, key)
    )
