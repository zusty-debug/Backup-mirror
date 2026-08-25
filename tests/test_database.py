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
    database.set_status_message(project.id, 1001, 500)
    assert database.project_by_status_message(1001, 500).id == project.id
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
    database.save_forum_channel_segment(project.id, 42, 901, "Movies", pinned=True)
    assert database.destination_topic_id(project.id, 42) == 142
    assert database.forum_topic_count(project.id) == 1
    segment = database.forum_channel_segment(project.id, 42)
    assert segment["destination_header_message_id"] == 901
    assert segment["pinned"] == 1
    database.save_project_plan(project.id, 120, 85, {"DOCUMENT": 80, "TEXT": 5})
    plan = database.project_plan(project.id)
    assert plan["scanned_total"] == 120
    assert plan["selected_total"] == 85
    assert plan["breakdown"]["DOCUMENT"] == 80
    database.save_worker_pacing(profile_id, 60, 25, 120)
    pacing = database.worker_pacing(profile_id, 40)
    assert pacing["sends_per_minute"] == 60
    assert pacing["successful_messages_since_adjustment"] == 25
    assert pacing["last_flood_wait_seconds"] == 120
    summary = database.global_admin_summary()
    assert summary["users"] == 1
    assert summary["projects"] == 1
    database.close()


async def test_fair_scheduler_queues_second_project_for_same_user() -> None:
    from types import SimpleNamespace

    from app.worker import WorkerManager

    database = make_database()
    profile_id = database.ensure_profile(1001)
    first = Project.draft(owner_id=1001, profile_id=profile_id, name="First", source_ref="a", destination_ref="b")
    second = Project.draft(owner_id=1001, profile_id=profile_id, name="Second", source_ref="c", destination_ref="d")
    database.create_project(first)
    database.create_project(second)

    first_release = __import__("asyncio").Event()
    second_release = __import__("asyncio").Event()

    class FakeWorker:
        settings = SimpleNamespace(max_concurrent_backups=2, max_active_projects_per_user=1)

        async def run(self, project_id: str) -> None:
            if project_id == first.id:
                await first_release.wait()
            else:
                await second_release.wait()

    manager = WorkerManager(FakeWorker(), database)
    assert await manager.start(first.id) == "STARTED"
    assert await manager.start(second.id) == "QUEUED"
    assert database.get_project(second.id).status == ProjectStatus.QUEUED
    assert manager.queue_position(second.id) == 1

    first_release.set()
    await __import__("asyncio").sleep(0)
    await __import__("asyncio").sleep(0)
    assert manager.is_running(second.id)
    second_release.set()
    await manager.shutdown()
    database.close()
