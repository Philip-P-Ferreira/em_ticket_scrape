"""Fake but realistic configuration values shared by the tests."""

from collections.abc import Callable
from pathlib import Path

# The redaction tests check that none of these secrets leak.
APP_PASSWORD = "abcd efgh ijkl mnop"
NTFY_TOPIC = "nk-alerts-q7f3x9z2"
NTFY_OPS_TOPIC = "nk-ops-k2m8p4w6"
NTFY_TOKEN = "tk_fake0123456789token"
HC_UUID = "3f1c2b7e-9a4d-4e6b-8c2f-1d5e7a9b0c3d"
HC_URL = f"https://hc-ping.com/{HC_UUID}"
SENDER_EMAIL = "sender@example.com"

BASE_ENV: dict[str, str] = {
    "SENDER_EMAIL": SENDER_EMAIL,
    "SENDER_APP_PASSWORD": f'"{APP_PASSWORD}"',
    "NTFY_TOPIC": NTFY_TOPIC,
    "NTFY_OPS_TOPIC": NTFY_OPS_TOPIC,
    "NTFY_TOKEN": NTFY_TOKEN,
    "HEALTHCHECKS_PING_URL": HC_URL,
}

type EnvFileFactory = Callable[..., Path]
