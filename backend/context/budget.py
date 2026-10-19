"""Conservative, tokenizer-independent context estimates (not billed usage)."""

import json


def estimate(messages, tools=()):
    # One token per UTF-8 byte is deliberately conservative across languages.
    return (
        len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8"))
        + 32
    )


def input_budget(config):
    budget = config.context_window - config.max_output_tokens
    maximum_input = getattr(config, "model_max_input", None)
    if maximum_input is not None:
        budget = min(budget, maximum_input)
    if budget <= 0:
        raise ValueError("Context window must exceed reserved output tokens")
    return budget
