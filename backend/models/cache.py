"""Atomic, credential-scoped capability cache; no credentials are stored."""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict

from .capabilities import ModelCapabilities

TTL = 24 * 60 * 60


class CapabilityCache:
    def __init__(self, config):
        identity = json.dumps(
            [
                config.base_url.rstrip("/"),
                config.model,
                config.catalog_provider,
                config.api_key,
            ]
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.path = config.root / ".minicode" / "model_capabilities" / (digest + ".json")

    def read(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 16_384:
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            age = time.time() - data["saved_at"]
            if not 0 <= age < TTL:
                return None
            return ModelCapabilities(**data["capabilities"])
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def write(self, capabilities):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump({"saved_at": time.time(), "capabilities": asdict(capabilities)}, handle)
            os.replace(temporary, self.path)
        except OSError:
            pass  # A read-only cache must not prevent using resolved metadata.
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
