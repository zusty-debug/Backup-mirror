from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.database import Database
from app.models import MediaType, Project
from app.worker import BackupWorker, ScanProgress


class FakeTelegramClient:
    async def download_media(self, message, file: str):
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-media")
        return str(path)


@pytest.mark.asyncio
async def test_download_uses_original_filename_inside_unique_temp_directory() -> None:
    with TemporaryDirectory() as root_text:
        root = Path(root_text)
        settings = Settings(
            BOT_TOKEN="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            TELEGRAM_API_ID=12345,
            TELEGRAM_API_HASH="a" * 32,
            ENCRYPTION_KEY=Fernet.generate_key().decode(),
            OWNER_IDS="1001",
            DATA_DIR=root,
            DATABASE_PATH=root / "app.db",
            TEMP_DIR=root / "tmp",
            REPORT_DIR=root / "reports",
        )
        settings.prepare_directories()
        database = Database(settings.database_path)
        database.initialize()
        database.ensure_user(1001)
        project = Project.draft(
            owner_id=1001,
            profile_id=database.ensure_profile(1001),
            name="Download test",
            source_ref="source",
            destination_ref="destination",
        )
        project.source_chat_id = 111
        database.create_project(project)
        worker = BackupWorker(settings, database, gateway=None, bot=None)  # type: ignore[arg-type]
        message = SimpleNamespace(
            id=5,
            file=SimpleNamespace(name="Original Name.pdf", size=10, ext=".pdf"),
            message="ignored caption",
        )

        item = await worker._download_media(  # noqa: SLF001 - focused worker behavior test
            project, FakeTelegramClient(), message, MediaType.DOCUMENT, ScanProgress()
        )
        assert item.path.name == "Original Name.pdf"
        assert item.path.exists()
        worker._delete_downloads([item])  # noqa: SLF001
        assert not item.path.exists()
        database.close()

class FakeServerCopyClient:
    def __init__(self) -> None:
        self.calls = []

    async def send_file(self, destination, media, **kwargs):
        self.calls.append((destination, media, kwargs))
        return SimpleNamespace(id=999)


@pytest.mark.asyncio
async def test_server_copy_reuses_message_media_without_downloading() -> None:
    with TemporaryDirectory() as root_text:
        root = Path(root_text)
        settings = Settings(
            BOT_TOKEN="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            TELEGRAM_API_ID=12345,
            TELEGRAM_API_HASH="a" * 32,
            ENCRYPTION_KEY=Fernet.generate_key().decode(),
            OWNER_IDS="1001",
            DATA_DIR=root,
            DATABASE_PATH=root / "app.db",
            TEMP_DIR=root / "tmp",
            REPORT_DIR=root / "reports",
        )
        settings.prepare_directories()
        database = Database(settings.database_path)
        database.initialize()
        database.ensure_user(1001)
        project = Project.draft(
            owner_id=1001,
            profile_id=database.ensure_profile(1001),
            name="Server copy test",
            source_ref="source",
            destination_ref="destination",
        )
        project.source_chat_id = 111
        project.destination_chat_id = 222
        database.create_project(project)
        worker = BackupWorker(settings, database, gateway=None, bot=None)  # type: ignore[arg-type]
        source_media = object()
        message = SimpleNamespace(
            id=7,
            media=source_media,
            file=SimpleNamespace(name="movie.mp4", size=123, ext=".mp4"),
            message="caption should not be copied",
        )
        client = FakeServerCopyClient()

        await worker._server_copy_with_retry(  # noqa: SLF001 - focused server-copy behavior test
            project, client, "destination", message, MediaType.VIDEO, ScanProgress()
        )

        assert client.calls[0][1] is source_media
        assert client.calls[0][2]["caption"] is None
        assert database.transfer_completed(project.id, 111, 7)
        assert database.counters(project.id).bytes_transferred == 123
        assert not list(settings.temp_dir.rglob("*"))
        database.close()
