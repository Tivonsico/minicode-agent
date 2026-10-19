"""Validated limits and the source that supplied them."""

from dataclasses import dataclass


def positive_int(value):
    return value if type(value) is int and value > 0 else None


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output: int | None
    max_input: int | None
    source: str

    def __post_init__(self):
        if not positive_int(self.context_window):
            raise ValueError("Invalid model context window")
        for value in (self.max_output, self.max_input):
            if value is not None and not positive_int(value):
                raise ValueError("Invalid model token limit")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("Missing capability source")

    @classmethod
    def from_metadata(cls, data, source):
        if not isinstance(data, dict):
            return None
        limits = data.get("limit", {})
        provider = data.get("top_provider", {})
        if not isinstance(limits, dict) or not isinstance(provider, dict):
            return None
        context = next(
            (
                positive_int(v)
                for v in (
                    data.get("context_window"),
                    data.get("context_length"),
                    data.get("max_context_tokens"),
                    limits.get("context"),
                    provider.get("context_length"),
                )
                if positive_int(v)
            ),
            None,
        )
        if context is None:
            return None
        output = next(
            (
                positive_int(v)
                for v in (
                    data.get("max_output_tokens"),
                    data.get("max_completion_tokens"),
                    limits.get("output"),
                    provider.get("max_completion_tokens"),
                )
                if positive_int(v)
            ),
            None,
        )
        input_limit = positive_int(data.get("max_input_tokens")) or positive_int(
            limits.get("input")
        )
        return cls(context, output, input_limit, source)
