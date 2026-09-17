class RequestLimit(RuntimeError):
    """Raised before sending a request exceeding the turn budget."""
