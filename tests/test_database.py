from pathlib import Path

from app.database import Database
from app.models import Project, ProjectStatus, TransferStatus


def make_database() -> Database:
    database = Database(Path(":memory:"))
    database.initialize()
    database.ensure_user(1001)
    return database


def test_project_transfer_ledger_is_durable_and_deduplicated() -> None:
    database = make_database()
    profile_id = database.ensure_profile(1001)
    project = Project.draft(
        owner_id=1001,
        profile_id=profile_id,
        name="Archive",
        source_ref="-100111",
        destination_ref="-100222",
    )
    database.create_project(project)
    database.update_project_resolution(project.id, 111, "Source", 222, "Destination")

    database.begin_transfer(
        project_id=project.id,
        source_chat_id=111,
        source_message_id=88,
        media_type="DOCUMENT",
        file_name="archive.zip",
        file_size=1024,
        status=TransferStatus.DOWNLOADING,
    )
    database.complete_transfer(
        project_id=project.id,
        source_chat_id=111,
        source_message_id=88,
        destination_chat_id=222,
        destination_message_id=900,
        checksum_sha256=None,
    )

    assert database.transfer_completed(project.id, 111, 88)
    counters = database.counters(project.id)
    assert counters.completed == 1
    assert counters.bytes_transferred == 1024
    assert database.get_project(project.id).status == ProjectStatus.READY
    database.close()


def test_incomplete_transfers_are_retryable_after_restart() -> None:
    database = make_database()
    profile_id = database.ensure_profile(1001)
    project = Project.draft(
        owner_id=1001,
        profile_id=profile_id,
        name="Resume test",
        source_ref="source",
        destination_ref="destination",
    )
    database.create_project(project)
    database.begin_transfer(
        project_id=project.id,
        source_chat_id=1,
        source_message_id=50,
        media_type="PHOTO",
        file_name="photo.jpg",
        file_size=10,
        status=TransferStatus.UPLOADING,
    )
    assert database.cleanup_incomplete_items() == 1
    assert database.retryable_source_message_ids(project.id) == [50]
    database.close()


def test_forum_topic_and_admin_summaries() -> None:
    database = make_database()
    profile_id = database.ensure_profile(1001)
    project = Project.draft(
        owner_id=1001,
        profile_id=profile_id,
        name="Forum clone",
        source_ref="source",
        destination_ref="destination",
    )
    database.create_project(project)
    database.save_forum_topic(project.id, 42, 142, "Movies")
    assert database.destination_topic_id(project.id, 42) == 142
    assert database.forum_topic_count(project.id) == 1
    summary = database.global_admin_summary()
    assert summary["users"] == 1
    assert summary["projects"] == 1
    database.close()
