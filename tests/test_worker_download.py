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
