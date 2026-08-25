from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_RATE_LIMIT = "WAITING_RATE_LIMIT"


class ScanMode(StrEnum):
    FULL = "FULL"
    FROM_MESSAGE_ID = "FROM_MESSAGE_ID"
    DATE_RANGE = "DATE_RANGE"
    NEW_FILES_ONLY = "NEW_FILES_ONLY"


class TransferStatus(StrEnum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"


class MediaType(StrEnum):
    DOCUMENT = "DOCUMENT"
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    VOICE = "VOICE"
    VIDEO_NOTE = "VIDEO_NOTE"
    STICKER = "STICKER"
    OTHER = "OTHER"


@dataclass(slots=True)
class ProjectSettings:
    media_types: list[str] = field(
        default_factory=lambda: [
            MediaType.DOCUMENT,
            MediaType.PHOTO,
            MediaType.VIDEO,
            MediaType.AUDIO,
            MediaType.VOICE,
            MediaType.VIDEO_NOTE,
            MediaType.OTHER,
        ]
    )
    preserve_captions: bool = False
    preserve_albums: bool = True
    skip_duplicates: bool = True
    ordering: str = "OLDEST_FIRST"
    checksum_enabled: bool = False
    continuous_sync: bool = False

    def allows(self, media_type: MediaType) -> bool:
        return media_type.value in {str(value) for value in self.media_types}

    def to_json(self) -> str:
        data = asdict(self)
        data["media_types"] = [str(item) for item in self.media_types]
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> ProjectSettings:
        if not raw:
            return cls()
        payload = json.loads(raw)
        return cls(**payload)


@dataclass(slots=True)
class Project:
    id: str
    owner_id: int
    profile_id: int
    name: str
    source_ref: str
    destination_ref: str
    source_chat_id: int | None
    destination_chat_id: int | None
    source_name: str | None
    destination_name: str | None
    scan_mode: ScanMode
    start_message_id: int | None
    start_date: str | None
    end_date: str | None
    status: ProjectStatus
    settings: ProjectSettings
    checkpoint_message_id: int | None
    status_message_chat_id: int | None
    status_message_id: int | None
    created_at: str
    updated_at: str
    last_error: str | None = None

    @classmethod
    def draft(
        cls,
        *,
        owner_id: int,
        profile_id: int,
        name: str,
        source_ref: str,
        destination_ref: str,
        scan_mode: ScanMode = ScanMode.FULL,
        settings: ProjectSettings | None = None,
    ) -> Project:
        now = utcnow()
        return cls(
            id=str(uuid4()),
            owner_id=owner_id,
            profile_id=profile_id,
            name=name.strip(),
            source_ref=source_ref.strip(),
            destination_ref=destination_ref.strip(),
            source_chat_id=None,
            destination_chat_id=None,
            source_name=None,
            destination_name=None,
            scan_mode=scan_mode,
            start_message_id=None,
            start_date=None,
            end_date=None,
            status=ProjectStatus.DRAFT,
            settings=settings or ProjectSettings(),
            checkpoint_message_id=None,
            status_message_chat_id=None,
            status_message_id=None,
            created_at=now,
            updated_at=now,
        )


@dataclass(slots=True)
class ProjectCounters:
    scanned: int = 0
    eligible: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_transferred: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ProjectCounters:
        return cls(
            scanned=int(row.get("scanned", 0)),
            eligible=int(row.get("eligible", 0)),
            completed=int(row.get("completed", 0)),
            skipped=int(row.get("skipped", 0)),
            failed=int(row.get("failed", 0)),
            bytes_transferred=int(row.get("bytes_transferred", 0)),
        )
