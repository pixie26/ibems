"""
Machine-readable repository state.

    ####################################################################
    #  Documentation is not a control.  Neither is a hand-maintained   #
    #  checksum file.  Every number a human might otherwise copy into  #
    #  a README is generated here and verified by a test.              #
    ####################################################################

WHY THIS MODULE EXISTS
----------------------
Before this, the repository carried a hand-written ``SHA256SUMS`` plus four
prose documents that each restated the project's validation status. They
drifted: ``README.md`` recorded a market-data entitlement as resolved while
``VALIDATION_MANIFEST.txt`` still recorded it as failed, and ``SHA256SUMS``
did not match its own worktree. For an ordinary project that is a
documentation defect. For a platform whose product *is* auditability, a
provenance file that does not describe the tree it ships with is worse than
no provenance file at all -- it manufactures confidence.

So there is exactly one machine-readable state file, ``STATE.json``, it is
generated, and ``tests/test_provenance.py`` fails the build when it is stale.
Gate B1 PASS is generated too: it is re-derived from a valid exact-freeze
sign-off/evidence attestation and Git history, never carried forward by hand.

FOUR HASHES, FOUR DIFFERENT QUESTIONS
-------------------------------------
They are deliberately not merged into one number:

``source_tree_sha256``
    What logic ran. Changing it invalidates a Gate B1 sign-off.

``config_tree_sha256``
    What limits it ran under. Invariant 17 makes the risk configuration part
    of the safety argument, so it cannot be folded into the source hash and
    silently ignored.

``dependency_lock_sha256``
    What *should* be installed.

``resolved_environment_sha256``
    What *actually was* installed when the evidence was produced. A lockfile
    is an intention; only this one is an observation, and Gate B1 is an
    argument about observations.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from . import attestation, gate

STATE_FILENAME = "STATE.json"

# Included in source_tree_sha256. Everything that decides behaviour.
SOURCE_ROOTS = ("src", "tests", "scripts")
SOURCE_SUFFIX = ".py"
SOURCE_EXTRA = ("pyproject.toml",)

# Included in config_tree_sha256. Real config files are gitignored; only the
# checked-in examples are hashed, and that is the point -- the examples are
# what a reviewer can actually read.
CONFIG_GLOBS = ("config/*.example.yml",)

LOCK_FILENAME = "uv.lock"


def _digest_files(root: Path, paths: Iterable[Path]) -> tuple[str, list[str]]:
    """Hash file *names and contents* together, so a rename is a change."""
    digest = hashlib.sha256()
    names: list[str] = []
    for path in sorted(set(paths)):
        name = path.relative_to(root).as_posix()
        names.append(name)
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), names


def source_tree_sha256(root: Path) -> tuple[str, list[str]]:
    paths = [root / name for name in SOURCE_EXTRA if (root / name).exists()]
    for folder in SOURCE_ROOTS:
        paths.extend(sorted((root / folder).rglob(f"*{SOURCE_SUFFIX}")))
    return _digest_files(root, paths)


def config_tree_sha256(root: Path) -> tuple[str, list[str]]:
    paths: list[Path] = []
    for pattern in CONFIG_GLOBS:
        paths.extend(sorted(root.glob(pattern)))
    return _digest_files(root, paths)


def dependency_lock_sha256(root: Path) -> Optional[str]:
    lock = root / LOCK_FILENAME
    if not lock.exists():
        return None
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def resolved_environment() -> list[str]:
    """The installed distributions, normalized, as an observation."""
    from importlib.metadata import distributions

    seen: dict[str, str] = {}
    for dist in distributions():
        name = (dist.metadata["Name"] or "").strip().lower().replace("_", "-")
        if name:
            seen[name] = dist.version or ""
    return [f"{name}=={seen[name]}" for name in sorted(seen)]


def resolved_environment_sha256() -> str:
    payload = "\n".join(resolved_environment()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def commit_sha(root: Path) -> Optional[str]:
    """HEAD, or None outside a git checkout. Never guessed."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def worktree_clean(root: Path) -> Optional[bool]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() == ""


def tree_state(root: Path) -> dict[str, Any]:
    """The part of the state that is a pure function of the worktree.

    ``STATE.json`` is regenerated from this and diffed by a test, so it must
    not contain anything that varies between machines or between runs --
    no timestamps, no interpreter version, no commit sha (a commit cannot
    contain its own hash).
    """
    source_hash, source_files = source_tree_sha256(root)
    config_hash, config_files = config_tree_sha256(root)
    return {
        "source_tree_sha256": source_hash,
        "source_file_count": len(source_files),
        "config_tree_sha256": config_hash,
        "config_files": config_files,
        "dependency_lock_sha256": dependency_lock_sha256(root),
    }


def environment_state() -> dict[str, Any]:
    """The part that is an observation of *this* machine, not of the tree."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "resolved_environment_sha256": resolved_environment_sha256(),
    }


def load_state(root: Path) -> Optional[dict[str, Any]]:
    path = root / STATE_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def derived_gate_status(root: Path) -> dict[str, Any]:
    """Recompute historical B1 passage and current-worktree coverage."""
    signed_off_commit = attestation.derive_signed_off_commit(root)
    historical = attestation.derive_historical_attested_freeze(root)
    return gate.as_state(
        signed_off_commit=signed_off_commit,
        attested_freeze_commit=(historical.freeze_commit if historical else None),
    )


def write_state(root: Path) -> Path:
    """Regenerate ``STATE.json``. The only supported way to change it.

    No Gate value is carried forward from the existing file. Requirements come
    from ``gate.B1_REQUIREMENTS``. Historical exact-freeze passage is derived
    from immutable Git objects, while current-worktree coverage is revalidated
    independently and becomes false after any later behavior change.
    """
    state = {
        "schema_version": 3,
        "_comment": (
            "GENERATED by `python -m ib_execution.provenance` -- do not hand-edit. "
            "gate_status separately derives historical B1 exact-freeze passage "
            "and current-worktree attestation coverage. tests/test_provenance.py "
            "fails when this file is stale."
        ),
        "tree": tree_state(root),
        "gate_status": derived_gate_status(root),
    }
    path = root / STATE_FILENAME
    tmp = root / f".{STATE_FILENAME}.tmp"
    # Write bytes so Windows cannot translate LF to CRLF. `.gitattributes`
    # deliberately disables Git EOL conversion; generated provenance must
    # therefore be platform-independent before Git sees it.
    tmp.write_bytes((json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    tmp.replace(path)
    return path


def stale_fields(root: Path) -> dict[str, tuple[Any, Any]]:
    """Recorded-vs-derived state for tree identity and the entire gate section."""
    recorded = load_state(root)
    if recorded is None:
        return {"STATE.json": ("missing", "expected")}
    stale = {
        key: (recorded.get("tree", {}).get(key), value)
        for key, value in tree_state(root).items()
        if recorded.get("tree", {}).get(key) != value
    }
    expected_gate = derived_gate_status(root)
    recorded_gate = recorded.get("gate_status", {})
    for key, value in expected_gate.items():
        if recorded_gate.get(key) != value:
            stale[f"gate_status.{key}"] = (recorded_gate.get(key), value)
    return stale


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Generate or verify STATE.json")
    ap.add_argument("--check", action="store_true", help="verify without writing")
    ap.add_argument("--root", default=None)
    ns = ap.parse_args(argv)
    root = Path(ns.root).resolve() if ns.root else Path(__file__).resolve().parents[2]

    if ns.check:
        stale = stale_fields(root)
        if stale:
            for key, (recorded, actual) in sorted(stale.items()):
                print(f"STALE {key}\n  recorded: {recorded}\n  actual:   {actual}")
            print("\nSTATE.json is stale. Run: python -m ib_execution.provenance", file=sys.stderr)
            return 1
        print("STATE.json matches the worktree and derived gate attestation")
        return 0

    path = write_state(root)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
