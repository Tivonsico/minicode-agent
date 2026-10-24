import json
import time
from dataclasses import replace

import httpx
import pytest

from backend.config import Config, ConfigError
from backend.context.budget import input_budget
from backend.models import resolve_config
from backend.models.cache import TTL, CapabilityCache
from backend.models.catalog import CATALOG_URL
from backend.runtime.provider import Provider


def config(root, **kwargs):
    return Config(root, "secret-not-for-catalog", model="custom-model", **kwargs)


def catalog(api=None, model="custom-model", provider="openai", window=128000):
    value = {"models": {model: {"limit": {"context": window, "output": 32000, "input": 100000}}}}
    if api:
        value["api"] = api
    return {provider: value}


def test_endpoint_limits_override_catalog_and_input_cap(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer secret-not-for-catalog"
        return httpx.Response(
            200,
            json={
                "id": "custom-model",
                "context_window": 128000,
                "max_output_tokens": 8000,
                "max_input_tokens": 100000,
            },
        )

    resolved = resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert resolved.max_output_tokens == 8000
    assert input_budget(resolved) == 100000
    assert resolved.capability_source.startswith("endpoint:")


def test_list_endpoint_supported_and_exact_id_required(tmp_path):
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "custom-model-next", "context_window": 1000000},
                        {"id": "custom-model", "context_length": 64000,
                         "max_completion_tokens": 16000},
                    ]
                },
            )
        return httpx.Response(200, json={"id": "wrong-model", "context_window": 1000000})

    result = resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert result.context_window == 64000
    assert result.model_max_output == 16000


def test_catalog_fallback_does_not_receive_api_key(tmp_path):
    def handler(request):
        if str(request.url) == CATALOG_URL:
            assert "Authorization" not in request.headers
            assert "secret-not-for-catalog" not in str(request.url)
            return httpx.Response(200, json=catalog())
        return httpx.Response(200, json={"id": "custom-model", "object": "model"})

    resolved = resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert resolved.context_window == 128000
    assert resolved.capability_source == "models.dev:openai"
    assert resolved.max_output_tokens == 32000


def test_missing_endpoint_output_is_filled_from_matching_catalog(tmp_path):
    def handler(request):
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json=catalog())
        return httpx.Response(200, json={"id": "custom-model", "context_window": 64000})

    resolved = resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert resolved.context_window == 64000
    assert resolved.max_output_tokens == 32000
    assert input_budget(resolved) == 32000


def test_endpoint_list_supplies_output_missing_from_detail_before_catalog(tmp_path):
    def handler(request):
        assert str(request.url) != CATALOG_URL
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{
                "id": "custom-model", "context_window": 128000,
                "max_output_tokens": 32000, "max_input_tokens": 100000,
            }]})
        return httpx.Response(200, json={"id": "custom-model", "context_window": 64000})

    resolved = resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert resolved.context_window == 64000
    assert resolved.max_output_tokens == 32000
    assert resolved.model_max_input == 100000


@pytest.mark.parametrize("model", ["custom", "custom-model-2026-10-08", "unknown"])
def test_never_guesses_similar_model(tmp_path, model):
    def handler(request):
        return (
            httpx.Response(200, json=catalog())
            if str(request.url) == CATALOG_URL
            else httpx.Response(404)
        )

    with pytest.raises(ConfigError, match="无法确认"):
        resolve_config(
            replace(config(tmp_path), model=model), transport=httpx.MockTransport(handler)
        )


def test_unknown_gateway_requires_explicit_provider(tmp_path):
    def handler(request):
        return (
            httpx.Response(200, json=catalog())
            if str(request.url) == CATALOG_URL
            else httpx.Response(404)
        )

    cfg = config(tmp_path, base_url="https://gateway.test/v1")
    with pytest.raises(ConfigError, match="MODEL_CATALOG_PROVIDER"):
        resolve_config(cfg, transport=httpx.MockTransport(handler))
    result = resolve_config(
        replace(cfg, catalog_provider="openai"), transport=httpx.MockTransport(handler)
    )
    assert result.context_window == 128000


def test_custom_provider_and_case_keep_original_request_model(tmp_path):
    def handler(request):
        return (
            httpx.Response(
                200,
                json=catalog(
                    "https://vendor.test/v1",
                    model="mixed-case",
                    provider="vendor",
                ),
            )
            if str(request.url) == CATALOG_URL
            else httpx.Response(404)
        )

    cfg = replace(config(tmp_path, base_url="https://vendor.test/v1/"), model="Mixed-Case")
    result = resolve_config(cfg, transport=httpx.MockTransport(handler))
    assert result.model == "Mixed-Case" and result.capability_source == "models.dev:vendor"


def test_auth_error_not_masked_or_leaked(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="secret-not-for-catalog")

    with pytest.raises(ConfigError, match="认证失败") as exc:
        resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert "secret-not-for-catalog" not in str(exc.value)
    assert len(calls) == 1


def test_redirect_does_not_forward_credentials(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json=catalog())
        return httpx.Response(302, headers={"Location": "https://evil.test/models"})

    resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert "evil.test" not in calls


@pytest.mark.parametrize("invalid", [0, -1, True, "100000", 1.5])
def test_invalid_limits_fail_closed(tmp_path, invalid):
    def handler(request):
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "custom-model", "context_window": invalid})

    with pytest.raises(ConfigError):
        resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))


def test_cache_ttl_identity_refresh_and_no_secret(tmp_path):
    cfg = config(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": cfg.model, "context_window": 128000,
                                         "max_output_tokens": 32000})

    transport = httpx.MockTransport(handler)
    first = resolve_config(cfg, transport=transport)
    assert resolve_config(cfg, transport=transport) == first and len(calls) == 1
    cache = CapabilityCache(cfg)
    assert cfg.api_key not in cache.path.read_text()
    assert CapabilityCache(replace(cfg, model="other")).path != cache.path
    assert CapabilityCache(replace(cfg, base_url="https://other.test")).path != cache.path
    assert CapabilityCache(replace(cfg, api_key="other-key")).path != cache.path
    resolve_config(cfg, transport=transport, refresh=True)
    assert len(calls) == 2
    data = json.loads(cache.path.read_text())
    data["saved_at"] = time.time() - TTL - 1
    cache.path.write_text(json.dumps(data))
    resolve_config(cfg, transport=transport)
    assert len(calls) == 3
    cache.path.write_text("invalid")
    resolve_config(cfg, transport=transport)
    assert len(calls) == 4


@pytest.mark.parametrize("previous_budget", [4000, 16000])
def test_model_output_limit_replaces_previous_application_budget(tmp_path, previous_budget):
    def handler(request):
        return httpx.Response(
            200, json={"id": "custom-model", "context_window": 64000, "max_output_tokens": 8000}
        )

    result = resolve_config(
        config(tmp_path, max_output_tokens=previous_budget), transport=httpx.MockTransport(handler)
    )
    assert result.max_output_tokens == 8000


def test_missing_output_limit_does_not_invent_a_budget(tmp_path):
    def handler(request):
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "custom-model", "context_window": 64000})

    with pytest.raises(ConfigError, match="最大输出"):
        resolve_config(config(tmp_path), transport=httpx.MockTransport(handler))
    assert not CapabilityCache(config(tmp_path)).path.exists()


async def test_declared_limits_reach_actual_request_and_input_budget(tmp_path):
    model = "GLM-5.3-Flash"

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            assert payload["max_tokens"] == 131072
            assert payload["reasoning_effort"] == "max"
            return httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            ))
        return httpx.Response(200, json={
            "id": model, "context_window": 1000000, "max_output_tokens": 131072,
            "max_input_tokens": 850000,
        })

    transport = httpx.MockTransport(handler)
    resolved = resolve_config(
        replace(config(tmp_path, base_url="https://vendor.test/v4"), model=model),
        transport=transport,
    )
    assert resolved.context_window == 1000000
    assert resolved.model_max_input == 850000
    assert input_budget(resolved) == 850000

    async def ignore_delta(event):
        pass

    response = await Provider(resolved, transport=transport).complete(
        [{"role": "user", "content": "hello"}], [], ignore_delta
    )
    assert response.content == "ok"


def test_network_failure_does_not_invent_window_and_valid_cache_works_offline(tmp_path):
    cfg = config(tmp_path)

    def offline(request):
        raise httpx.ConnectError("secret-not-for-catalog", request=request)

    with pytest.raises(ConfigError, match="无法确认") as error:
        resolve_config(cfg, transport=httpx.MockTransport(offline))
    assert cfg.api_key not in str(error.value)
    online = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"id": cfg.model, "context_window": 64000, "max_output_tokens": 8000},
        )
    )
    expected = resolve_config(cfg, transport=online)
    assert resolve_config(cfg, transport=httpx.MockTransport(offline)) == expected
