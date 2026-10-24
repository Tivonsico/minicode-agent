"""Experimental exact-span protection; no runtime default or model extraction.

Required spans must come from an explicit trusted constraint source. This does
not discover missing requirements, resolve rule revisions, or guarantee meaning.
All spans must be present in the currently visible state: no hidden restoration.
"""


class UnsafeCompaction(ValueError):
    pass


def compact_preserving_spans(text, required_spans, *, max_chars):
    spans = list(dict.fromkeys(required_spans))
    if any(not isinstance(s, str) or not s.strip() for s in spans):
        raise ValueError("Required spans must be nonempty strings")
    if any(s not in text for s in spans):
        raise UnsafeCompaction("Required span absent from current visible input")
    output = "\n".join(spans)
    if len(output) > max_chars:
        raise UnsafeCompaction("Required spans exceed character budget")
    for line in text.splitlines():
        if line in spans or not line.strip():
            continue
        candidate = output + ("\n" if output else "") + line
        if len(candidate) <= max_chars:
            output = candidate
    return output
