"""Typed settings from the environment and ``.env``, plus startup file-safety checks.

``load_settings()`` is the only code path that reads the env file, and it checks the
file's permissions first. An empty value (``KEY=``) means "none" for list settings
and optional secrets; text and path settings reject it, and any other setting validates
it as given instead of using the default.

``Settings`` must never be logged or formatted directly; use ``Settings.summary()``,
which shows only non-sensitive values and reports secrets as ``set``/``unset``.
"""

import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger(__name__)

ENV_FILE_VAR: Final = "ENV_FILE"
DEFAULT_ENV_FILE: Final = Path(".env")
MIN_SECRET_LENGTH: Final = 6
MAX_QUANTITY: Final = 8
PRIVATE_DIR_MODE: Final = 0o700
PRIVATE_UMASK: Final = 0o077

_EMAIL_RE: Final = re.compile(r"[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+")
_HTTP_URL_RE: Final = re.compile(r"https?://[^\s/]+\S*")

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# NoDecode: these are comma-separated in the environment, not JSON.
type Quantities = Annotated[frozenset[int], NoDecode]
type CsvStrings = Annotated[tuple[str, ...], NoDecode]


def _reject_blank(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        raise ValueError("must not be empty")
    return value


# Without these, ``KEY=`` would be accepted as "" (or as Path("."), the working directory).
type Text = Annotated[str, BeforeValidator(_reject_blank)]
type ConfigPath = Annotated[Path, BeforeValidator(_reject_blank), AfterValidator(Path.expanduser)]


class ConfigError(Exception):
    """Invalid configuration or an unsafe ``.env``; the CLI exits 78 (EX_CONFIG)."""


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """Alert-rule parameters with money in exact integer cents."""

    wanted_quantities: frozenset[int]
    preferred_quantities: frozenset[int]
    primary_alert_max_price_cents: int
    primary_drop_percent: int
    preferred_max_price_cents: int
    baseline_min_samples: int


def dollars_to_cents(amount: Decimal) -> int:
    """Convert a ``Decimal`` dollar amount to integer cents, rounding half-up."""
    return int((amount * 100).to_integral_value(ROUND_HALF_UP))


class Settings(BaseSettings):
    """All runtime configuration. Field ``foo_bar`` is read from env var ``FOO_BAR``."""

    model_config = SettingsConfigDict(
        env_file=None,  # load_settings() passes the permission-checked file
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
    )

    # Event
    event_id: Text = Field("2000655CAB442FB5", pattern=r"^[A-Za-z0-9]+$")
    event_url: Text = (
        "https://www.ticketmaster.com/noah-kahan-the-last-of-the-birmingham-alabama-04-27-2027"
        "/event/2000655CAB442FB5"
    )
    event_name: Text = "Noah Kahan — Protective Stadium, Birmingham AL"
    event_date: date = date(2027, 4, 27)
    timezone: Text = "America/Chicago"
    stop_after: datetime = datetime(2027, 4, 28)  # naive values are interpreted in `timezone`

    # Polling and backoff
    poll_interval_seconds: PositiveInt = 300
    poll_jitter_seconds: NonNegativeInt = 60
    error_backoff_max_seconds: PositiveInt = 1800
    block_backoff_base_seconds: PositiveInt = 900
    block_backoff_max_seconds: PositiveInt = 3600
    failure_alert_after_minutes: PositiveInt = 60

    # Alert rules (prices are per ticket, listPrice, in dollars)
    wanted_quantities: Quantities = frozenset({2, 3})
    preferred_quantities: Quantities = frozenset({3})
    primary_alert_max_price: Decimal = Field(Decimal("200.00"), gt=0, decimal_places=2)
    preferred_max_price: Decimal = Field(Decimal("300.00"), gt=0, decimal_places=2)
    primary_drop_percent: int = Field(20, ge=1, le=99)
    baseline_window_days: PositiveInt = 7
    baseline_min_samples: PositiveInt = 12
    realert_after_absent_hours: NonNegativeInt = 24
    canary_hour: int = Field(8, ge=0, le=23)

    # Browser
    chromium_path: ConfigPath = Path("/usr/bin/chromium")
    browser_profile_dir: ConfigPath = Path("~/.local/share/em_ticket_scrape/chromium-profile")
    browser_extra_args: CsvStrings = ("--ozone-platform=x11",)
    browser_recycle_hours: PositiveFloat = 6.0
    capture_timeout_seconds: PositiveFloat = 20.0
    capture_settle_seconds: NonNegativeFloat = 3.0

    # State
    state_dir: ConfigPath = Path("~/.local/share/em_ticket_scrape")

    # Email
    smtp_host: Text = "smtp.gmail.com"
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_timeout_seconds: PositiveFloat = 20.0
    sender_email: Text
    sender_app_password: SecretStr
    alert_recipients: CsvStrings = ("philip@ferreira-family.com",)
    admin_email: Text = "philip@ferreira-family.com"

    # Push and monitoring
    ntfy_server: Text = "https://ntfy.sh"
    ntfy_topic: SecretStr
    ntfy_ops_topic: SecretStr | None = None
    ntfy_token: SecretStr | None = None
    healthchecks_ping_url: SecretStr | None = None

    log_level: LogLevel = "INFO"

    # --- field validators -------------------------------------------------------------------

    @field_validator(
        "wanted_quantities",
        "preferred_quantities",
        "browser_extra_args",
        "alert_recipients",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("ntfy_ops_topic", "ntfy_token", "healthchecks_ping_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown IANA time zone") from None
        return value

    @field_validator("event_url", "ntfy_server")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not _HTTP_URL_RE.fullmatch(value):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator("sender_email", "admin_email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return _check_email(value)

    @field_validator("alert_recipients")
    @classmethod
    def _valid_recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one recipient is required")
        return tuple(_check_email(address) for address in value)

    @field_validator("wanted_quantities")
    @classmethod
    def _valid_wanted(cls, value: frozenset[int]) -> frozenset[int]:
        if not value:
            raise ValueError("at least one quantity is required")
        return _check_quantities(value)

    @field_validator("preferred_quantities")
    @classmethod
    def _valid_preferred(cls, value: frozenset[int]) -> frozenset[int]:
        return _check_quantities(value)

    @field_validator("sender_app_password")
    @classmethod
    def _valid_password(cls, value: SecretStr) -> SecretStr:
        _check_secret_length("".join(value.get_secret_value().split()))
        return value

    @field_validator("ntfy_topic", "ntfy_ops_topic", "ntfy_token")
    @classmethod
    def _valid_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            _check_secret_length(value.get_secret_value())
        return value

    @field_validator("healthchecks_ping_url")
    @classmethod
    def _valid_ping_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not _HTTP_URL_RE.fullmatch(value.get_secret_value()):
            raise ValueError("must be an http(s) URL")
        return value

    # --- cross-field validation -------------------------------------------------------------

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if not self.preferred_quantities <= self.wanted_quantities:
            raise ValueError("PREFERRED_QUANTITIES must be a subset of WANTED_QUANTITIES")
        if self.poll_jitter_seconds >= self.poll_interval_seconds:
            raise ValueError("POLL_JITTER_SECONDS must be less than POLL_INTERVAL_SECONDS")
        if self.block_backoff_base_seconds > self.block_backoff_max_seconds:
            raise ValueError("BLOCK_BACKOFF_BASE_SECONDS must not exceed BLOCK_BACKOFF_MAX_SECONDS")
        if self.stop_after.tzinfo is None:
            self.stop_after = self.stop_after.replace(tzinfo=self.tz)
        return self

    # --- derived values ---------------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def ops_topic(self) -> SecretStr:
        """The ntfy topic for admin/ops notices (defaults to the alert topic)."""
        return self.ntfy_ops_topic if self.ntfy_ops_topic is not None else self.ntfy_topic

    @property
    def debug_dir(self) -> Path:
        return self.state_dir / "debug"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "poller.lock"

    @property
    def private_dirs(self) -> tuple[Path, ...]:
        """Directories that must exist with mode 0700."""
        return (self.state_dir, self.debug_dir, self.browser_profile_dir)

    def rule_config(self) -> RuleConfig:
        """The alert-rule parameters, with dollars converted to cents once."""
        return RuleConfig(
            wanted_quantities=self.wanted_quantities,
            preferred_quantities=self.preferred_quantities,
            primary_alert_max_price_cents=dollars_to_cents(self.primary_alert_max_price),
            primary_drop_percent=self.primary_drop_percent,
            preferred_max_price_cents=dollars_to_cents(self.preferred_max_price),
            baseline_min_samples=self.baseline_min_samples,
        )

    def secret_values(self) -> tuple[str, ...]:
        """Every string that must never appear in logs (consumed by the redacting formatter)."""
        password = self.sender_app_password.get_secret_value()
        values = [password, "".join(password.split())]
        for secret in (self.ntfy_topic, self.ntfy_ops_topic, self.ntfy_token):
            if secret is not None:
                values.append(secret.get_secret_value())
        if self.healthchecks_ping_url is not None:
            url = self.healthchecks_ping_url.get_secret_value()
            values.append(url)
            # The UUID / ping key / slug path segments identify the check on their own.
            segments = urlsplit(url).path.split("/")
            values.extend(s for s in segments if len(s) >= MIN_SECRET_LENGTH)
        return tuple(dict.fromkeys(v for v in values if v))

    def summary(self) -> str:
        """A loggable ``KEY=value`` view of the configuration; secrets show as set/unset."""
        return "\n".join(
            f"{name.upper()}={self._display(name)}" for name in type(self).model_fields
        )

    def _display(self, name: str) -> str:
        value: object = getattr(self, name)
        if value is None:
            return "unset (uses NTFY_TOPIC)" if name == "ntfy_ops_topic" else "unset"
        if isinstance(value, SecretStr):
            return "set"
        match value:
            case frozenset():
                return ",".join(str(item) for item in sorted(value))
            case tuple():
                return ",".join(str(item) for item in value)
            case datetime():
                return value.isoformat()
            case _:
                return str(value)


def _check_email(value: str) -> str:
    address = value.strip()
    if not _EMAIL_RE.fullmatch(address):
        raise ValueError("invalid email address")
    return address


def _check_quantities(value: frozenset[int]) -> frozenset[int]:
    if not all(1 <= quantity <= MAX_QUANTITY for quantity in value):
        raise ValueError(f"quantities must be between 1 and {MAX_QUANTITY}")
    return value


def _check_secret_length(value: str) -> None:
    if len(value) < MIN_SECRET_LENGTH:
        # Short secrets would make log redaction mask common substrings.
        raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters")


def _format_validation_error(exc: ValidationError) -> str:
    """Describe what is wrong without echoing any (possibly secret) input value."""
    problems = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]).upper() or "settings"
        problems.append(f"{location}: {error['msg']}")
    return "invalid configuration: " + "; ".join(problems)


def check_env_file(path: Path) -> None:
    """Require an existing env file to be owned by us and not group/world accessible."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError(f"{path} is group/world-readable or not yours; run: chmod 600 {path}")


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate settings from the process environment, then the env file.

    The env file is ``env_file``, else ``$ENV_FILE``, else ``./.env``. A missing default
    ``.env`` is allowed (everything may come from the environment); an explicit one is not.
    Raises ``ConfigError`` on any problem.
    """
    explicit = env_file if env_file is not None else os.environ.get(ENV_FILE_VAR)
    path = Path(explicit).expanduser() if explicit else DEFAULT_ENV_FILE
    if explicit and not path.is_file():
        raise ConfigError(f"env file not found: {path}")
    try:
        check_env_file(path)
        return Settings(_env_file=path)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc.strerror}") from exc


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` with mode 0700, or tighten an existing directory to 0700.

    Raises ``ConfigError`` if ``path`` is not a directory or cannot be created or tightened.
    """
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != PRIVATE_DIR_MODE:
            path.chmod(PRIVATE_DIR_MODE)
            if mode & 0o077:
                log.warning("tightened permissions on %s from %03o to 700", path, mode)
    except OSError as exc:
        reason = "not a directory" if isinstance(exc, FileExistsError) else exc.strerror
        raise ConfigError(f"cannot use private directory {path}: {reason}") from exc


def secure_runtime(settings: Settings) -> None:
    """Apply ``umask 077`` and make sure the state, debug and profile dirs are private.

    Raises ``ConfigError`` if a directory cannot be made private.
    """
    os.umask(PRIVATE_UMASK)
    for directory in settings.private_dirs:
        ensure_private_dir(directory)
