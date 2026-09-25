import json
import logging
import re

LOG = logging.getLogger("qq-codex-agent")


def log_text(value):
    text = (
        f"{type(value).__name__}: {value}"
        if isinstance(value, BaseException)
        else str(value)
    )
    text = re.sub(
        r"(?:data:image/[^;,\s]+;base64,|base64://)[A-Za-z0-9+/=]+", "[image]", text
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)(\b(?:access_token|refresh_token|id_token|api_key|token|authorization|cookie|password)[\"'\s]*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1[redacted]",
        text,
    )
    return json.dumps(text, ensure_ascii=False)
