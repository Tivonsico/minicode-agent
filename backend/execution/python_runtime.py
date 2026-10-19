"""Locate the running app's Python runtime without granting application access."""

import sys
from pathlib import Path


def python_runtime(application_root=None):
    root = Path(sys.base_prefix).resolve()
    executable = Path(sys.executable).resolve()
    if not executable.is_relative_to(root):
        return None
    if application_root is not None:
        application = Path(application_root).resolve()
        if root.is_relative_to(application) or application.is_relative_to(root):
            return None
    return root, executable
