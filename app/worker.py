from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from telethon import errors, functions
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
    counted_messages: int = 0
    rate_wait_total: int | None = None
    rate_wait_remaining: int | None = None
    last_send_at: float = 0.0
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
        self._active_progress: dict[str, ScanProgress] = {}

    async def run(self, project_id: str) -> None:
        project = self.database.get_project(project_id)
        if project is None:
            return
        run_id = self.database.open_run(project.id)
        progress = ScanProgress()
        self._active_progress[project.id] = progress
        result = "FAILED"
        try:
            if project.source_chat_id is None or project.destination_chat_id is None:
                await self._preflight(project)
                project = self._reload(project.id)
            self.database.update_project_status(project.id, ProjectStatus.RUNNING)
            plan = self.database.project_plan(project.id)
            if plan:
                progress.total_eligible = int(plan["selected_total"])
                progress.counted_messages = int(plan["scanned_total"])
                progress.phase = "🚀 Sending approved plan"
            self.database.log_event(project.id, "INFO", "Backup run started")
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
                self.database.log_event(project_id, "INFO", f"Backup run finished: {result}")
                await self._status(project, progress, force=True, final=True)
            self._active_progress.pop(project_id, None)

    def live_progress(self, project_id: str) -> ScanProgress | None:
        return self._active_progress.get(project_id)

    async def _preflight(self, project: Project) -> None:
        source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
        self.database.update_project_resolution(
            project.id,
            int(source.id),
            self.gateway.entity_name(source),
            int(destination.id),
            self.gateway.entity_name(destination),
        )

    async def preview(self, project_id: str, progress_callback=None) -> dict[str, int]:
        """Read-only two-pass plan scan using the exact transfer selection."""
        project = self._reload(project_id)
        async with self.gateway.client_for_profile(project.profile_id) as client:
            source = await self.gateway.resolve_entity(client, project.source_ref)
            # First pass obtains a real denominator for the live scanning bar.
            source_total = 0
            last_count_update = 0.0
            async for _ in self._iter_selected_source_messages(project, client, source):
                source_total += 1
                now = time.monotonic()
                if progress_callback and now - last_count_update >= 2:
                    await progress_callback("🧮 Counting selected source range", source_total, 0, 0)
                    last_count_update = now
            if progress_callback:
                await progress_callback("🧮 Counting valid selected content", 0, source_total, 0)
            counts: dict[str, int] = {}
            scanned = 0
            last_update = 0.0
            async for message in self._iter_selected_source_messages(project, client, source):
                scanned += 1
                kind = self._eligible_media(project, message)
                if kind:
                    counts[kind.value] = counts.get(kind.value, 0) + 1
                now = time.monotonic()
                if progress_callback and (now - last_update >= 2 or scanned == source_total):
                    await progress_callback("🔎 Scanning selected source", scanned, source_total, sum(counts.values()))
                    last_update = now
        counts["SCANNED"] = scanned
        counts["TOTAL"] = sum(value for key, value in counts.items() if key != "SCANNED")
        self.database.log_event(project.id, "INFO", f"Preview plan scan: {counts['TOTAL']} selected items found")
        return counts

    async def _iter_selected_source_messages(self, project: Project, client, source):
        """Yield messages selected by the project with the same logic used for sending."""
        if getattr(source, "forum", False) and project.settings.forum_to_channel_segments:
            for topic_id in [int(item) for item in project.settings.forum_topic_ids]:
                async for message in client.iter_messages(source, reverse=True, reply_to=topic_id):
                    if self._within_date_range(project, message):
                        yield message
            return
        checkpoint = project.checkpoint_message_id or 0
        min_id = max(checkpoint, (project.start_message_id or 1) - 1)
        async for message in client.iter_messages(source, reverse=True, min_id=min_id):
            if self._within_date_range(project, message) and self._message_in_selected_topic(project, message):
                yield message

    async def _run_single_pass(self, project: Project, progress: ScanProgress) -> None:
        if project.source_chat_id is None or project.destination_chat_id is None:
            raise TelegramGatewayError("Project has not completed source/destination validation.")

        async with self.gateway.client_for_profile(project.profile_id) as client:
            source = await self.gateway.resolve_entity(client, project.source_ref)
            destination = await self.gateway.resolve_entity(client, project.destination_ref)
            if getattr(source, "forum", False) and project.settings.forum_to_channel_segments:
                await self._run_forum_to_channel_segments(project, client, source, destination, progress)
                return
            await self._ensure_forum_topics(project, client, source, destination, progress)
            if progress.total_eligible is None and not project.settings.continuous_sync:
                await self._count_eligible_items(project, client, source, progress)
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
                if not self._message_in_selected_topic(project, message):
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
    def _message_in_selected_topic(project: Project, message: Message) -> bool:
        selected = {int(topic_id) for topic_id in project.settings.forum_topic_ids}
        if not selected:
            return True
        reply = getattr(message, "reply_to", None)
        topic_id = getattr(reply, "reply_to_top_id", None) if reply else None
        # The General topic may not carry a top-message reference on every message.
        return bool(topic_id and int(topic_id) in selected)

    async def _ensure_forum_topics(self, project: Project, client, source, destination, progress: ScanProgress) -> None:
        if not getattr(source, "forum", False):
            return
        if not getattr(destination, "forum", False):
            raise TelegramGatewayError("Source is a forum group but destination is not a forum group.")
        if not project.settings.clone_forum_topics:
            return
        progress.phase = "🧵 Matching forum topics"
        result = await client(
            functions.messages.GetForumTopicsRequest(
                peer=source,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q=None,
            )
        )
        selected_topic_ids = {int(topic_id) for topic_id in project.settings.forum_topic_ids}
        for topic in getattr(result, "topics", []):
            source_topic_id = int(topic.id)
            if selected_topic_ids and source_topic_id not in selected_topic_ids:
                continue
            if self.database.destination_topic_id(project.id, source_topic_id):
                continue
            # Topic creation/matching is completed during the setup wizard.
            # The worker never attempts a privileged topic-create operation mid-run.
            title = str(getattr(topic, "title", None) or "Topic")
            raise TelegramGatewayError(
                f"Destination topic mapping is missing for '{title}'. Recreate the project and complete forum setup first."
            )

    async def _run_forum_to_channel_segments(self, project: Project, client, source, destination, progress: ScanProgress) -> None:
        """Mirror selected forum topics into one normal channel, topic by topic."""
        selected = [int(topic_id) for topic_id in project.settings.forum_topic_ids]
        if not selected:
            raise TelegramGatewayError("No forum topics were selected for channel segmentation.")
        result = await client(
            functions.messages.GetForumTopicsRequest(
                peer=source,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q=None,
            )
        )
        topic_by_id = {int(topic.id): topic for topic in getattr(result, "topics", []) if getattr(topic, "id", None)}
        if progress.total_eligible is None and not project.settings.continuous_sync:
            progress.phase = "🧮 Counting selected topic content"
            progress.current_file = "Calculating exact forum topic total"
            total = 0
            counted = 0
            for topic_id in selected:
                async for message in client.iter_messages(source, reverse=True, reply_to=topic_id):
                    if not self._within_date_range(project, message):
                        continue
                    counted += 1
                    if self._eligible_media(project, message) is not None:
                        total += 1
                    if counted % 250 == 0:
                        progress.counted_messages = counted
                        await self._status(project, progress)
            progress.counted_messages = counted
            progress.total_eligible = total
            self.database.log_event(project.id, "INFO", f"Counted {total} selected forum-topic items for progress tracking")
        for topic_id in selected:
            topic = topic_by_id.get(topic_id)
            if not topic:
                self.database.log_event(project.id, "WARNING", f"Selected forum topic {topic_id} is unavailable; skipped")
                continue
            title = str(getattr(topic, "title", None) or f"Topic {topic_id}")
            await self._ensure_forum_channel_header(project, client, destination, topic_id, title)
            progress.phase = f"🧵 Mirroring topic: {title}"
            progress.current_file = f"Topic header pinned: {title}"
            self.database.log_event(project.id, "INFO", f"Started forum channel segment: {title}")
            await self._status(project, progress, force=True)
            album: list[Message] = []
            album_key: int | None = None
            # Telegram can fetch a forum topic thread directly. This prevents a
            # selected-topic backup from scanning every unrelated forum message.
            async for message in client.iter_messages(source, reverse=True, reply_to=topic_id):
                if not self._within_date_range(project, message):
                    continue
                progress.scanned += 1
                grouped_id = getattr(message, "grouped_id", None)
                if album and grouped_id != album_key:
                    await self._process_batch(project, client, destination, album, progress)
                    if await self._at_control_boundary(project.id, progress):
                        return
                    album = []
                    album_key = None
                if grouped_id:
                    album.append(message)
                    album_key = grouped_id
                else:
                    await self._process_message(project, client, destination, message, progress)
                    if await self._at_control_boundary(project.id, progress):
                        return
                await self._status(project, progress)
            if album:
                await self._process_batch(project, client, destination, album, progress)
                if await self._at_control_boundary(project.id, progress):
                    return
            self.database.log_event(project.id, "INFO", f"Finished forum channel segment: {title}")

    async def _ensure_forum_channel_header(self, project: Project, client, destination, topic_id: int, title: str) -> int:
        existing = self.database.forum_channel_segment(project.id, topic_id)
        if existing:
            return int(existing["destination_header_message_id"])
        header = await client.send_message(
            destination,
            f"📌 <b>{self._escape(title)}</b>\n\n<i>Forum topic mirror begins below</i>",
            parse_mode="html",
            link_preview=False,
        )
        pinned = False
        try:
            await client.pin_message(destination, header, notify=False)
            pinned = True
        except errors.RPCError as exc:
            self.database.log_event(project.id, "WARNING", f"Could not pin topic header '{title}': {self._error_text(exc)}")
        self.database.save_forum_channel_segment(project.id, topic_id, int(header.id), title, pinned=pinned)
        return int(header.id)

    @staticmethod
    def _forum_message_topic_id(message: Message) -> int:
        reply = getattr(message, "reply_to", None)
        top_id = getattr(reply, "reply_to_top_id", None) if reply else None
        return int(top_id) if top_id else 1

    async def _count_eligible_items(self, project: Project, client, source, progress: ScanProgress) -> None:
        """Count selected source items once to provide real progress and ETA."""
        checkpoint = project.checkpoint_message_id or 0
        min_id = max(checkpoint, (project.start_message_id or 1) - 1)
        progress.phase = "🧮 Counting selected content"
        progress.current_file = "Calculating total for live progress"
        total = 0
        counted = 0
        async for message in client.iter_messages(source, reverse=True, min_id=min_id):
            if not self._within_date_range(project, message) or not self._message_in_selected_topic(project, message):
                continue
            counted += 1
            if self._eligible_media(project, message) is not None:
                total += 1
            if counted % 250 == 0:
                progress.counted_messages = counted
                await self._status(project, progress)
        progress.counted_messages = counted
        progress.total_eligible = total
        progress.phase = "🔎 Scanning source"
        self.database.log_event(project.id, "INFO", f"Counted {total} selected source items for progress tracking")

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
        content_type = self._eligible_media(project, message)
        if content_type is None:
            return
        progress.eligible += 1
        source_id = int(project.source_chat_id or 0)
        if project.settings.skip_duplicates and self.database.transfer_completed(project.id, source_id, int(message.id)):
            progress.skipped += 1
            return
        if content_type in {MediaType.TEXT, MediaType.LINK}:
            await self._send_text_with_retry(project, client, destination, message, content_type, progress)
            return
        await self._transfer_with_retry(project, client, destination, message, content_type, progress)

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
                await self._pace_before_send(progress, len(group))
                sent = await client.send_file(
                    destination,
                    [message.media for message, _ in group],
                    caption=captions,
                    parse_mode=None,
                    allow_cache=False,
                    reply_to=self._destination_reply_target(project, group[0][0]),
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
                await self._pace_before_send(progress, len(files))
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

    async def _send_text_with_retry(
        self,
        project: Project,
        client,
        destination,
        message: Message,
        content_type: MediaType,
        progress: ScanProgress,
    ) -> None:
        source_chat_id = int(project.source_chat_id)
        text = self._message_links(message) if content_type == MediaType.LINK else (message.message or "")
        if not text.strip():
            return
        progress.current_file = truncate(text, 60)
        progress.phase = "💬 Sending fresh text/link"
        for attempt in range(self.settings.max_upload_retries):
            try:
                self.database.begin_transfer(
                    project_id=project.id,
                    source_chat_id=source_chat_id,
                    source_message_id=int(message.id),
                    media_type=content_type.value,
                    file_name="links.txt" if content_type == MediaType.LINK else "message.txt",
                    file_size=len(text.encode("utf-8")),
                    status=TransferStatus.UPLOADING,
                )
                await self._pace_before_send(progress)
                sent = await client.send_message(
                    destination,
                    text,
                    parse_mode=None,
                    link_preview=content_type != MediaType.LINK,
                    reply_to=self._destination_reply_target(project, message),
                )
                self.database.complete_transfer(
                    project_id=project.id,
                    source_chat_id=source_chat_id,
                    source_message_id=int(message.id),
                    destination_chat_id=int(project.destination_chat_id),
                    destination_message_id=int(sent.id),
                    checksum_sha256=None,
                )
                return
            except errors.FloodWaitError as exc:
                if await self._wait_flood(project, progress, exc.seconds):
                    self.database.mark_transfer(project.id, source_chat_id, int(message.id), TransferStatus.RETRY_WAIT, "Interrupted during FloodWait")
                    return
            except Exception as exc:
                if attempt + 1 >= self.settings.max_upload_retries:
                    self.database.mark_transfer(project.id, source_chat_id, int(message.id), TransferStatus.FAILED, self._error_text(exc))
                    progress.failed_this_run += 1
                    return
                await asyncio.sleep(min(2 ** (attempt + 1), 20))

    def _destination_reply_target(self, project: Project, message: Message) -> int | None:
        reply = getattr(message, "reply_to", None)
        if not reply:
            if project.settings.forum_to_channel_segments:
                segment = self.database.forum_channel_segment(project.id, 1)
                return int(segment["destination_header_message_id"]) if segment else None
            return None
        parent_source_id = getattr(reply, "reply_to_msg_id", None)
        if parent_source_id:
            copied_parent = self.database.destination_message_id(project.id, int(project.source_chat_id), int(parent_source_id))
            if copied_parent:
                return copied_parent
        source_topic_id = getattr(reply, "reply_to_top_id", None)
        if project.settings.forum_to_channel_segments:
            topic_id = int(source_topic_id) if source_topic_id else 1
            segment = self.database.forum_channel_segment(project.id, topic_id)
            if segment:
                return int(segment["destination_header_message_id"])
        if source_topic_id:
            return self.database.destination_topic_id(project.id, int(source_topic_id))
        return None

    @staticmethod
    def _message_links(message: Message) -> str:
        text = message.message or ""
        links = re.findall(r"(?:https?://|tg://|t\.me/)[^\s<>]+", text, flags=re.IGNORECASE)
        for entity in getattr(message, "entities", None) or []:
            url = getattr(entity, "url", None)
            if url:
                links.append(str(url))
        return "\n".join(dict.fromkeys(links))

    async def _pace_before_send(self, progress: ScanProgress, units: int = 1) -> None:
        """Maintain a conservative per-message delivery pace for public workers."""
        interval = 60 / self.settings.max_sends_per_minute
        target = progress.last_send_at + interval * max(1, units)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        progress.last_send_at = time.monotonic()

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
                await self._pace_before_send(progress)
                sent = await client.send_file(
                    destination,
                    message.media,
                    caption=caption,
                    parse_mode=None,
                    allow_cache=False,
                    reply_to=self._destination_reply_target(project, message),
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
                await self._pace_before_send(progress)
                sent = await client.send_file(
                    destination,
                    str(item.path),
                    caption=item.caption,
                    force_document=media_type == MediaType.DOCUMENT,
                    voice_note=media_type == MediaType.VOICE,
                    video_note=media_type == MediaType.VIDEO_NOTE,
                    allow_cache=False,
                    parse_mode=None,
                    reply_to=self._destination_reply_target(project, message),
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
        mode = project.settings.content_mode.upper()
        links = BackupWorker._message_links(message)
        if getattr(message, "media", None):
            if getattr(message, "sticker", False):
                media_type = MediaType.STICKER
            elif getattr(message, "photo", None):
                media_type = MediaType.PHOTO
            elif getattr(message, "gif", False):
                media_type = MediaType.GIF
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
            if project.settings.allows(media_type):
                return media_type
            if links and project.settings.allows(MediaType.LINK):
                return MediaType.LINK
            return None
        if mode == "EVERYTHING" and (message.message or ""):
            return MediaType.TEXT
        if links and project.settings.allows(MediaType.LINK):
            return MediaType.LINK
        return None

    async def _wait_flood(self, project: Project, progress: ScanProgress, seconds: int) -> bool:
        """Respect Telegram pacing; return True when pause/stop interrupts it."""
        seconds = max(1, int(seconds))
        self.database.update_project_status(project.id, ProjectStatus.WAITING_RATE_LIMIT)
        progress.phase = "🛡️ Telegram pace protection"
        progress.rate_wait_total = seconds
        progress.rate_wait_remaining = seconds
        progress.current_file = f"Telegram is pacing sends — resuming in {seconds}s"
        await self._status(project, progress, force=True)
        self.database.log_event(project.id, "INFO", f"Telegram pace protection active: waiting {seconds} seconds")
        remaining = seconds
        last_refresh = seconds
        while remaining > 0:
            await asyncio.sleep(min(remaining, 2))
            remaining = max(0, remaining - 2)
            progress.rate_wait_remaining = remaining
            state = self._reload(project.id).status
            if state in {ProjectStatus.PAUSE_REQUESTED, ProjectStatus.STOP_REQUESTED}:
                return True
            # Refresh countdown and both progress bars every two seconds.
            if last_refresh - remaining >= 2 or remaining == 0:
                progress.current_file = f"Telegram is pacing sends — resuming in {remaining}s"
                await self._status(project, progress, force=True)
                last_refresh = remaining
        self.database.update_project_status(project.id, ProjectStatus.RUNNING)
        progress.rate_wait_total = None
        progress.rate_wait_remaining = None
        progress.phase = "⚡ Sending fresh Telegram media"
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
        status_interval = min(2, self.settings.status_update_seconds)
        if not force and now - self._last_status.get(project.id, 0) < status_interval:
            return
        self._last_status[project.id] = now
        counters = self.database.counters(project.id)
        elapsed = max(time.monotonic() - progress.started_at, 0.001)
        speed = progress.bytes_this_run / elapsed
        raw_state = self._reload(project.id).status.value
        state = "🛡️ Telegram pace protection" if raw_state == ProjectStatus.WAITING_RATE_LIMIT.value else raw_state
        title = "✅ Backup completed" if final and raw_state == ProjectStatus.COMPLETED.value else "📡 Live backup status"
        elapsed_text = self._readable_duration(elapsed)
        copied_or_skipped = counters.completed + progress.skipped
        progress_line = f"{copied_or_skipped:,} processed"
        progress_bar = ""
        eta_line = "⏳ ETA: calculating…"
        if progress.total_eligible is not None:
            percentage = min(100.0, copied_or_skipped * 100 / max(1, progress.total_eligible))
            progress_line = f"{copied_or_skipped:,} / {progress.total_eligible:,} ({percentage:.1f}%)"
            progress_bar = f"{self._progress_bar(percentage)} {percentage:.1f}%"
            item_rate = copied_or_skipped / elapsed
            remaining = max(0, progress.total_eligible - copied_or_skipped)
            eta_line = f"⏳ ETA: {self._readable_duration(remaining / item_rate)}" if item_rate > 0 else "⏳ ETA: calculating…"
        elif progress.phase.startswith("🧮"):
            eta_line = f"🧮 Count scan: {progress.counted_messages:,} source messages checked"
        rate_bar_line = ""
        if progress.rate_wait_total is not None and progress.rate_wait_remaining is not None:
            wait_elapsed_pct = 100 * (progress.rate_wait_total - progress.rate_wait_remaining) / max(1, progress.rate_wait_total)
            rate_bar_line = (
                f"🛡️ Pace timer: {self._progress_bar(wait_elapsed_pct)} {wait_elapsed_pct:.1f}%\n"
                f"⏳ Telegram resumes in: {self._readable_duration(progress.rate_wait_remaining)}\n"
            )
        text = (
            f"<b>{title}</b>\n"
            f"📁 Project: <b>{self._escape(project.name)}</b>\n"
            f"🔄 State: <code>{state}</code>\n"
            f"📍 Phase: {self._escape(progress.phase)}\n\n"
            f"📊 Progress: {progress_line}\n"
            f"{progress_bar + chr(10) if progress_bar else ''}"
            f"{rate_bar_line}"
            f"🔎 Source messages scanned: {progress.scanned:,}\n"
            f"🎞️ Media/files found: {progress.eligible:,}\n"
            f"✅ Sent: {counters.completed:,}\n"
            f"♻️ Already copied (resume protection): {progress.skipped:,}\n"
            f"⚠️ Failed: {max(counters.failed, progress.failed_this_run):,}\n"
            f"📦 Media reused: {readable_bytes(counters.bytes_transferred)}\n"
            f"⚡ Effective speed: {readable_bytes(speed)}/s\n"
            f"{eta_line}\n"
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
    def _progress_bar(percentage: float, width: int = 10) -> str:
        filled = max(0, min(width, round(width * percentage / 100)))
        return "▰" * filled + "▱" * (width - filled)

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
    """Fair scheduler for the shared public-bot worker pool.

    The application can serve many users while keeping a controlled number of
    Telegram client jobs active. One user cannot occupy every active slot.
    """

    def __init__(self, worker: BackupWorker, database: Database) -> None:
        self.worker = worker
        self.database = database
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.queued_project_ids: list[str] = []
        self._lock = asyncio.Lock()

    def is_running(self, project_id: str) -> bool:
        task = self.tasks.get(project_id)
        return bool(task and not task.done())

    def queue_position(self, project_id: str) -> int | None:
        try:
            return self.queued_project_ids.index(project_id) + 1
        except ValueError:
            return None

    def live_progress(self, project_id: str) -> ScanProgress | None:
        return self.worker.live_progress(project_id)

    def _owner_active_count(self, owner_id: int) -> int:
        active = 0
        for running_project_id, task in self.tasks.items():
            if task.done():
                continue
            project = self.database.get_project(running_project_id)
            if project and project.owner_id == owner_id:
                active += 1
        return active

    def _has_capacity(self, project: Project) -> bool:
        active_total = sum(1 for task in self.tasks.values() if not task.done())
        return (
            active_total < self.worker.settings.max_concurrent_backups
            and self._owner_active_count(project.owner_id) < self.worker.settings.max_active_projects_per_user
        )

    async def start(self, project_id: str) -> str:
        """Start now or place the project into a durable fair queue."""
        async with self._lock:
            if self.is_running(project_id):
                return "ALREADY_RUNNING"
            project = self.database.get_project(project_id)
            if project is None:
                raise ValueError("Project does not exist")
            if project.status in {ProjectStatus.COMPLETED, ProjectStatus.STOPPED, ProjectStatus.FAILED, ProjectStatus.PAUSED}:
                self.database.update_project_status(project_id, ProjectStatus.READY)
                project = self.database.get_project(project_id)
            if not self._has_capacity(project):
                if project_id not in self.queued_project_ids:
                    self.queued_project_ids.append(project_id)
                self.database.update_project_status(project_id, ProjectStatus.QUEUED)
                self.database.log_event(project_id, "INFO", "Queued by fair scheduler")
                return "QUEUED"
            self._launch(project_id)
            return "STARTED"

    def _launch(self, project_id: str) -> None:
        task = asyncio.create_task(self._run_and_cleanup(project_id), name=f"backup-{project_id}")
        self.tasks[project_id] = task

    async def _run_and_cleanup(self, project_id: str) -> None:
        try:
            await self.worker.run(project_id)
        finally:
            self.tasks.pop(project_id, None)
            await self._drain_queue()

    async def _drain_queue(self) -> None:
        async with self._lock:
            for project_id in list(self.queued_project_ids):
                project = self.database.get_project(project_id)
                if project is None or project.status != ProjectStatus.QUEUED:
                    self.queued_project_ids.remove(project_id)
                    continue
                if not self._has_capacity(project):
                    continue
                self.queued_project_ids.remove(project_id)
                self._launch(project_id)
                # Continue to use any remaining global slots for other owners.

    def request_pause(self, project_id: str) -> None:
        if project_id in self.queued_project_ids:
            self.queued_project_ids.remove(project_id)
            self.database.update_project_status(project_id, ProjectStatus.PAUSED)
            return
        self.database.request_pause(project_id)

    def request_stop(self, project_id: str) -> None:
        if project_id in self.queued_project_ids:
            self.queued_project_ids.remove(project_id)
            self.database.update_project_status(project_id, ProjectStatus.STOPPED)
            return
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
