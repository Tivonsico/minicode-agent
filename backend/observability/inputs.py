"""Store the redacted payload actually sent, without authentication headers."""

import hashlib
import json
import re
import uuid

from backend.storage.files import atomic_json, internal_directory


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {"api_key", "password", "access_token", "authorization", "secret_key"}
            and isinstance(item, str)
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.DOTALL,
    )
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|password|access[_-]?token|authorization)\b[\"\']?\s*[:=]\s*)"
        r"([^\s,;\n]+)",
        r"\1[REDACTED]",
        value,
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def save_input(store, payload, config):
    directory = internal_directory(store.root, "sessions", store.session_id, "inputs")
    safe = redact(payload, store.secrets)
    identifier = uuid.uuid4().hex
    versions = {
        "instructions": digest(
            [m for m in safe["messages"] if m.get("role") in {"system", "developer"}]
        ),
        "tools": digest(safe.get("tools", [])),
        "payload": digest(safe),
    }
    artifact = {
        "schema_version": 1,
        "payload": safe,
        "versions": versions,
        "model": config.model,
        "base_url": config.base_url,
        "redaction_applied": safe != payload,
    }
    from backend.modes.catalog import tags

    from .scope import current

    artifact.update(tags(current(store)))
    atomic_json(directory / f"{identifier}.json", artifact)
    return {"input_ref": f"inputs/{identifier}.json", "input_versions": versions}


def read_input(store, ref):
    # Never accept an arbitrary path supplied by a browser.
    if not re.fullmatch(r"inputs/[a-f0-9]{32}\.json", ref):
        raise ValueError("无效的请求输入引用")
    path = store.directory / ref
    if path.is_symlink() or not path.resolve().is_relative_to(store.directory.resolve()):
        raise ValueError("请求输入路径无效")
    return json.loads(path.read_text(encoding="utf-8"))
