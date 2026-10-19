"""Models.dev fallback, matched by endpoint and exact model identity."""

from urllib.parse import urlparse

from .capabilities import ModelCapabilities
from .metadata import read_json

CATALOG_URL = "https://models.dev/api.json"


def endpoint_id(url):
    parsed = urlparse(url)
    # DeepSeek and several compatible providers accept both / and /v1.
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    return parsed.scheme.lower(), parsed.netloc.lower(), path


def from_catalog(config, client):
    _, data = read_json(client, CATALOG_URL, timeout=10)
    if not isinstance(data, dict):
        return None
    requested = endpoint_id(config.base_url)
    for provider_id, provider in data.items():
        if not isinstance(provider, dict):
            continue
        api = provider.get("api")
        if provider_id == "openai" and not api:
            api = "https://api.openai.com/v1"
        if config.catalog_provider:
            if provider_id != config.catalog_provider:
                continue
        elif not isinstance(api, str) or endpoint_id(api) != requested:
            continue
        models = provider.get("models", {})
        if not isinstance(models, dict):
            continue
        # Some official GLM examples use different case; never strip prefixes,
        # dates or suffixes, or infer another model from a similar name.
        matches = [
            value for key, value in models.items() if key.casefold() == config.model.casefold()
        ]
        if len(matches) != 1:
            continue
        return ModelCapabilities.from_metadata(matches[0], f"models.dev:{provider_id}")
    return None
