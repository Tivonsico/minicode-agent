"""Context decisions use provider tokens rather than serialized UTF-8 bytes."""
from types import SimpleNamespace

import httpx
import pytest

from backend.config import Config
from backend.context import ContextManager
from backend.runtime.provider import Provider, ProviderError


class Store:
    def __init__(self):
        self.events = []

    def append(self, kind, **data):
        self.events.append({'type': kind, **data})


class CountedProvider:
    async def count_tokens(self, messages, tools):
        return 100

    async def complete(self, *args, **kwargs):
        raise AssertionError('100 tokens must not trigger compression at 80% of 1000')


@pytest.mark.asyncio
async def test_provider_tokens_control_context_threshold_instead_of_bytes():
    store = Store()
    manager = ContextManager(
        SimpleNamespace(context_window=1100, max_output_tokens=100), CountedProvider(), store
    )
    messages = [{'role': 'user', 'content': '国际象棋' * 1000}]
    assert await manager.prepare(messages, []) == messages
    assert manager.last_estimate == 100
    assert store.events[-1]['method'] == 'provider_tokenizer'
    assert store.events[-1]['token_count'] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('tokens,compressions', [(799, 0), (800, 1)])
async def test_normal_eighty_percent_boundary(tokens, compressions):
    class BoundaryProvider(CountedProvider):
        calls = 0

        async def count_tokens(self, messages, tools):
            return tokens if not self.calls else 200

        async def complete(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(content='Earlier work summarized.', tool_calls=[],
                                   finish_reason='stop', usage=None)

    provider = BoundaryProvider()
    manager = ContextManager(
        SimpleNamespace(context_window=1100, max_output_tokens=100), provider, Store()
    )
    messages = [{'role': 'user', 'content': 'Keep the chess requirements.'}]
    messages += [{'role': 'assistant', 'content': f'progress {i}'} for i in range(8)]
    result = await manager.prepare(messages, [])
    assert provider.calls == compressions
    assert messages[0] in result
    assert result[-4:] == messages[-4:]


@pytest.mark.asyncio
async def test_official_tokenizer_receives_exact_model_messages_and_tools(tmp_path):
    import json

    messages = [{'role': 'user', 'content': '国际象棋'}]
    tools = [{'type': 'function', 'function': {'name': 'ReadFile'}}]

    def handle(request):
        assert str(request.url) == 'https://open.bigmodel.cn/api/paas/v4/tokenizer'
        assert json.loads(request.content) == {
            'model': 'GLM-5.3-Flash', 'messages': messages, 'tools': tools
        }
        return httpx.Response(200, json={'usage': {'prompt_tokens': 160}})

    config = Config(root=tmp_path, api_key='test', model='GLM-5.3-Flash',
                    base_url='https://open.bigmodel.cn/api/paas/v4/')
    provider = Provider(config, transport=httpx.MockTransport(handle))
    assert await provider.count_tokens(messages, tools) == 160


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [httpx.Response(503),
                                    httpx.Response(200, json={'usage': {'prompt_tokens': True}})])
async def test_failed_tokenizer_does_not_fall_back_to_bytes(tmp_path, response):
    config = Config(root=tmp_path, api_key='test', model='GLM-5.3-Flash',
                    context_window=1000000, max_output_tokens=16384,
                    base_url='https://open.bigmodel.cn/api/paas/v4/')
    provider = Provider(config, transport=httpx.MockTransport(lambda request: response))
    manager = ContextManager(config, provider, Store())
    with pytest.raises(ProviderError, match='token'):
        await manager.prepare([{'role': 'user', 'content': 'hello'}], [])
