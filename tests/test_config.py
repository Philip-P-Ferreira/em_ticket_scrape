import errno
import logging
import os
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tests.sample_env import (
    APP_PASSWORD,
    HC_URL,
    HC_UUID,
    NTFY_OPS_TOPIC,
    NTFY_TOKEN,
    NTFY_TOPIC,
    SENDER_EMAIL,
    EnvFileFactory,
)
from ticket_poller.config import (
    ConfigError,
    ConfigPath,
    RuleConfig,
    Settings,
    Text,
    check_env_file,
    dollars_to_cents,
    ensure_private_dir,
    load_settings,
    secure_runtime,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


_NON_EMPTY_TYPES: tuple[object, ...] = (Text, ConfigPath)
_NON_EMPTY_KEYS = [
    name.upper()
    for name, info in Settings.model_fields.items()
    if info.annotation in _NON_EMPTY_TYPES
]


class TestDefaults:
    def test_defaults_match_design(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file())

        assert settings.event_id == "2000655CAB442FB5"
        assert settings.event_url.endswith("/event/2000655CAB442FB5")
        assert settings.wanted_quantities == frozenset({2, 3})
        assert settings.preferred_quantities == frozenset({3})
        assert settings.poll_interval_seconds == 300
        assert settings.smtp_port == 587
        assert settings.alert_recipients == ("philip@ferreira-family.com",)
        assert settings.browser_extra_args == ("--ozone-platform=x11",)
        assert settings.ops_topic.get_secret_value() == NTFY_OPS_TOPIC

    def test_stop_after_is_localized_to_timezone(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file())

        assert settings.stop_after == datetime(2027, 4, 28, tzinfo=ZoneInfo("America/Chicago"))

    def test_rule_config_in_cents(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file())

        assert settings.rule_config() == RuleConfig(
            wanted_quantities=frozenset({2, 3}),
            preferred_quantities=frozenset({3}),
            primary_alert_max_price_cents=20000,
            primary_drop_percent=20,
            preferred_max_price_cents=30000,
            baseline_min_samples=12,
        )

    def test_ops_topic_defaults_to_alert_topic(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file(NTFY_OPS_TOPIC=None))

        assert settings.ops_topic.get_secret_value() == NTFY_TOPIC

    def test_tilde_is_expanded(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file(STATE_DIR="~/somewhere"))

        assert settings.state_dir == Path.home() / "somewhere"


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "cents"), [("300.00", 30000), ("300", 30000), ("0.01", 1), ("383.5", 38350)]
    )
    def test_prices_to_cents(self, make_env_file: EnvFileFactory, raw: str, cents: int) -> None:
        settings = load_settings(make_env_file(PREFERRED_MAX_PRICE=raw))

        assert settings.rule_config().preferred_max_price_cents == cents

    @pytest.mark.parametrize(
        ("amount", "cents"),
        [(Decimal("1.005"), 101), (Decimal("343.50"), 34350), (Decimal(2), 200)],
    )
    def test_dollars_to_cents_rounds_half_up(self, amount: Decimal, cents: int) -> None:
        assert dollars_to_cents(amount) == cents

    def test_comma_separated_lists(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(
            make_env_file(
                WANTED_QUANTITIES=" 2, 3 ",
                PREFERRED_QUANTITIES="3,2",
                ALERT_RECIPIENTS="a@example.com, b@example.org",
                BROWSER_EXTRA_ARGS="--ozone-platform=x11,--lang=en-US",
            )
        )

        assert settings.wanted_quantities == frozenset({2, 3})
        assert settings.preferred_quantities == frozenset({2, 3})
        assert settings.alert_recipients == ("a@example.com", "b@example.org")
        assert settings.browser_extra_args == ("--ozone-platform=x11", "--lang=en-US")

    def test_process_environment_overrides_env_file(
        self, make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = make_env_file(POLL_INTERVAL_SECONDS="300")
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "120")

        assert load_settings(path).poll_interval_seconds == 120

    def test_env_file_from_env_var(
        self, make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = make_env_file().rename(tmp_path / "other.env")
        monkeypatch.setenv("ENV_FILE", str(other))

        assert load_settings().ntfy_topic.get_secret_value() == NTFY_TOPIC

    def test_default_env_file_in_working_directory(self, make_env_file: EnvFileFactory) -> None:
        make_env_file()

        assert load_settings().sender_email == SENDER_EMAIL

    @pytest.mark.parametrize("key", ["NTFY_OPS_TOPIC", "NTFY_TOKEN", "HEALTHCHECKS_PING_URL"])
    def test_empty_optional_secret_is_unset(self, make_env_file: EnvFileFactory, key: str) -> None:
        settings = load_settings(make_env_file(**{key: ""}))

        assert getattr(settings, key.lower()) is None

    def test_empty_optional_secret_in_process_environment_is_unset(
        self, make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = make_env_file()
        monkeypatch.setenv("NTFY_TOKEN", "")

        assert load_settings(path).ntfy_token is None

    def test_empty_preferred_quantities_means_none(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file(PREFERRED_QUANTITIES=""))

        assert settings.preferred_quantities == frozenset()
        assert settings.rule_config().preferred_quantities == frozenset()

    def test_empty_browser_extra_args_means_none(self, make_env_file: EnvFileFactory) -> None:
        assert load_settings(make_env_file(BROWSER_EXTRA_ARGS="")).browser_extra_args == ()

    def test_settings_alone_does_not_read_env_file(self, make_env_file: EnvFileFactory) -> None:
        make_env_file()

        with pytest.raises(ValidationError, match="sender_email"):
            Settings()


class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "problem"),
        [
            ({"WANTED_QUANTITIES": "2", "PREFERRED_QUANTITIES": "3"}, "subset"),
            ({"WANTED_QUANTITIES": "0,2"}, "only quantities 2 and 3 are supported"),
            ({"WANTED_QUANTITIES": "2,4"}, "only quantities 2 and 3 are supported"),
            ({"WANTED_QUANTITIES": "1"}, "only quantities 2 and 3 are supported"),
            ({"PREFERRED_QUANTITIES": "4"}, "only quantities 2 and 3 are supported"),
            ({"WANTED_QUANTITIES": ","}, "at least one quantity"),
            ({"WANTED_QUANTITIES": ""}, "at least one quantity"),
            ({"ALERT_RECIPIENTS": ""}, "at least one recipient"),
            ({"POLL_INTERVAL_SECONDS": ""}, "POLL_INTERVAL_SECONDS"),
            ({"WANTED_QUANTITIES": "two"}, "WANTED_QUANTITIES"),
            ({"PRIMARY_ALERT_MAX_PRICE": "0"}, "PRIMARY_ALERT_MAX_PRICE"),
            ({"PREFERRED_MAX_PRICE": "-5"}, "PREFERRED_MAX_PRICE"),
            ({"PREFERRED_MAX_PRICE": "300.001"}, "PREFERRED_MAX_PRICE"),
            ({"PRIMARY_DROP_PERCENT": "0"}, "PRIMARY_DROP_PERCENT"),
            ({"PRIMARY_DROP_PERCENT": "100"}, "PRIMARY_DROP_PERCENT"),
            ({"SENDER_EMAIL": "not-an-email"}, "SENDER_EMAIL"),
            ({"ADMIN_EMAIL": "a@b"}, "ADMIN_EMAIL"),
            ({"ALERT_RECIPIENTS": "ok@example.com,bad"}, "ALERT_RECIPIENTS"),
            ({"TIMEZONE": "Mars/Base"}, "unknown IANA time zone"),
            ({"CANARY_HOUR": "24"}, "CANARY_HOUR"),
            ({"POLL_JITTER_SECONDS": "300"}, "POLL_JITTER_SECONDS"),
            ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
            ({"NTFY_SERVER": "ntfy.sh"}, "http(s) URL"),
            ({"HEALTHCHECKS_PING_URL": "hc-ping.com/abcdef"}, "http(s) URL"),
            ({"SENDER_APP_PASSWORD": None}, "SENDER_APP_PASSWORD"),
            ({"NTFY_TOPIC": None}, "NTFY_TOPIC"),
            ({"NTFY_TOPIC": ""}, "NTFY_TOPIC"),
        ],
    )
    def test_invalid_settings_raise_config_error(
        self, make_env_file: EnvFileFactory, overrides: dict[str, str | None], problem: str
    ) -> None:
        with pytest.raises(ConfigError, match=problem.replace("(", r"\(").replace(")", r"\)")):
            load_settings(make_env_file(**overrides))

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("NTFY_TOPIC", "tiny5"),
            ("NTFY_TOKEN", "tok12"),
            ("SENDER_APP_PASSWORD", "ab cd e"),  # 7 characters, 5 without spaces
        ],
    )
    def test_short_secrets_rejected_without_echoing_them(
        self, make_env_file: EnvFileFactory, key: str, value: str
    ) -> None:
        with pytest.raises(ConfigError, match="at least 6 characters") as caught:
            load_settings(make_env_file(**{key: value}))

        assert value not in str(caught.value)
        assert caught.value.__cause__ is None

    def test_every_text_and_path_setting_uses_a_non_empty_type(self) -> None:
        bare = [
            name for name, info in Settings.model_fields.items() if info.annotation in (str, Path)
        ]

        assert not bare
        assert {"EVENT_NAME", "EVENT_URL", "CHROMIUM_PATH", "STATE_DIR"} <= set(_NON_EMPTY_KEYS)

    @pytest.mark.parametrize("value", ["", "   "])
    @pytest.mark.parametrize("key", _NON_EMPTY_KEYS)
    def test_empty_text_or_path_rejected(
        self, make_env_file: EnvFileFactory, key: str, value: str
    ) -> None:
        with pytest.raises(ConfigError, match=f"{key}: Value error, must not be empty"):
            load_settings(make_env_file(**{key: value}))

    def test_empty_path_in_process_environment_rejected(
        self, make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = make_env_file()
        monkeypatch.setenv("CHROMIUM_PATH", "")

        with pytest.raises(ConfigError, match="CHROMIUM_PATH: Value error, must not be empty"):
            load_settings(path)

    def test_explicit_missing_env_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_settings(tmp_path / "missing.env")

    def test_missing_default_env_file_uses_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENDER_EMAIL", SENDER_EMAIL)
        monkeypatch.setenv("SENDER_APP_PASSWORD", APP_PASSWORD)
        monkeypatch.setenv("NTFY_TOPIC", NTFY_TOPIC)

        assert load_settings().ntfy_topic.get_secret_value() == NTFY_TOPIC


class TestEnvFilePermissions:
    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
    def test_group_or_world_access_rejected(self, make_env_file: EnvFileFactory, mode: int) -> None:
        with pytest.raises(ConfigError, match="chmod 600"):
            load_settings(make_env_file(mode=mode))

    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_owner_only_accepted(self, make_env_file: EnvFileFactory, mode: int) -> None:
        check_env_file(make_env_file(mode=mode))

    def test_foreign_owner_rejected(
        self, make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = make_env_file()
        monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)

        with pytest.raises(ConfigError, match="not yours"):
            check_env_file(path)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-0000 file")
    def test_unreadable_env_file(self, make_env_file: EnvFileFactory) -> None:
        path = make_env_file(mode=0o000)

        with pytest.raises(ConfigError, match=f"cannot read env file {path}: Permission denied"):
            load_settings(path)


class TestPrivateDirs:
    def test_secure_runtime_creates_private_dirs(
        self, make_env_file: EnvFileFactory, tmp_path: Path
    ) -> None:
        settings = load_settings(make_env_file(STATE_DIR=str(tmp_path / "a" / "state")))

        secure_runtime(settings)

        for directory in (settings.state_dir, settings.debug_dir, settings.browser_profile_dir):
            assert _mode(directory) == 0o700
        assert _mode(tmp_path / "a") == 0o700  # parents are created under umask 077
        assert os.umask(0o077) == 0o077

    def test_loose_dir_is_tightened_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = tmp_path / "loose"
        directory.mkdir()
        directory.chmod(0o755)

        with caplog.at_level(logging.WARNING):
            ensure_private_dir(directory)

        assert _mode(directory) == 0o700
        assert "tightened permissions" in caplog.text

    def test_private_dir_is_left_alone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = tmp_path / "private"
        directory.mkdir(mode=0o700)

        ensure_private_dir(directory)

        assert _mode(directory) == 0o700
        assert not caplog.records

    @pytest.mark.parametrize("key", ["STATE_DIR", "BROWSER_PROFILE_DIR"])
    def test_dir_that_is_a_file(
        self, make_env_file: EnvFileFactory, tmp_path: Path, key: str
    ) -> None:
        target = tmp_path / "a-file"
        target.touch()
        settings = load_settings(make_env_file(**{key: str(target)}))

        with pytest.raises(ConfigError, match=f"{target}: not a directory"):
            secure_runtime(settings)

    def test_dir_under_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "a-file").touch()

        with pytest.raises(ConfigError, match="a-file/sub: Not a directory"):
            ensure_private_dir(tmp_path / "a-file" / "sub")

    def test_foreign_dir_that_cannot_be_tightened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "foreign"
        directory.mkdir(mode=0o755)

        def deny(self: Path, mode: int) -> None:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(self))

        monkeypatch.setattr(Path, "chmod", deny)

        with pytest.raises(ConfigError, match=f"{directory}: Operation not permitted"):
            ensure_private_dir(directory)


class TestSecretHygiene:
    def test_summary_contains_no_secrets(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(make_env_file())

        summary = settings.summary()

        for secret in (*settings.secret_values(), HC_UUID):
            assert secret not in summary
        assert "SENDER_APP_PASSWORD=set" in summary
        assert "NTFY_TOPIC=set" in summary
        assert "HEALTHCHECKS_PING_URL=set" in summary
        assert f"SENDER_EMAIL={SENDER_EMAIL}" in summary
        assert "WANTED_QUANTITIES=2,3" in summary
        assert "PREFERRED_MAX_PRICE=300.00" in summary

    def test_summary_reports_unset_optionals(self, make_env_file: EnvFileFactory) -> None:
        settings = load_settings(
            make_env_file(NTFY_OPS_TOPIC=None, NTFY_TOKEN=None, HEALTHCHECKS_PING_URL=None)
        )

        summary = settings.summary()

        assert "NTFY_OPS_TOPIC=unset (uses NTFY_TOPIC)" in summary
        assert "NTFY_TOKEN=unset" in summary
        assert "HEALTHCHECKS_PING_URL=unset" in summary

    def test_repr_does_not_reveal_secrets(self, make_env_file: EnvFileFactory) -> None:
        text = repr(load_settings(make_env_file()))

        for secret in (APP_PASSWORD, NTFY_TOPIC, NTFY_OPS_TOPIC, NTFY_TOKEN, HC_URL):
            assert secret not in text

    def test_secret_values_cover_all_forms(self, make_env_file: EnvFileFactory) -> None:
        values = set(load_settings(make_env_file()).secret_values())

        assert values == {
            APP_PASSWORD,
            APP_PASSWORD.replace(" ", ""),
            NTFY_TOPIC,
            NTFY_OPS_TOPIC,
            NTFY_TOKEN,
            HC_URL,
            HC_UUID,
        }

    def test_ping_url_slug_segments_are_secret(self, make_env_file: EnvFileFactory) -> None:
        url = "https://hc-ping.com/pingkey123456/ticket-poller"

        values = load_settings(make_env_file(HEALTHCHECKS_PING_URL=url)).secret_values()

        assert {url, "pingkey123456", "ticket-poller"} <= set(values)


def test_env_example_is_valid(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / ".env.example"
    copy = tmp_path / "example.env"
    copy.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    copy.chmod(0o600)

    settings = load_settings(copy)

    assert settings.ntfy_topic.get_secret_value().startswith("change-me")
    assert settings.healthchecks_ping_url is None
