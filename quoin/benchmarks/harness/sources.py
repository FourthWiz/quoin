"""Task sources that run on a prepared repository checkout.

SWE-bench Verified and SWE-bench Pro tasks share the repository preparation,
patch capture, session record and deferred judging; they differ only in the
prompt and the judge, which dispatch on these names.
"""

from __future__ import annotations

import re

VERIFIED_SOURCE = "swebench_verified"
PRO_SOURCE = "swebench_pro"
REPO_TASK_SOURCES = (VERIFIED_SOURCE, PRO_SOURCE)
# `instance_<owner>__<repo>-<fix sha>[-v<env sha>|-vnan]`; safe as a ref name
# and a path component.
PRO_INSTANCE_ID_PATTERN = (
    r"instance_[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9a-f]{40}(?:-v(?:[0-9a-f]{40}|nan))?"
)
PRO_INSTANCE_ID_RE = re.compile("^" + PRO_INSTANCE_ID_PATTERN + "$")


def is_repo_task(task_spec) -> bool:
    return isinstance(task_spec, dict) and task_spec.get("source") in REPO_TASK_SOURCES
