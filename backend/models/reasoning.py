"""Official reasoning levels; unsupported models never receive invented settings."""

from backend.observability.scope import current

SOURCE = "https://github.com/zai-org/GLM-5/blob/main/README_zh.md"
GLM_LEVELS = (
    {"value": "low", "label": "轻量（low）"},
    {"value": "high", "label": "增强（high）"},
    {"value": "max", "label": "深度（max）"},
)


def policy(config):
    if config.model.casefold() in {"glm-5.3", "glm-5.3-flash"}:
        return {"levels": list(GLM_LEVELS), "default": "max", "source": SOURCE}
    return {"levels": [], "default": None, "source": None}


def validate(config, value):
    settings = policy(config)
    if value is None:
        return settings["default"]
    if value not in {item["value"] for item in settings["levels"]}:
        raise ValueError("该模型不支持此思考档位，请选择模型官方提供的档位")
    return value


def request_settings(config):
    value = validate(config, current().get("reasoning_effort"))
    return {"reasoning_effort": value} if value is not None else {}
