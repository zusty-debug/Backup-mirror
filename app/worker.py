from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from telethon import errors
from telethon.tl.custom.message import Message

from .config import Settings
from .database import Database
from .models import MediaType, Project, ProjectStatus, TransferStatus
from .telegram_gateway import TelegramGateway, TelegramGatewayError
from .utils import readable_bytes, safe_filename, sha256_file, truncate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanProgress:
    scanned: int = 0
    eligible: int = 0
    skipped: int = 0
    failed_this_run: int = 0
    bytes_this_run: int = 0
    current_file: str = "Preparing…"
    phase: str = "🚀 Preparing"
    total_eligible: int | None = None
    started_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class DownloadedMedia:
    message: Message
    path: Path
    filename: str
    media_type: MediaType
    size: int
    caption: str | None


class BackupWorker:
    """One ordered worker for one project. It never invokes Telegram forwarding APIs."""

    def __init__(self, settings: Settings, database: Database, gateway: TelegramGateway, bot) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.bot = bot
        self._last_status: dict[str, float] = {}

    async def run(self, project_id: str) -> None:
        project = self.database.get_project(project_id)
        if project is None:
            return
        run_id = self.database.open_run(project.id)
        progress = ScanProgress()
        result = "FAILED"
        try:
            if project.source_chat_id is None or project.destination_chat_id is None:
                await self._preflight(project)
                project = self._reload(project.id)
            self.database.update_project_status(project.id, ProjectStatus.RUNNING)
            await self._status(project, progress, force=True)

            waited_for_idle_media = False
            while True:
                project = self._reload(project.id)
                eligible_before_pass = progress.eligible
                progress.phase = "🔎 Scanning source"
                await self._run_single_pass(project, progress)
                found_media_this_pass = progress.eligible > eligible_before_pass
                project = self._reload(project.id)
                state = await self._handle_requested_state(project, progress)
                if state is not None:
                    result = state
                    break
                if not project.settings.continuous_sync:
                    self.database.update_project_status(project.id, ProjectStatus.COMPLETED)
                    progress.phase = "✅ Backup completed"
                    result = "COMPLETED"
                    break
                if waited_for_idle_media and not found_media_this_pass:
                    self.database.update_project_status(project.id, ProjectStatus.COMPLETED)
                    progress.phase = "⏹️ Sync stopped — no new media"
                    progress.current_file = "No new media arrived during the idle timer"
                    result = "IDLE_TIMEOUT"
                    break
                if found_media_this_pass:
                    waited_for_idle_media = False
                    continue
                idle_seconds = max(30, int(project.settings.idle_stop_seconds or self.settings.sync_poll_seconds))
                progress.phase = "👀 Waiting for new media"
                progress.current_file = f"Waiting up to {idle_seconds}s for a new media/file"
                await self._status(project, progress, force=True)
                if await self._wait_for_sync_or_control(project.id, idle_seconds):
                    project = self._reload(project.id)
                    result = await self._handle_requested_state(project, progress) or "STOPPED"
                    break
                waited_for_idle_media = True
        except asyncio.CancelledError:
            self.database.update_project_status(project_id, ProjectStatus.PAUSED, "Worker task was interrupted")
            result = "INTERRUPTED"
            raise
        except Exception as exc:
            logger.exception("Project %s ended with an unhandled error", project_id)
            self.database.update_project_status(project_id, ProjectStatus.FAILED, self._error_text(exc))
            self.database.log_event(project_id, "ERROR", self._error_text(exc))
            result = "FAILED"
        finally:
            project = self.database.get_project(project_id)
            if project:
                counters = self.database.counters(project_id)
                counters.scanned = progress.scanned
                counters.eligible = max(counters.eligible, progress.eligible)
                counters.skipped = progress.skipped
                counters.failed = max(counters.failed, progress.failed_this_run)
                counters.bytes_transferred = max(counters.bytes_transferred, progress.bytes_this_run)
                self.database.close_run(run_id, result, counters)
                await self._status(project, progress, force=True, final=True)

    async def _preflight(self, project: Project) -> None:
        source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
        self.database.update_project_resolution(
            project.id,
            int(source.id),
            self.gateway.entity_name(source),
            int(destination.id),
            self.gateway.entity_name(destination),
        )

    async def _run_single_pass(self, project: Project, progress: ScanProgress) -> None:
        if project.source_chat_id is None or project.destination_chat_id is None:
            raise TelegramGatewayError("Project has not completed source/destination validation.")

        async with self.gateway.client_for_profile(project.profile_id) as client:
            source = await self.gateway.resolve_entity(client, project.source_ref)
            destination = await self.gateway.resolve_entity(client, project.destination_ref)
            # Retry items that were left failed/retryable before moving past the saved checkpoint.
            for message_id in self.database.retryable_source_message_ids(project.id):
                message = await client.get_messages(source, ids=message_id)
                if message:
                    await self._process_message(project, client, destination, message, progress)
                    if await self._at_control_boundary(project.id, progress):
                        return

            checkpoint = project.checkpoint_message_id or 0
            min_id = max(checkpoint, (project.start_message_id or 1) - 1)
            album: list[Message] = []
            album_key: int | None = None
            async for message in client.iter_messages(source, reverse=True, min_id=min_id):
                if not self._within_date_range(project, message):
                    continue
                progress.scanned += 1
                grouped_id = getattr(message, "grouped_id", None)
                if album and grouped_id != album_key:
                    # A checkpoint is only advanced after the entire album has been persisted.
                    await self._process_batch(project, client, destination, album, progress)
                    self.database.update_project_checkpoint(project.id, int(album[-1].id))
                    if await self._at_control_boundary(project.id, progress):
                        return
                    album = []
                    album_key = None
                if grouped_id:
                    album.append(message)
                    album_key = grouped_id
                else:
                    await self._process_message(project, client, destination, message, progress)
                    self.database.update_project_checkpoint(project.id, int(message.id))
                    if await self._at_control_boundary(project.id, progress):
                        return
                await self._status(project, progress)
            if album:
                await self._process_batch(project, client, destination, album, progress)
                self.database.update_project_checkpoint(project.id, int(album[-1].id))

    @staticmethod
    def _within_date_range(project: Project, message: Message) -> bool:
        date = getattr(message, "date", None)
        if date is None:
            return True
        day = date.date().isoformat()
        if project.start_date and day < project.start_date:
            return False
        return not (project.end_date and day > project.end_date)

    async def _process_batch(self, project: Project, client, destination, messages: list[Message], progress: ScanProgress) -> None:
        eligible = [message for message in messages if self._eligible_media(project, message) is not None]
        if not eligible:
            return
        if project.settings.preserve_albums and len(eligible) > 1:
            await self._process_album(project, client, destination, eligible, progress)
            return
        for message in eligible:
            await self._process_message(project, client, destination, message, progress)

    async def _process_message(self, project: Project, client, destination, message: Message, progress: ScanProgress) -> None:
        media_type = self._eligible_media(project, message)
        if media_type is None:
            return
        progress.eligible += 1
        source_id = int(project.source_chat_id or 0)
        if project.settings.skip_duplicates and self.database.transfer_completed(project.id, source_id, int(message.id)):
            progress.skipped += 1
            return
        await self._transfer_with_retry(project, client, destination, message, media_type, progress)

    async def _process_album(self, project: Project, client, destination, messages: list[Message], progress: ScanProgress) -> None:
        selected: list[tuple[Message, MediaType]] = []
        source_id = int(project.source_chat_id or 0)
        for message in messages:
            media_type = self._eligible_media(project, message)
            if media_type is None:
                continue
            progress.eligible += 1
            if project.settings.skip_duplicates and self.database.transfer_completed(project.id, source_id, int(message.id)):
                progress.skipped += 1
                continue
            selected.append((message, media_type))
        if not selected:
            return

        # Telegram media groups accept up to 10 items. Split defensively if the source contains more.
        for offset in range(0, len(selected), 10):
            group = selected[offset : offset + 10]
            if project.settings.server_side_copy and not project.settings.checksum_enabled:
                await self._server_copy_album_with_retry(project, client, destination, group, progress)
            else:
                await self._album_with_retry(project, client, destination, group, progress)

    async def _server_copy_album_with_retry(
        self,
        project: Project,
        client,
        destination,
        group: list[tuple[Message, MediaType]],
        progress: ScanProgress,
    ) -> None:
        """Create a new destination album by reusing source media on Telegram's servers.

        Unlike forwarding, this invokes SendMedia and produces fresh destination
        messages without a forward header. No file bytes pass through this server.
        """
        attempt = 0
        source_chat_id = int(project.source_chat_id)
        progress.phase = "⚡ Sending fresh album via Telegram"
        while attempt < self.settings.max_upload_retries:
            try:
                for message, media_type in group: 
                    filename = self._message_filename(message, media_type)
                    size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
                    progress.current_file = filename
                    self.database.begin_transfer(
                        project_id=project.id,
                        source_chat_id=source_chat_id,
                        source_message_id=int(message.id),
                        media_type=media_type.value,
                        file_name=filename,
                        file_size=size,
                        status=TransferStatus.UPLOADING,
                    )
                captions = [message.message or "" if project.settings.preserve_captions else "" for message, _ in group]
                sent = await client.send_file(
                    destination,
                    [message.media for message, _ in group],
                    caption=captions,
                    parse_mode=None,
                    allow_cache=False,
                )
                sent_messages = sent if isinstance(sent, list) else [sent]
                if len(sent_messages) != len(group):
                    raise RuntimeError("Telegram did not return all destination album messages")
                for (message, _), destination_message in zip(group, sent_messages, strict=True):
                    self.database.complete_transfer(
                        project_id=project.id,
                        source_chat_id=source_chat_id,
                        source_message_id=int(message.id),
                        destination_chat_id=int(project.destination_chat_id),
                        destination_message_id=int(destination_message.id),
                        checksum_sha256=None,
                    )
                    progress.bytes_this_run += int(getattr(getattr(message, "file", None), "size", 0) or 0)
                return
            except errors.FloodWaitError as exc:
                if await self._wait_flood(project, progress, exc.seconds):
                    for message, _ in group:
                        self.database.mark_transfer(
                            project.id,
                            source_chat_id,
                            int(message.id),
                            TransferStatus.RETRY_WAIT,
                            "Interrupted during FloodWait",
                        )
                    return
            except Exception as exc:
                attempt += 1
                if attempt >= self.settings.max_upload_retries:
                    for message, media_type in group:
                        self.database.begin_transfer(
                            project_id=project.id,
                            source_chat_id=source_chat_id,
                            source_message_id=int(message.id),
                            media_type=media_type.value,
                            file_name=self._message_filename(message, media_type),
                            file_size=int(getattr(getattr(message, "file", None), "size", 0) or 0),
                            status=TransferStatus.RETRY_WAIT,
                        )
                        self.database.mark_transfer(
                            project.id, source_chat_id, int(message.id), TransferStatus.FAILED, self._error_text(exc)
                        )
                    progress.failed_this_run += len(group)
                    self.database.log_event(project.id, "ERROR", f"Server-copy album: {self._error_text(exc)}")
                    return
                await asyncio.sleep(min(2**attempt, 20))

    async def _album_with_retry(self, project: Project, client, destination, group: list[tuple[Message, MediaType]], progress: ScanProgress) -> None:
        attempt = 0
        while attempt < self.settings.max_upload_retries:
            files: list[DownloadedMedia] = []
            try:
                for message, media_type in group:
                    files.append(await self._download_media(project, client, message, media_type, progress))
                for item in files:
                    self.database.mark_transfer(project.id, int(project.source_chat_id), int(item.message.id), TransferStatus.UPLOADING)
                captions = [item.caption or "" for item in files]
                sent = await client.send_file(
                    destination,
                    [str(item.path) for item in files],
                    caption=captions,
                    force_document=False,
                    allow_cache=False,
                    parse_mode=None,
                )
                sent_messages = sent if isinstance(sent, list) else [sent]
                if len(sent_messages) != len(files):
                    raise RuntimeError("Telegram did not return all destination album messages")
                for item, destination_message in zip(files, sent_messages, strict=True):
                    checksum = sha256_file(item.path) if project.settings.checksum_enabled else None
                    self.database.complete_transfer(
                        project_id=project.id,
                        source_chat_id=int(project.source_chat_id),
                        source_message_id=int(item.message.id),
                        destination_chat_id=int(project.destination_chat_id),
                        destination_message_id=int(destination_message.id),
                        checksum_sha256=checksum,
                    )
                    progress.bytes_this_run += item.size
                return
            except errors.FloodWaitError as exc:
                if await self._wait_flood(project, progress, exc.seconds):
                    for message, _ in group:
                        self.database.mark_transfer(
                            project.id,
                            int(project.source_chat_id),
                            int(message.id),
                            TransferStatus.RETRY_WAIT,
                            "Interrupted during FloodWait",
                        )
                    return
            except Exception as exc:
                attempt += 1
                if attempt >= self.settings.max_upload_retries:
                    for message, media_type in group:
                        # A download can fail before later album members have a row.
                        # Insert/update every member so none disappear from failure reporting.
                        self.database.begin_transfer(
                            project_id=project.id,
                            source_chat_id=int(project.source_chat_id),
                            source_message_id=int(message.id),
                            media_type=media_type.value,
                            file_name=self._message_filename(message, media_type),
                            file_size=int(getattr(getattr(message, "file", None), "size", 0) or 0),
                            status=TransferStatus.RETRY_WAIT,
                        )
                        self.database.mark_transfer(
                            project.id,
                            int(project.source_chat_id),
                            int(message.id),
                            TransferStatus.FAILED,
                            self._error_text(exc),
                        )
                    progress.failed_this_run += len(group)
                    self.database.log_event(
                        project.id,
                        "ERROR",
                        f"Album {group[0][0].grouped_id}: {self._error_text(exc)}",
                    )
                    return
                await asyncio.sleep(min(2**attempt, 20))
            finally:
                self._delete_downloads(files)

    async def _server_copy_with_retry(
        self,
        project: Project,
        client,
        destination,
        message: Message,
        media_type: MediaType,
        progress: ScanProgress,
    ) -> None:
        """Send an existing source media object as a fresh destination message.

        Telethon turns ``message.media`` into Telegram's InputMedia document/photo
        reference and calls SendMedia. This is server-side media reuse, not a
        ForwardMessages call and not a local download/re-upload.
        """
        attempt = 0
        source_chat_id = int(project.source_chat_id)
        filename = self._message_filename(message, media_type)
        size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        progress.current_file = filename
        progress.phase = "⚡ Sending fresh media via Telegram"
        while attempt < self.settings.max_upload_retries:
            try:
                self.database.begin_transfer(
                    project_id=project.id,
                    source_chat_id=source_chat_id,
                    source_message_id=int(message.id),
                    media_type=media_type.value,
                    file_name=filename,
                    file_size=size,
                    status=TransferStatus.UPLOADING,
                )
                caption = message.message or "" if project.settings.preserve_captions else None
                sent = await client.send_file(
                    destination,
                    message.media,
                    caption=caption,
                    parse_mode=None,
                    allow_cache=False,
                )
                self.database.complete_transfer(
                    project_id=project.id,
                    source_chat_id=source_chat_id,
                    source_message_id=int(message.id),
                    destination_chat_id=int(project.destination_chat_id),
                    destination_message_id=int(sent.id),
                    checksum_sha256=None,
                )
                progress.bytes_this_run += size
                return
            except errors.FloodWaitError as exc:
                if await self._wait_flood(project, progress, exc.seconds):
                    self.database.mark_transfer(
                        project.id,
                        source_chat_id,
                        int(message.id),
                        TransferStatus.RETRY_WAIT,
                        "Interrupted during FloodWait",
                    )
                    return
            except Exception as exc:
                attempt += 1
                if attempt >= self.settings.max_upload_retries:
                    self.database.mark_transfer(
                        project.id,
                        source_chat_id,
                        int(message.id),
                        TransferStatus.FAILED,
                        self._error_text(exc),
                    )
                    progress.failed_this_run += 1
                    self.database.log_event(project.id, "ERROR", f"Server-copy message {message.id}: {self._error_text(exc)}")
                    return
                self.database.mark_transfer(
                    project.id,
                    source_chat_id,
                    int(message.id),
                    TransferStatus.RETRY_WAIT,
                    self._error_text(exc),
                )
                await asyncio.sleep(min(2**attempt, 20))

    async def _transfer_with_retry(
        self, project: Project, client, destination, message: Message, media_type: MediaType, progress: ScanProgress
    ) -> None:
        if project.settings.server_side_copy and not project.settings.checksum_enabled:
            await self._server_copy_with_retry(project, client, destination, message, media_type, progress)
            return
        attempt = 0
        while attempt < self.settings.max_upload_retries:
            item: DownloadedMedia | None = None
            try:
                item = await self._download_media(project, client, message, media_type, progress)
                self.database.mark_transfer(project.id, int(project.source_chat_id), int(message.id), TransferStatus.UPLOADING)
                sent = await client.send_file(
                    destination,
                    str(item.path),
                    caption=item.caption,
                    force_document=media_type == MediaType.DOCUMENT,
                    voice_note=media_type == MediaType.VOICE,
                    video_note=media_type == MediaType.VIDEO_NOTE,
                    allow_cache=False,
                    parse_mode=None,
                )
                checksum = sha256_file(item.path) if project.settings.checksum_enabled else None
                self.database.complete_transfer(
                    project_id=project.id,
                    source_chat_id=int(project.source_chat_id),
                    source_message_id=int(message.id),
                    destination_chat_id=int(project.destination_chat_id),
                    destination_message_id=int(sent.id),
                    checksum_sha256=checksum,
                )
                progress.bytes_this_run += item.size
                return
            except errors.FloodWaitError as exc:
                if await self._wait_flood(project, progress, exc.seconds):
                    self.database.mark_transfer(
                        project.id,
                        int(project.source_chat_id),
                        int(message.id),
                        TransferStatus.RETRY_WAIT,
                        "Interrupted during FloodWait",
                    )
                    return
            except Exception as exc:
                attempt += 1
                if attempt >= self.settings.max_upload_retries:
                    self.database.mark_transfer(
                        project.id, int(project.source_chat_id), int(message.id), TransferStatus.FAILED, self._error_text(exc)
                    )
                    progress.failed_this_run += 1
                    self.database.log_event(project.id, "ERROR", f"Message {message.id}: {self._error_text(exc)}")
                    return
                self.database.mark_transfer(project.id, int(project.source_chat_id), int(message.id), TransferStatus.RETRY_WAIT, self._error_text(exc))
                await asyncio.sleep(min(2**attempt, 20))
            finally:
                if item:
                    self._delete_downloads([item])

    async def _download_media(
        self, project: Project, client, message: Message, media_type: MediaType, progress: ScanProgress
    ) -> DownloadedMedia:
        filename = self._message_filename(message, media_type)
        size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        progress.current_file = filename
        self.database.begin_transfer(
            project_id=project.id,
            source_chat_id=int(project.source_chat_id),
            source_message_id=int(message.id),
            media_type=media_type.value,
            file_name=filename,
            file_size=size,
            status=TransferStatus.DOWNLOADING,
        )
        # The random directory prevents collisions while the final path basename preserves
        # the original filename on the newly uploaded Telegram document.
        download_dir = self.settings.temp_dir / project.id / f"{message.id}-{uuid4().hex}"
        download_dir.mkdir(parents=True, exist_ok=True)
        destination = download_dir / filename
        downloaded = await client.download_media(message, file=str(destination))
        if not downloaded:
            raise RuntimeError("Telegram returned no downloaded media file")
        path = Path(downloaded)
        if not path.exists():
            raise RuntimeError("Downloaded temporary file is missing")
        actual_size = path.stat().st_size
        if actual_size:
            size = actual_size
        caption = (message.message or "") if project.settings.preserve_captions else None
        return DownloadedMedia(message, path, filename, media_type, size, caption)

    @staticmethod
    def _message_filename(message: Message, media_type: MediaType) -> str:
        file = getattr(message, "file", None)
        raw_name = getattr(file, "name", None)
        extension = getattr(file, "ext", None) or ""
        defaults = {
            MediaType.PHOTO: f"photo_{message.id}.jpg",
            MediaType.VIDEO: f"video_{message.id}.mp4",
            MediaType.AUDIO: f"audio_{message.id}.mp3",
            MediaType.VOICE: f"voice_{message.id}.ogg",
            MediaType.VIDEO_NOTE: f"video_note_{message.id}.mp4",
            MediaType.STICKER: f"sticker_{message.id}{extension or '.webp'}",
            MediaType.DOCUMENT: f"document_{message.id}{extension}",
            MediaType.OTHER: f"media_{message.id}{extension}",
        }
        return safe_filename(raw_name, defaults[media_type])

    @staticmethod
    def _eligible_media(project: Project, message: Message) -> MediaType | None:
        if not getattr(message, "media", None):
            return None
        if getattr(message, "sticker", False):
            media_type = MediaType.STICKER
        elif getattr(message, "photo", None):
            media_type = MediaType.PHOTO
        elif getattr(message, "video_note", False):
            media_type = MediaType.VIDEO_NOTE
        elif getattr(message, "voice", False):
            media_type = MediaType.VOICE
        elif getattr(message, "video", False):
            media_type = MediaType.VIDEO
        elif getattr(message, "audio", False):
            media_type = MediaType.AUDIO
        elif getattr(message, "document", None):
            media_type = MediaType.DOCUMENT
        else:
            media_type = MediaType.OTHER
        return media_type if project.settings.allows(media_type) else None

    async def _wait_flood(self, project: Project, progress: ScanProgress, seconds: int) -> bool:
        """Wait for Telegram's requested limit; return True when pause/stop interrupts it."""
        seconds = max(1, int(seconds))
        self.database.update_project_status(project.id, ProjectStatus.WAITING_RATE_LIMIT, f"FloodWait: {seconds}s")
        progress.current_file = f"Telegram rate limit — waiting {seconds}s"
        await self._status(project, progress, force=True)
        self.database.log_event(project.id, "WARNING", f"FloodWait: waiting {seconds} seconds")
        remaining = seconds
        while remaining > 0:
            await asyncio.sleep(min(remaining, 5))
            remaining -= 5
            state = self._reload(project.id).status
            if state in {ProjectStatus.PAUSE_REQUESTED, ProjectStatus.STOP_REQUESTED}:
                return True
        self.database.update_project_status(project.id, ProjectStatus.RUNNING)
        return False

    async def _at_control_boundary(self, project_id: str, progress: ScanProgress) -> bool:
        project = self._reload(project_id)
        await self._status(project, progress)
        return project.status in {ProjectStatus.PAUSE_REQUESTED, ProjectStatus.STOP_REQUESTED}

    async def _handle_requested_state(self, project: Project, progress: ScanProgress) -> str | None:
        if project.status == ProjectStatus.PAUSE_REQUESTED:
            self.database.update_project_status(project.id, ProjectStatus.PAUSED)
            progress.current_file = "Paused safely"
            await self._status(self._reload(project.id), progress, force=True)
            return "PAUSED"
        if project.status == ProjectStatus.STOP_REQUESTED:
            self.database.update_project_status(project.id, ProjectStatus.STOPPED)
            progress.current_file = "Stopped safely — progress retained"
            await self._status(self._reload(project.id), progress, force=True)
            return "STOPPED"
        return None

    async def _wait_for_sync_or_control(self, project_id: str, seconds: int) -> bool:
        remaining = seconds
        while remaining > 0:
            await asyncio.sleep(min(remaining, 5))
            remaining -= 5
            project = self._reload(project_id)
            if project.status in {ProjectStatus.PAUSE_REQUESTED, ProjectStatus.STOP_REQUESTED}:
                return True
        return False

    async def _status(self, project: Project, progress: ScanProgress, *, force: bool = False, final: bool = False) -> None:
        if not project.status_message_chat_id or not project.status_message_id:
            return
        now = time.monotonic()
        if not force and now - self._last_status.get(project.id, 0) < self.settings.status_update_seconds:
            return
        self._last_status[project.id] = now
        counters = self.database.counters(project.id)
        elapsed = max(time.monotonic() - progress.started_at, 0.001)
        speed = progress.bytes_this_run / elapsed
        state = self._reload(project.id).status.value
        title = "✅ Backup completed" if final and state == ProjectStatus.COMPLETED.value else "📡 Live backup status"
        elapsed_text = self._readable_duration(elapsed)
        copied_or_skipped = counters.completed + progress.skipped
        progress_line = f"{copied_or_skipped:,} processed"
        if progress.total_eligible:
            percentage = min(100.0, copied_or_skipped * 100 / progress.total_eligible)
            progress_line = f"{copied_or_skipped:,} / {progress.total_eligible:,} ({percentage:.1f}%)"
        text = (
            f"<b>{title}</b>\n"
            f"📁 Project: <b>{self._escape(project.name)}</b>\n"
            f"🔄 State: <code>{state}</code>\n"
            f"📍 Phase: {self._escape(progress.phase)}\n\n"
            f"📊 Progress: {progress_line}\n"
            f"🔎 Source messages scanned: {progress.scanned:,}\n"
            f"🎞️ Media/files found: {progress.eligible:,}\n"
            f"✅ Copied: {counters.completed:,}\n"
            f"⏭️ Skipped: {progress.skipped:,}\n"
            f"⚠️ Failed: {max(counters.failed, progress.failed_this_run):,}\n"
            f"📦 Media reused: {readable_bytes(counters.bytes_transferred)}\n"
            f"⚡ Effective speed: {readable_bytes(speed)}/s\n"
            f"⏱️ Elapsed: {elapsed_text}\n"
            f"📌 Current: <code>{self._escape(truncate(progress.current_file, 70))}</code>\n\n"
            "<i>Developed by — @xzusty</i>"
        )
        try:
            await self.bot.edit_message_text(
                text, chat_id=project.status_message_chat_id, message_id=project.status_message_id
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Unable to edit status message for %s: %s", project.id, exc)

    @staticmethod
    def _delete_downloads(items: Iterable[DownloadedMedia]) -> None:
        for item in items:
            try:
                item.path.unlink(missing_ok=True)
                item.path.parent.rmdir()
            except OSError:
                logger.warning("Could not remove temporary file %s", item.path)

    def _reload(self, project_id: str) -> Project:
        project = self.database.get_project(project_id)
        if project is None:
            raise RuntimeError("Project was deleted while its worker was active")
        return project

    @staticmethod
    def _error_text(exc: BaseException) -> str:
        return truncate(f"{exc.__class__.__name__}: {exc}", 500)

    @staticmethod
    def _readable_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class WorkerManager:
    def __init__(self, worker: BackupWorker, database: Database) -> None:
        self.worker = worker
        self.database = database
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def is_running(self, project_id: str) -> bool:
        task = self.tasks.get(project_id)
        return bool(task and not task.done())

    async def start(self, project_id: str) -> bool:
        if self.is_running(project_id):
            return False
        project = self.database.get_project(project_id)
        if project is None:
            raise ValueError("Project does not exist")
        if project.status in {ProjectStatus.COMPLETED, ProjectStatus.STOPPED, ProjectStatus.FAILED, ProjectStatus.PAUSED}:
            self.database.update_project_status(project_id, ProjectStatus.READY)
        task = asyncio.create_task(self._run_and_cleanup(project_id), name=f"backup-{project_id}")
        self.tasks[project_id] = task
        return True

    async def _run_and_cleanup(self, project_id: str) -> None:
        try:
            await self.worker.run(project_id)
        finally:
            self.tasks.pop(project_id, None)

    def request_pause(self, project_id: str) -> None:
        self.database.request_pause(project_id)

    def request_stop(self, project_id: str) -> None:
        self.database.request_stop(project_id)

    async def resume_after_restart(self) -> None:
        for project in self.database.projects_to_resume():
            await self.start(project.id)

    async def shutdown(self) -> None:
        for project_id, task in list(self.tasks.items()):
            if not task.done():
                self.database.request_pause(project_id)
        if self.tasks:
            await asyncio.wait(self.tasks.values(), timeout=30)
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
