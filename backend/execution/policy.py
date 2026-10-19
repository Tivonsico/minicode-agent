"""Explicit Codex runtime policy; user CLI defaults cannot broaden permissions."""

from pathlib import Path

from .python_runtime import python_runtime


def sandbox_state(root, roots, application_root, runtime_roots=()):
    def entry(path, access):
        return {"path": {"type": "path", "path": str(Path(path).resolve())}, "access": access}

    entries = [{"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"}]
    runtime = python_runtime(application_root)
    if runtime:
        entries.append(entry(runtime[0], "read"))
    for path in runtime_roots:
        entries.append(entry(path, "read"))
    for work in roots:
        entries.append(entry(work, "write"))
        for protected in (".git", ".minicode", "project_system_prompt.md", "project_skills"):
            entries.append(entry(Path(work) / protected, "deny"))
    entries.append(entry(application_root, "deny"))
    for pattern in ("**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa", "**/id_ed25519"):
        entries.append({"path": {"type": "glob_pattern", "pattern": pattern}, "access": "deny"})
    return {
        "permissionProfile": {
            "type": "managed",
            "file_system": {"type": "restricted", "entries": entries},
            "network": "restricted",
        },
        "sandboxCwd": Path(root).resolve().as_uri(),
    }
