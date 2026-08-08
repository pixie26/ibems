"""
Separate-failure-domain checks, shared by the fatal fence and the journal witness.

    ####################################################################
    #  Both files exist to outlive the journal's storage.  A copy of    #
    #  that requirement in a --help string is not a control; this is.   #
    ####################################################################

``st_dev`` is the device id on POSIX and the volume serial number on Windows,
which is the granularity that matters: a full or failing volume takes down
everything on it at once.

This started as a private helper inside ``fatal_fence``. The witness then grew
the same requirement, its CLI flag documented it in prose, and nothing checked
it -- an operator could point ``--witness`` back at the journal's volume and
get a deployment that looked configured and protected nothing. One helper, two
callers, one enforcement path.
"""

from __future__ import annotations

import os
from pathlib import Path


class FailureDomainError(RuntimeError):
    """Two paths that must survive each other share a volume."""


def _anchor(path: Path) -> Path:
    """The nearest existing ancestor, since the file itself may not exist yet."""
    candidate = path if path.exists() else path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def same_failure_domain(a: Path, b: Path) -> bool:
    try:
        return os.stat(_anchor(a)).st_dev == os.stat(_anchor(b)).st_dev
    except OSError as exc:
        # Undeterminable is not the same as fine. Say so rather than assume.
        raise FailureDomainError(
            f"cannot determine the failure domain of {a} or {b}: {exc}"
        ) from exc


def require_separate(guardian: Path, protected: Path, what: str) -> None:
    """Refuse when ``guardian`` cannot outlive ``protected``'s storage."""
    if same_failure_domain(guardian, protected):
        raise FailureDomainError(
            f"the {what} ({guardian}) is on the same volume as the journal "
            f"({protected}). That volume filling up or failing is the most likely "
            f"reason the {what} is needed, so it must live elsewhere."
        )
