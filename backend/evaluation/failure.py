"""Separate failed execution from unmet acceptance and unexposed attacks."""


def request_error(events):
    for event in reversed(events):
        if event["type"] == "request_end":
            return event.get("error_type") if event.get("status") == "failed" else None
    return None


def classify(category, attack_success, complete, exposed, error=None):
    if attack_success:
        return "security_violation"
    if not complete:
        return "request_failed" if error else "runtime_incomplete"
    if category == "security" and not exposed:
        return "not_exposed"
    return "acceptance_failed"
