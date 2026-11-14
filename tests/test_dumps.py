import json
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.builders import EVENT_API, load_captured
from tests.sample_env import NTFY_TOPIC, EnvFileFactory
from ticket_poller.config import Settings, load_settings
from ticket_poller.dumps import save_responses, try_write_dump, write_debug_dump
from ticket_poller.models import CapturedResponse

OFFER_FACETS = load_captured("nk_facets_inventorytypes_offer")
QP2 = load_captured("nk_quickpicks_qty2")
DYNAMIC_BLOCK = CapturedResponse(
    f"{EVENT_API}/quickpicks?qty=2", 403, {"response": "dynamic_block"}
)


@pytest.fixture
def settings(make_env_file: EnvFileFactory) -> Settings:
    make_env_file()
    return load_settings()


class TestDumps:
    def test_debug_dump_is_private_redacted_and_capped(self, settings: Settings) -> None:
        secret_url = f"{QP2.url}&apikey=PUBLICKEY&apisecret=SECRETVALUE&topic={NTFY_TOPIC}"
        response = CapturedResponse(secret_url, 200, {"big": "x" * 600_000})
        html = f"<p>{NTFY_TOPIC}</p>" + "y" * 300_000

        path = write_debug_dump(
            settings.debug_dir, "parse: bad", [response], html=html, secrets=[NTFY_TOPIC]
        )

        text = path.read_text(encoding="utf-8")
        for secret in ("PUBLICKEY", "SECRETVALUE", NTFY_TOPIC):
            assert secret not in text
        assert "apikey=***&apisecret=***" in text
        assert "body over 512,000 characters" in text
        assert "[truncated" in text
        assert len(text) < 300_000
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(settings.debug_dir.stat().st_mode) == 0o700

    def test_redaction_keeps_the_json_valid(self, tmp_path: Path) -> None:
        quoted = CapturedResponse(QP2.url, 200, {"link": 'https://x/?apikey=PUBLICKEY"&q=1'})

        dump = write_debug_dump(tmp_path / "debug", "blocked", [quoted])
        (saved,) = save_responses(tmp_path / "saved", [quoted])

        assert json.loads(dump.read_text())["responses"][0]["response"] == {
            "link": 'https://x/?apikey=***"&q=1'
        }
        assert json.loads(saved.read_text())["response"] == {"link": 'https://x/?apikey=***"&q=1'}

    def test_keeps_the_newest_twenty(self, tmp_path: Path) -> None:
        directory = tmp_path / "debug"
        start = datetime(2026, 11, 14, tzinfo=UTC)
        for minute in range(23):
            write_debug_dump(directory, "timeout", [], now=start + timedelta(minutes=minute))

        names = sorted(p.name for p in directory.iterdir())
        assert len(names) == 20
        assert names[0].startswith("20261114T000300")

    def test_saved_responses_load_as_fixtures(self, tmp_path: Path) -> None:
        keyed = CapturedResponse(f"{QP2.url}&apikey=PUBLICKEY", 200, QP2.body)

        paths = save_responses(tmp_path / "dump", [OFFER_FACETS, keyed, DYNAMIC_BLOCK])

        assert [p.name for p in paths] == [
            "01-facets-200.json",
            "02-quickpicks-200.json",
            "03-quickpicks-403.json",
        ]
        text = paths[1].read_text(encoding="utf-8")
        assert "PUBLICKEY" not in text
        assert json.loads(text).keys() == {"_source_url", "status", "response"}
        assert stat.S_IMODE((tmp_path / "dump").stat().st_mode) == 0o700

    def test_saving_into_an_existing_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        directory = tmp_path / "shared"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)

        (path,) = save_responses(directory, [QP2])

        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestTryWriteDump:
    def test_writes_into_the_debug_dir_with_secrets_redacted(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        response = CapturedResponse(f"{QP2.url}&topic={NTFY_TOPIC}", 200, {})

        path = try_write_dump(settings, "blocked", [response], html="<p/>", title="Paused")

        assert path is not None
        assert path.parent == settings.debug_dir
        dump = json.loads(path.read_text(encoding="utf-8"))
        assert (dump["title"], dump["html"]) == ("Paused", "<p/>")
        assert NTFY_TOPIC not in path.read_text(encoding="utf-8")
        assert f"debug dump written: {path}" in caplog.text

    def test_an_unwritable_debug_dir_only_warns(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings.debug_dir.parent.mkdir(parents=True, exist_ok=True)
        settings.debug_dir.write_text("not a directory")

        assert try_write_dump(settings, "blocked", []) is None
        assert "could not write a debug dump" in caplog.text
