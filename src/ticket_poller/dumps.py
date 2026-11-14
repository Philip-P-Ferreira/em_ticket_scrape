"""Redacted JSON dumps of captured responses: debug dumps on failures and ``--save`` fixtures."""

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from ticket_poller.config import ConfigError, Settings, ensure_private_dir
from ticket_poller.logsetup import redact
from ticket_poller.models import CapturedResponse

log = logging.getLogger(__name__)

_API_CREDENTIAL_RE: Final = re.compile(r"\b(apikey|apisecret)=[^&\"'\s]+", re.IGNORECASE)
DEBUG_DUMPS_KEPT: Final = 20
MAX_DUMP_BODY_CHARS: Final = 512_000
MAX_DUMP_HTML_CHARS: Final = 256_000


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask ismds ``apikey``/``apisecret`` query values and every configured secret."""
    return redact(_API_CREDENTIAL_RE.sub(r"\1=***", text), secrets)


def _scrubbed(value: Any, secrets: Iterable[str]) -> Any:
    """``value`` with every string in it scrubbed; done before encoding so JSON stays valid."""
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, Mapping):
        return {_scrubbed(k, secrets): _scrubbed(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrubbed(item, secrets) for item in value]
    return value


def _redacted_json(value: Any, secrets: Iterable[str]) -> str:
    return json.dumps(_scrubbed(value, tuple(secrets)), indent=1)


def _capped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated {len(text) - limit:,} characters]"


def _fixture(response: CapturedResponse, body_limit: int | None = None) -> dict[str, Any]:
    """The response in test-fixture form, with an over-long body replaced by a note."""
    body: Any = response.body
    if body_limit is not None and body is not None and len(json.dumps(body)) > body_limit:
        body = {"_truncated": f"body over {body_limit:,} characters"}
    return {"_source_url": response.url, "status": response.status, "response": body}


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)


def write_debug_dump(
    directory: Path,
    reason: str,
    responses: Sequence[CapturedResponse],
    *,
    html: str = "",
    title: str = "",
    secrets: Iterable[str] = (),
    now: datetime | None = None,
) -> Path:
    """Write a redacted, size-capped JSON dump to ``directory`` (0700); keep the newest 20."""
    ensure_private_dir(directory)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S.%fZ")
    slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")[:40] or "dump"
    path = directory / f"{stamp}-{slug}.json"
    dump = {
        "reason": reason,
        "title": title,
        "responses": [_fixture(r, MAX_DUMP_BODY_CHARS) for r in responses],
        "html": _capped(html, MAX_DUMP_HTML_CHARS),
    }
    _write_private(path, _redacted_json(dump, secrets))
    _prune_dumps(directory, DEBUG_DUMPS_KEPT)
    return path


def try_write_dump(
    settings: Settings,
    reason: str,
    responses: Sequence[CapturedResponse],
    *,
    html: str = "",
    title: str = "",
) -> Path | None:
    """``write_debug_dump`` into ``settings.debug_dir``, logging the outcome; never raises."""
    try:
        path = write_debug_dump(
            settings.debug_dir,
            reason,
            responses,
            html=html,
            title=title,
            secrets=settings.secret_values(),
        )
    except (ConfigError, OSError) as exc:
        log.warning("could not write a debug dump: %s", exc)
        return None
    log.info("debug dump written: %s", path)
    return path


def _prune_dumps(directory: Path, keep: int) -> None:
    dumps = sorted(directory.glob("*.json"), key=lambda p: p.name, reverse=True)
    for stale in dumps[keep:]:
        stale.unlink(missing_ok=True)


def save_responses(
    directory: Path, responses: Sequence[CapturedResponse], secrets: Iterable[str] = ()
) -> list[Path]:
    """Write each body as a test fixture (``{_source_url, status, response}``) for ``--save``.

    A new ``directory`` is created 0700; an existing one keeps its mode. Files are 0600.
    """
    secrets = tuple(secrets)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths: list[Path] = []
    for number, response in enumerate(responses, start=1):
        name = Path(urlsplit(response.url).path).name or "response"
        path = directory / f"{number:02d}-{name}-{response.status}.json"
        _write_private(path, _redacted_json(_fixture(response), secrets) + "\n")
        paths.append(path)
    return paths
