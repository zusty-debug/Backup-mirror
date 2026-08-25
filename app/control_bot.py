from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field, replace

from telethon import Button, TelegramClient, errors, events, functions
from telethon.errors import RPCError
from telethon.sessions import StringSession

from .config import Settings
from .database import Database
from .models import MediaType, Project, ProjectSettings, ProjectStatus, ScanMode
from .reports import build_project_report
from .telegram_gateway import ProfileNotConnectedError, TelegramGateway, TelegramGatewayError
from .utils import readable_bytes, truncate
from .worker import WorkerManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Flow:
    stage: str
    data: dict[str, object] = field(default_factory=dict)


class TelegramControlBot:
    """Small Telethon-only control surface to keep the worker within low-memory hosting limits."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        gateway: TelegramGateway,
        workers: WorkerManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.workers = workers
        self.client = TelegramClient(
            StringSession(),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            device_model="Telegram Media Mirror Control Bot",
            system_version="Linux",
            app_version="0.1.0",
        )
        self.flows: dict[int, Flow] = {}
        self.plan_scan_tasks: dict[str, asyncio.Task[None]] = {}
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_callback, events.CallbackQuery())

    async def start(self) -> None:
        await self.client.start(bot_token=self.settings.bot_token)
        me = await self.client.get_me()
        logger.info("Telegram Media Mirror Bot started as @%s", me.username)
        await self.workers.resume_after_restart()
        await self.client.run_until_disconnected()

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> None:
        project = self.database.project_by_status_message(chat_id, message_id)
        buttons = [[Button.inline("🔄 Refresh Live Status", f"live:{project.id}".encode())]] if project else None
        await self.client.edit_message(chat_id, message_id, self._brand(text), parse_mode="html", buttons=buttons)

    @staticmethod
    def _brand(text: str) -> str:
        footer = "<i>Developed by — @xzusty</i>"
        return text if footer in text else f"{text}\n\n{footer}"

    async def _reply(self, event, text: str, *args, **kwargs):
        kwargs.setdefault("parse_mode", "html")
        return await event.respond(self._brand(text), *args, **kwargs)

    async def _edit_reply(self, event, text: str, *args, **kwargs):
        kwargs.setdefault("parse_mode", "html")
        try:
            return await event.edit(self._brand(text), *args, **kwargs)
        except errors.MessageNotModifiedError:
            return None

    @staticmethod
    def _allowed(user_id: int | None) -> bool:
        # Public service: every real Telegram user can create isolated projects.
        return bool(user_id)

    def _is_admin(self, user_id: int | None) -> bool:
        return bool(user_id and user_id in self.settings.owner_ids)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        user_id = event.sender_id
        if not self._allowed(user_id):
            return
        text = (event.raw_text or "").strip()
        if not text:
            return
        self.database.ensure_user(user_id)
        if text.startswith("/start"):
            self.flows.pop(user_id, None)
            await self._send_menu(event, "<b>✨ Telegram Media Backup Bot</b>\n\nChoose an action below.")
            return
        if text.startswith("/help"):
            await self._reply(event, self._help_text(), parse_mode="html")
            return
        if text.startswith("/connect"):
            await self._begin_connect(event)
            return
        if text.startswith("/new"):
            await self._begin_new(event)
            return
        if text.startswith("/projects"):
            await self._send_projects(event, user_id)
            return
        flow = self.flows.get(user_id)
        if flow:
            await self._advance_flow(event, user_id, text, flow)

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        user_id = event.sender_id
        if not self._allowed(user_id):
            await event.answer("Not authorised", alert=True)
            return
        self.database.ensure_user(user_id)
        data = event.data.decode("utf-8", "replace")
        try:
            await self._handle_callback(event, user_id, data)
        except Exception as exc:
            logger.exception("Callback failure: %s", data)
            await event.answer(f"Error: {truncate(str(exc), 120)}", alert=True)

    async def _handle_callback(self, event: events.CallbackQuery.Event, user_id: int, data: str) -> None:
        if data == "noop":
            await event.answer("Scan is in progress")
            return
        if data == "account:connect":
            await event.answer()
            await self._begin_connect(event)
            return
        if data == "project:new":
            await event.answer()
            await self._begin_new(event)
            return
        if data == "project:list":
            await event.answer()
            await self._edit_projects(event, user_id)
            return
        if data == "worker:status":
            await event.answer()
            await self._edit_worker_status(event, user_id)
            return
        if data in {"admin", "admin:refresh", "admin:workers", "admin:active"}:
            if not self._is_admin(user_id):
                await event.answer("Admin access only", alert=True)
                return
            await event.answer()
            if data == "admin:workers":
                await self._edit_admin_workers(event)
            elif data == "admin:active":
                await self._edit_admin_active_projects(event)
            else:
                await self._edit_admin_panel(event, user_id)
            return
        if data == "help":
            await event.answer()
            await self._reply(event, self._help_text(), parse_mode="html")
            return
        if data.startswith("topic:") or data.startswith("topics:"):
            await event.answer()
            await self._choose_forum_topics(event, user_id, data)
            return
        if data.startswith("filter:"):
            await event.answer()
            await self._toggle_media_filter(event, user_id, data)
            return
        if data.startswith("content:"):
            await event.answer()
            await self._choose_content(event, user_id, data)
            return
        if data.startswith("mode:"):
            await event.answer()
            await self._choose_mode(event, user_id, data.split(":", 1)[1])
            return
        action, _, project_id = data.partition(":")
        project = self.database.get_project(project_id, user_id) if project_id else None
        if action == "live":
            if not project:
                await event.answer("Project not found", alert=True)
                return
            await event.answer("Live status refreshed")
            await self._edit_live_status(event, project)
            return
        project_actions = {
            "confirm", "cancel", "view", "start", "pause", "stopask", "stop", "status",
            "settings", "mediafilter", "caption", "sync", "report", "failed", "activity", "verify", "preview", "retry",
            "duplicate", "deleteask", "delete",
        }
        if action in project_actions and not project:
            await event.answer("Project not found", alert=True)
            return
        if action == "confirm":
            self.flows.pop(user_id, None)
            await event.answer("Project created")
            await self._edit_project(event, project)
        elif action == "cancel":
            self.database.delete_project(project.id, user_id)
            self.flows.pop(user_id, None)
            await event.answer("Project cancelled")
            await self._edit_projects(event, user_id)
        elif action == "view":
            await event.answer()
            await self._edit_project(event, project)
        elif action == "start":
            if not self.gateway.has_session(user_id):
                await event.answer("Connect worker account first", alert=True)
                return
            if not self.database.project_plan(project.id):
                await event.answer("Scan selected content first")
                await self._edit_reply(
                    event,
                    f"<b>🧮 Scan required — {self._esc(project.name)}</b>\n\n"
                    "First count every valid selected source message. Then the bot will show the exact total and ask for final sending confirmation.",
                    buttons=[
                        [Button.inline("🔍 Scan Selected Content", f"preview:{project.id}".encode())],
                        [Button.inline("⬅️ Project", f"view:{project.id}".encode())],
                    ],
                )
                return
            status = await self.client.send_message(
                event.chat_id,
                self._brand(f"<b>📡 Live backup status</b>\n📁 Project: <b>{self._esc(project.name)}</b>\n🚀 Preparing…"),
                parse_mode="html",
                buttons=[[Button.inline("🔄 Refresh Live Status", f"live:{project.id}".encode())]],
            )
            self.database.set_status_message(project.id, int(event.chat_id), int(status.id))
            start_result = await self.workers.start(project.id)
            if start_result == "QUEUED":
                position = self.workers.queue_position(project.id)
                await event.answer(f"Queued at position {position}")
            elif start_result == "ALREADY_RUNNING":
                await event.answer("Backup is already running")
            else:
                await event.answer("Backup started")
            await self._edit_project(event, self.database.get_project(project.id, user_id))
        elif action == "pause":
            if self.workers.is_running(project.id):
                self.workers.request_pause(project.id)
                notice = "Pause requested"
            else:
                self.database.update_project_status(project.id, ProjectStatus.PAUSED)
                notice = "Project paused"
            await event.answer(notice)
            await self._edit_project(event, self.database.get_project(project.id, user_id))
        elif action == "stopask":
            await event.answer()
            await self._edit_reply(event, 
                f"<b>Stop {self._esc(project.name)}?</b>\n\nProgress is retained for resume.",
                parse_mode="html",
                buttons=[[Button.inline("Yes, Stop", f"stop:{project.id}".encode())], [Button.inline("Cancel", f"view:{project.id}".encode())]],
            )
        elif action == "stop":
            if self.workers.is_running(project.id):
                self.workers.request_stop(project.id)
                notice = "Stop requested"
            else:
                self.database.update_project_status(project.id, ProjectStatus.STOPPED)
                notice = "Project stopped"
            await event.answer(notice)
            await self._edit_project(event, self.database.get_project(project.id, user_id))
        elif action == "status":
            await event.answer()
            await self._edit_project(event, project, show_counters=True)
        elif action == "verify":
            try:
                source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
                self.database.update_project_resolution(
                    project.id,
                    int(source.id),
                    self.gateway.entity_name(source),
                    int(destination.id),
                    self.gateway.entity_name(destination),
                )
                self.database.log_event(project.id, "INFO", "Manual source/destination verification passed")
                await event.answer("Source and destination verified")
            except TelegramGatewayError as exc:
                self.database.log_event(project.id, "ERROR", f"Manual verification failed: {exc}")
                await event.answer(f"Verification failed: {truncate(str(exc), 90)}", alert=True)
            await self._edit_project(event, self.database.get_project(project.id, user_id))
        elif action == "preview":
            existing = self.plan_scan_tasks.get(project.id)
            if existing and not existing.done():
                await event.answer("Source scan is already running")
                return
            await event.answer("Started live source scan")
            await self._edit_reply(
                event,
                f"<b>🧮 Scanning selected content — {self._esc(project.name)}</b>\n\n"
                "Preparing source scan…\n\n"
                "No messages will be sent until you approve the final total.",
                buttons=[[Button.inline("⏳ Scan in progress", b"noop")]],
            )
            task = asyncio.create_task(
                self._run_plan_scan(project.id, int(event.chat_id), int(event.message_id)),
                name=f"plan-scan-{project.id}",
            )
            self.plan_scan_tasks[project.id] = task
        elif action == "activity":
            rows = self.database.recent_events(project.id)
            lines = [f"<b>📜 Activity — {self._esc(project.name)}</b>", ""]
            if not rows:
                lines.append("No recorded events yet.")
            else:
                for row in rows:
                    icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌"}.get(str(row["level"]), "•")
                    lines.append(
                        f"{icon} <code>{self._esc(str(row['created_at']))}</code>\n"
                        f"   {self._esc(truncate(str(row['event']), 180))}"
                    )
            await event.answer()
            await self._edit_reply(event, "\n".join(lines), buttons=[[Button.inline("⬅️ Project", f"view:{project.id}".encode())]])
        elif action == "retry":
            status = await self.workers.start(project.id)
            await event.answer("Retry queued" if status == "QUEUED" else "Retry started")
            await self._edit_project(event, self.database.get_project(project.id, user_id))
        elif action == "duplicate":
            clone = Project.draft(
                owner_id=user_id,
                profile_id=project.profile_id,
                name=f"Copy of {project.name}"[:100],
                source_ref=project.source_ref,
                destination_ref=project.destination_ref,
                scan_mode=project.scan_mode,
                settings=project.settings,
            )
            clone.start_message_id = project.start_message_id
            clone.start_date = project.start_date
            clone.end_date = project.end_date
            self.database.create_project(clone)
            self.database.update_project_resolution(
                clone.id,
                int(project.source_chat_id),
                project.source_name or project.source_ref,
                int(project.destination_chat_id),
                project.destination_name or project.destination_ref,
            )
            await event.answer("Project configuration duplicated")
            await self._edit_project(event, self.database.get_project(clone.id, user_id))
        elif action == "mediafilter":
            await event.answer()
            await self._edit_media_filters(event, project)
        elif action == "settings":
            await event.answer()
            await self._edit_settings(event, project)
        elif action in {"caption", "sync"}:
            settings = project.settings
            if action == "caption":
                settings = replace(settings, preserve_captions=not settings.preserve_captions)
            else:
                settings = replace(settings, continuous_sync=not settings.continuous_sync)
            self.database.update_project_settings(project.id, settings)
            await event.answer("Setting updated")
            await self._edit_settings(event, self.database.get_project(project.id, user_id))
        elif action == "report":
            report = build_project_report(self.database, project, self.settings.report_dir)
            await self.client.send_file(
                event.chat_id,
                str(report),
                caption=f"📄 Full report — {project.name}\nDeveloped by — @xzusty",
            )
            await event.answer("Report sent")
        elif action == "failed":
            rows = self.database.failed_items(project.id, 30)
            if not rows:
                text = "No failed transfer records."
            else:
                text = "<b>Failed files</b>\n" + "\n".join(
                    f"• #{row['source_message_id']} — <code>{self._esc(truncate(row['file_name'] or 'Unknown', 45))}</code>"
                    for row in rows
                )
            await self._reply(event, text, parse_mode="html")
            await event.answer()
        elif action == "deleteask":
            await event.answer()
            await self._edit_reply(event, 
                f"<b>Delete {self._esc(project.name)}?</b>",
                parse_mode="html",
                buttons=[[Button.inline("Yes, Delete", f"delete:{project.id}".encode())], [Button.inline("Cancel", f"view:{project.id}".encode())]],
            )
        elif action == "delete":
            if self.workers.is_running(project.id):
                await event.answer("Stop it first", alert=True)
                return
            self.database.delete_project(project.id, user_id)
            await event.answer("Deleted")
            await self._edit_projects(event, user_id)

    async def _begin_connect(self, event) -> None:
        self.flows[event.sender_id] = Flow("phone")
        await self._reply(event, "Send the worker account phone number, for example <code>+15551234567</code>.", parse_mode="html")

    async def _begin_new(self, event) -> None:
        if not self.gateway.has_session(event.sender_id):
            await self._reply(event, "Connect the worker account first using <code>/connect</code>.", parse_mode="html")
            return
        if self.database.count_projects(event.sender_id) >= self.settings.max_projects_per_owner:
            await self._reply(event, "Project limit reached.")
            return
        self.flows[event.sender_id] = Flow("source")
        await self._reply(event, "Send the source channel/group username, numeric ID, or Telegram link.")

    async def _advance_flow(self, event: events.NewMessage.Event, user_id: int, text: str, flow: Flow) -> None:
        if flow.stage == "phone":
            try:
                hint = await self.gateway.begin_login(user_id, text)
            except TelegramGatewayError as exc:
                await self._reply(event, f"Could not request code: <code>{self._esc(str(exc))}</code>", parse_mode="html")
                return
            self.flows[user_id] = Flow("code")
            await self._reply(event, f"Code sent to account ending <code>{hint}</code>. Send the code.", parse_mode="html")
        elif flow.stage == "code":
            try:
                complete = await self.gateway.finish_login_code(user_id, text)
            except TelegramGatewayError as exc:
                await self._reply(event, f"Login error: <code>{self._esc(str(exc))}</code>", parse_mode="html")
                return
            if complete:
                self.flows.pop(user_id, None)
                await self._send_menu(event, "Worker account connected.")
            else:
                self.flows[user_id] = Flow("password")
                await self._reply(event, "Send the two-step-verification password.")
        elif flow.stage == "password":
            try:
                await self.gateway.finish_login_password(user_id, text)
            except TelegramGatewayError as exc:
                await self._reply(event, f"Login error: <code>{self._esc(str(exc))}</code>", parse_mode="html")
                return
            self.flows.pop(user_id, None)
            await self._send_menu(event, "Worker account connected.")
        elif flow.stage == "source":
            flow.data["source_ref"] = text
            try:
                topics = await self.gateway.forum_topics(self.gateway.default_profile_id(user_id), text)
            except TelegramGatewayError as exc:
                await self._reply(event, f"Unable to inspect source: <code>{self._esc(str(exc))}</code>")
                return
            if topics:
                flow.data["forum_topics"] = topics
                flow.data["selected_topic_ids"] = []
                flow.stage = "forum_topics"
                await self._reply(
                    event,
                    "<b>🧵 Select forum topics</b>\n\nTap one or more topics, then press <b>Done</b>.",
                    buttons=self._forum_topic_buttons(flow),
                )
                return
            flow.stage = "destination"
            await self._reply(
                event,
                "Send the destination channel/group username, numeric ID, or Telegram link.\n\n"
                "For a forum source, you may instead send <code>CREATE_FORUM</code> to create a new forum clone with matching topics.",
            )
        elif flow.stage == "destination":
            flow.data["destination_ref"] = text
            flow.stage = "name"
            await self._reply(event, "Give this backup project a name.")
        elif flow.stage == "name":
            name = " ".join(text.split())
            if not name:
                await self._reply(event, "Project name cannot be empty.")
                return
            flow.data["name"] = name[:100]
            flow.data["selected_content"] = []
            flow.stage = "content"
            await self._reply(
                event,
                "<b>📦 Select content to copy</b>\n\nTap one or more of Files, Media, and Links. Everything is an all-content mode.",
                buttons=self._content_buttons(flow),
            )
        elif flow.stage == "message_id":
            try:
                flow.data["start_message_id"] = int(text)
                if int(text) <= 0:
                    raise ValueError
            except ValueError:
                await self._reply(event, "Send a positive message ID or a Telegram message link.")
                return
            await self._create_project(event, user_id, flow)
        elif flow.stage == "start_link":
            message_id = self._message_id_from_reference(text)
            if not message_id:
                await self._reply(event, "Send a valid Telegram message link or a positive message ID.")
                return
            flow.data["start_message_id"] = message_id
            await self._create_project(event, user_id, flow)
    def _forum_topic_buttons(self, flow: Flow):
        selected = {int(topic_id) for topic_id in flow.data.get("selected_topic_ids", [])}
        rows = []
        for topic in flow.data.get("forum_topics", [])[:20]:
            topic_id = int(topic["id"])
            mark = "✅" if topic_id in selected else "⬜"
            title = truncate(str(topic["title"]), 28)
            rows.append([Button.inline(f"{mark} {title}", f"topic:toggle:{topic_id}".encode())])
        rows.append([Button.inline("✅ Select all", b"topics:all"), Button.inline("➡️ Done", b"topics:done")])
        return rows

    async def _choose_forum_topics(self, event: events.CallbackQuery.Event, user_id: int, data: str) -> None:
        flow = self.flows.get(user_id)
        if not flow or flow.stage != "forum_topics":
            await event.answer("Start a new project again", alert=True)
            return
        topics = flow.data.get("forum_topics", [])
        selected = [int(topic_id) for topic_id in flow.data.get("selected_topic_ids", [])]
        if data.startswith("topic:toggle:"):
            topic_id = int(data.rsplit(":", 1)[1])
            if topic_id in selected:
                selected.remove(topic_id)
            else:
                # Preserve the user's selection order for channel segment ordering.
                selected.append(topic_id)
            flow.data["selected_topic_ids"] = selected
            await self._edit_reply(
                event,
                "<b>🧵 Select forum topics</b>\n\nTap one or more topics, then press <b>Done</b>.",
                buttons=self._forum_topic_buttons(flow),
            )
            return
        if data == "topics:all":
            flow.data["selected_topic_ids"] = [int(topic["id"]) for topic in topics]
            await self._edit_reply(
                event,
                "<b>🧵 Select forum topics</b>\n\nAll displayed topics selected. Press <b>Done</b>.",
                buttons=self._forum_topic_buttons(flow),
            )
            return
        if data == "topics:done":
            if not selected:
                await event.answer("Select at least one topic", alert=True)
                return
            flow.stage = "destination"
            await self._edit_reply(
                event,
                "🧵 Topics selected.\n\n"
                "Send <code>CREATE_CHANNEL</code> to create a normal destination channel. "
                "The bot will post selected topics one after another with a pinned topic header.\n\n"
                "<code>CREATE_FORUM</code> remains available only for Premium worker accounts.",
            )

    def _content_buttons(self, flow: Flow):
        selected = set(flow.data.get("selected_content", []))
        def label(key: str, emoji: str, title: str) -> str:
            return f"{'✅' if key in selected else '⬜'} {emoji} {title}"
        return [
            [Button.inline(label("files", "📄", "Files"), b"content:toggle:files"), Button.inline(label("media", "🎞️", "Media"), b"content:toggle:media")],
            [Button.inline(label("links", "🔗", "Links"), b"content:toggle:links")],
            [Button.inline(label("everything", "✨", "Everything"), b"content:toggle:everything")],
            [Button.inline("➡️ Done", b"content:done")],
        ]

    async def _choose_content(self, event: events.CallbackQuery.Event, user_id: int, data: str) -> None:
        flow = self.flows.get(user_id)
        if not flow or flow.stage != "content":
            await event.answer("Start a new project again", alert=True)
            return
        selected = set(flow.data.get("selected_content", []))
        if data.startswith("content:toggle:"):
            choice = data.rsplit(":", 1)[1]
            if choice == "everything":
                selected = set() if "everything" in selected else {"everything"}
            elif choice in {"files", "media", "links"}:
                selected.discard("everything")
                if choice in selected:
                    selected.remove(choice)
                else:
                    selected.add(choice)
            flow.data["selected_content"] = sorted(selected)
            await self._edit_reply(
                event,
                "<b>📦 Select content to copy</b>\n\nTap one or more of Files, Media, and Links. Everything is an all-content mode.",
                buttons=self._content_buttons(flow),
            )
            return
        if data != "content:done" or not selected:
            await event.answer("Select at least one content type", alert=True)
            return
        if "everything" in selected:
            mode = "EVERYTHING"
        elif selected == {"files"}:
            mode = "FILES"
        elif selected == {"media"}:
            mode = "MEDIA"
        elif selected == {"links"}:
            mode = "LINKS"
        elif selected == {"files", "media"}:
            mode = "MEDIA_FILES"
        else:
            mode = "MEDIA_FILES_LINKS"
        flow.data["settings"] = ProjectSettings(
            content_mode=mode,
            preserve_captions=mode == "EVERYTHING",
            forum_topic_ids=[int(topic_id) for topic_id in flow.data.get("selected_topic_ids", [])],
        )
        flow.stage = "mode"
        await self._edit_reply(
            event,
            "<b>🧭 Choose where to start</b>\n\n"
            "Custom start is available for channels and regular groups. "
            "Forum-topic copies always start from the beginning of the selected topics.",
            buttons=[
                [Button.inline("⏮️ From the beginning", b"mode:full")],
                [Button.inline("🆕 New media only + 300s idle stop", b"mode:new")],
                [Button.inline("📍 Custom message link / ID", b"mode:custom")],
            ],
        )

    async def _choose_mode(self, event: events.CallbackQuery.Event, user_id: int, choice: str) -> None:
        flow = self.flows.get(user_id)
        if not flow or flow.stage != "mode":
            await event.answer("Start a new project again", alert=True)
            return
        settings = flow.data.get("settings", ProjectSettings())
        if choice == "full":
            flow.data["scan_mode"] = ScanMode.FULL
            await self._create_project(event, user_id, flow)
        elif choice == "new":
            flow.data["scan_mode"] = ScanMode.NEW_FILES_ONLY
            flow.data["settings"] = replace(settings, continuous_sync=True)
            await self._create_project(event, user_id, flow)
        elif choice == "custom":
            flow.data["scan_mode"] = ScanMode.FROM_MESSAGE_ID
            flow.stage = "start_link"
            await self._reply(event, "📍 Send the first source message ID or its Telegram message link.")

    @staticmethod
    def _message_id_from_reference(value: str) -> int | None:
        match = re.search(r"(?:^|/)(\d+)(?:\?.*)?$", value.strip())
        if match:
            return int(match.group(1))
        return int(value) if value.strip().isdigit() and int(value) > 0 else None

    async def _prepare_forum_topic_mapping(self, project: Project, *, create_missing: bool) -> None:
        """Map selected source topics to destination topics before a run starts.

        Topic creation is deliberately performed during setup rather than inside
        the backup worker, so capability errors are reported before any copy run.
        """
        selected = {int(topic_id) for topic_id in project.settings.forum_topic_ids}
        if not selected:
            return
        source_topics = await self.gateway.forum_topics(project.profile_id, project.source_ref)
        destination_topics = await self.gateway.forum_topics(project.profile_id, project.destination_ref)
        source_topics = [topic for topic in source_topics if int(topic["id"]) in selected]
        if not source_topics:
            raise TelegramGatewayError("The selected source forum topics are no longer accessible.")
        destination_by_title = {str(topic["title"]).casefold(): topic for topic in destination_topics}
        missing: list[str] = []
        async with self.gateway.client_for_profile(project.profile_id) as client:
            destination = await self.gateway.resolve_entity(client, project.destination_ref)
            for topic in source_topics:
                source_topic_id = int(topic["id"])
                if source_topic_id == 1:
                    self.database.save_forum_topic(project.id, 1, 1, str(topic["title"]))
                    continue
                existing = destination_by_title.get(str(topic["title"]).casefold())
                if existing:
                    self.database.save_forum_topic(project.id, source_topic_id, int(existing["id"]), str(topic["title"]))
                    continue
                if not create_missing:
                    missing.append(str(topic["title"]))
                    continue
                try:
                    created = await client(
                        functions.messages.CreateForumTopicRequest(
                            peer=destination,
                            title=str(topic["title"]),
                            icon_color=topic.get("icon_color") or 0x6FB9F0,
                            icon_emoji_id=topic.get("icon_emoji_id"),
                        )
                    )
                except errors.PremiumAccountRequiredError as exc:
                    raise TelegramGatewayError(
                        "Telegram requires a Premium worker account to create forum topics through the API. "
                        "Create matching destination topics manually, then use that existing forum as destination."
                    ) from exc
                destination_topic_id = None
                for update in getattr(created, "updates", []):
                    new_message = getattr(update, "message", None)
                    if new_message and getattr(new_message, "id", None):
                        destination_topic_id = int(new_message.id)
                        break
                if destination_topic_id is None:
                    refreshed = await self.gateway.forum_topics(project.profile_id, project.destination_ref)
                    matched = next((item for item in refreshed if str(item["title"]).casefold() == str(topic["title"]).casefold()), None)
                    destination_topic_id = int(matched["id"]) if matched else None
                if not destination_topic_id:
                    raise TelegramGatewayError(f"Unable to create/map destination forum topic: {topic['title']}")
                self.database.save_forum_topic(project.id, source_topic_id, destination_topic_id, str(topic["title"]))
        if missing:
            raise TelegramGatewayError(
                "Destination forum is missing these selected topics: " + ", ".join(missing) + ". "
                "Create matching topics manually, then create the project again."
            )

    async def _create_project(self, event, user_id: int, flow: Flow) -> None:
        destination_ref = str(flow.data["destination_ref"])
        settings = flow.data.get("settings", ProjectSettings())
        project = Project.draft(
            owner_id=user_id,
            profile_id=self.gateway.default_profile_id(user_id),
            name=str(flow.data["name"]),
            source_ref=str(flow.data["source_ref"]),
            destination_ref=destination_ref,
            scan_mode=flow.data.get("scan_mode", ScanMode.FULL),
            settings=settings,
        )
        project.start_message_id = flow.data.get("start_message_id")
        try:
            destination_mode = destination_ref.upper()
            if destination_mode in {"CREATE_FORUM", "CREATE_CHANNEL"}:
                async with self.gateway.client_for_profile(project.profile_id) as client:
                    source = await self.gateway.resolve_entity(client, project.source_ref)
                    if project.scan_mode == ScanMode.FROM_MESSAGE_ID and getattr(source, "forum", False):
                        raise TelegramGatewayError("Custom start link/ID is not available for forum-topic copies.")
                    if destination_mode == "CREATE_FORUM":
                        if not getattr(source, "forum", False):
                            raise TelegramGatewayError("CREATE_FORUM can only be used when the source is a forum group with topics.")
                        created = await client(
                            functions.channels.CreateChannelRequest(
                                title=f"{self.gateway.entity_name(source)} Backup"[:128],
                                about="Media mirror forum clone",
                                megagroup=True,
                                forum=True,
                            )
                        )
                        project.settings = replace(project.settings, clone_forum_topics=True)
                    else:
                        created = await client(
                            functions.channels.CreateChannelRequest(
                                title=f"{self.gateway.entity_name(source)} Topics Backup"[:128],
                                about="Sequential topic mirror channel",
                                broadcast=True,
                                megagroup=False,
                            )
                        )
                        if getattr(source, "forum", False):
                            project.settings = replace(project.settings, forum_to_channel_segments=True)
                    destination = created.chats[0]
                    project.destination_ref = f"-100{destination.id}"
                    self.database.create_project(project)
                    self.database.update_project_resolution(
                        project.id,
                        int(source.id),
                        self.gateway.entity_name(source),
                        int(destination.id),
                        self.gateway.entity_name(destination),
                    )
            else:
                self.database.create_project(project)
                source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
                if project.scan_mode == ScanMode.FROM_MESSAGE_ID and getattr(source, "forum", False):
                    raise TelegramGatewayError("Custom start link/ID is available only for channels and groups with topics disabled.")
                if getattr(source, "forum", False) and project.settings.forum_topic_ids:
                    if getattr(destination, "forum", False):
                        project.settings = replace(project.settings, clone_forum_topics=True)
                    elif getattr(destination, "broadcast", False):
                        project.settings = replace(project.settings, forum_to_channel_segments=True)
                    else:
                        raise TelegramGatewayError("Selected forum topics need a destination forum or a destination channel.")
                    self.database.update_project_settings(project.id, project.settings)
                self.database.update_project_resolution(
                    project.id,
                    int(source.id),
                    self.gateway.entity_name(source),
                    int(destination.id),
                    self.gateway.entity_name(destination),
                )
            if project.settings.clone_forum_topics and project.settings.forum_topic_ids:
                await self._prepare_forum_topic_mapping(
                    project,
                    create_missing=destination_mode == "CREATE_FORUM",
                )
            if project.scan_mode == ScanMode.NEW_FILES_ONLY:
                latest = await self.gateway.latest_message_id(project.profile_id, project.source_ref)
                if latest:
                    self.database.update_project_checkpoint(project.id, latest)
        except (TelegramGatewayError, ProfileNotConnectedError, RPCError) as exc:
            self.database.delete_project(project.id, user_id)
            await self._reply(event, f"Project validation failed: <code>{self._esc(str(exc))}</code>", parse_mode="html")
            return
        self.flows[user_id] = Flow("confirm", {"project_id": project.id})
        project = self.database.get_project(project.id, user_id)
        await self._reply(event, self._project_card(project), parse_mode="html", buttons=self._confirm_buttons(project.id))

    async def _send_menu(self, event, text: str) -> None:
        connected = self.gateway.has_session(event.sender_id)
        buttons = []
        if not connected:
            buttons.append([Button.inline("🔐 Connect Worker Account", b"account:connect")])
        buttons.extend(
            [
                [Button.inline("🚀 New Backup Project", b"project:new")],
                [Button.inline("📂 My Projects", b"project:list"), Button.inline("👤 Worker Status", b"worker:status")],
            ]
        )
        if self._is_admin(event.sender_id):
            buttons.append([Button.inline("🛠️ Admin Panel", b"admin")])
        buttons.append([Button.inline("ℹ️ Help", b"help")])
        await self._reply(event, text, parse_mode="html", buttons=buttons)

    async def _send_projects(self, event, user_id: int) -> None:
        projects = self.database.list_projects(user_id)
        await self._reply(event, "<b>My Projects</b>" if projects else "No projects yet.", parse_mode="html", buttons=self._projects_buttons(projects))

    async def _edit_projects(self, event, user_id: int) -> None:
        projects = self.database.list_projects(user_id)
        await self._edit_reply(event, "<b>My Projects</b>" if projects else "No projects yet.", parse_mode="html", buttons=self._projects_buttons(projects))

    async def _run_plan_scan(self, project_id: str, chat_id: int, message_id: int) -> None:
        project = self.database.get_project(project_id)
        if not project:
            return
        last_edit = 0.0

        async def update(phase: str, scanned: int, source_total: int, selected: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < 2 and scanned != source_total:
                return
            if source_total:
                percentage = 100 * scanned / source_total
                filled = round(10 * percentage / 100)
                bar = "▰" * filled + "▱" * (10 - filled)
                scan_line = f"{bar} {percentage:.1f}%\n🔎 Source messages checked: {scanned:,} / {source_total:,}"
            else:
                scan_line = f"▰▰▰▱▱▱▱▱▱▱ counting…\n🔎 Source messages counted: {scanned:,}"
            text = (
                f"<b>🧮 Scanning selected content — {self._esc(project.name)}</b>\n\n"
                f"📍 Phase: {self._esc(phase)}\n"
                f"{scan_line}\n"
                f"📦 Valid selected messages found: {selected:,}\n\n"
                "No messages have been sent."
            )
            try:
                await self.client.edit_message(
                    chat_id,
                    message_id,
                    self._brand(text),
                    parse_mode="html",
                    buttons=[[Button.inline("⏳ Scan in progress", b"noop")]],
                )
            except errors.MessageNotModifiedError:
                pass
            last_edit = now

        try:
            counts = await self.workers.worker.preview(project.id, progress_callback=update)
            total = counts.pop("TOTAL", 0)
            scanned = counts.pop("SCANNED", 0)
            self.database.save_project_plan(project.id, scanned, total, counts)
            breakdown = "\n".join(
                f"• {self._esc(kind.title().replace('_', ' '))}: {amount:,}"
                for kind, amount in sorted(counts.items())
            )
            text = (
                f"<b>✅ Sending plan ready — {self._esc(project.name)}</b>\n\n"
                f"🔎 Source messages scanned: {scanned:,}\n"
                f"📦 Valid selected messages: <b>{total:,}</b>\n\n"
                f"{breakdown or 'No selected content found.'}\n\n"
                "No messages have been sent. Review the total, then press Start Sending."
            )
            self.database.log_event(project.id, "INFO", f"Sending plan approved: {total} selected items")
            await self.client.edit_message(
                chat_id,
                message_id,
                self._brand(text),
                parse_mode="html",
                buttons=[
                    [Button.inline(f"▶️ Start Sending {total:,} Items", f"start:{project.id}".encode())],
                    [Button.inline("🔍 Scan Again", f"preview:{project.id}".encode()), Button.inline("⬅️ Project", f"view:{project.id}".encode())],
                ],
            )
        except Exception as exc:
            self.database.log_event(project.id, "ERROR", f"Plan scan failed: {exc}")
            error_text = (
                f"<b>❌ Scan failed — {self._esc(project.name)}</b>\n\n"
                f"<code>{self._esc(truncate(str(exc), 240))}</code>"
            )
            await self.client.edit_message(
                chat_id,
                message_id,
                self._brand(error_text),
                parse_mode="html",
                buttons=[[Button.inline("🔍 Try Scan Again", f"preview:{project.id}".encode())]],
            )
        finally:
            self.plan_scan_tasks.pop(project_id, None)

    async def _edit_live_status(self, event, project: Project) -> None:
        counters = self.database.counters(project.id)
        progress = self.workers.live_progress(project.id)
        if progress:
            current = truncate(progress.current_file, 70)
            phase = progress.phase
            elapsed = max(0, int(time.monotonic() - progress.started_at))
            total = progress.total_eligible
        else:
            current = "Waiting for worker update"
            phase = "🛡️ Telegram pace protection" if project.status == ProjectStatus.WAITING_RATE_LIMIT else "⏸️ Not actively running"
            elapsed = 0
            total = None
        sent_this_run = max(0, counters.completed - progress.completed_at_start) if progress else counters.completed
        pending_this_pass = max(
            0,
            progress.eligible - sent_this_run - progress.skipped - progress.failed_this_run,
        ) if progress else 0
        processed = counters.completed + (progress.skipped if progress else 0)
        if total is not None:
            percentage = min(100.0, processed * 100 / max(1, total))
            bar = "▰" * round(10 * percentage / 100) + "▱" * (10 - round(10 * percentage / 100))
            progress_line = f"{bar} {percentage:.1f}%\n📊 {processed:,} / {total:,} processed"
        else:
            progress_line = f"📊 {processed:,} processed"
        rate_line = ""
        if progress and progress.rate_wait_total is not None and progress.rate_wait_remaining is not None:
            wait_pct = 100 * (progress.rate_wait_total - progress.rate_wait_remaining) / max(1, progress.rate_wait_total)
            wait_bar = "▰" * round(10 * wait_pct / 100) + "▱" * (10 - round(10 * wait_pct / 100))
            rate_line = f"\n🛡️ Pace timer: {wait_bar} {wait_pct:.1f}%\n⏳ Resumes in: {progress.rate_wait_remaining}s"
        state = "🛡️ Telegram pace protection" if project.status == ProjectStatus.WAITING_RATE_LIMIT else project.status.value
        text = (
            "<b>📡 Live backup status</b>\n"
            f"📁 Project: <b>{self._esc(project.name)}</b>\n"
            f"🔄 State: <code>{state}</code>\n"
            f"📍 Phase: {self._esc(phase)}\n\n"
            f"{progress_line}{rate_line}\n"
            f"✅ Sent this pass: {sent_this_run:,}\n"
            f"🟡 Pending/retrying this pass: {pending_this_pass:,}\n"
            f"♻️ Already copied: {progress.skipped if progress else 0:,}\n"
            f"⚠️ Failed: {counters.failed:,}\n"
            f"📦 Media reused: {readable_bytes(counters.bytes_transferred)}\n"
            f"⏱️ Elapsed: {elapsed}s\n"
            f"📌 Current: <code>{self._esc(current)}</code>"
        )
        await self._edit_reply(
            event,
            text,
            buttons=[[Button.inline("🔄 Refresh Live Status", f"live:{project.id}".encode())]],
        )

    async def _edit_worker_status(self, event, user_id: int) -> None:
        profile = self.database.worker_profile_summary(user_id)
        if not profile or not profile["connected"]:
            await self._edit_reply(
                event,
                "<b>👤 Worker Account Status</b>\n\n🔴 No worker account is connected yet.",
                buttons=[[Button.inline("🔐 Connect Worker Account", b"account:connect")], [Button.inline("🏠 Main Menu", b"project:list")]],
            )
            return
        account_line = "🟡 Session saved — checking account"
        try:
            async with self.gateway.client_for_profile(int(profile["id"])) as client:
                me = await client.get_me()
                name = " ".join(part for part in [me.first_name, me.last_name] if part).strip() or "Telegram user"
                username = f"@{me.username}" if me.username else "No username"
                account_line = f"🟢 Connected\n👤 Account: <b>{self._esc(name)}</b> ({self._esc(username)})\n🆔 Account ID: <code>{me.id}</code>"
        except Exception as exc:
            account_line = f"🟠 Saved session could not be checked: <code>{self._esc(truncate(str(exc), 110))}</code>"
        summary = self.database.project_status_summary(user_id)
        restrictions = self.database.observed_worker_restrictions(user_id)
        waiting = summary.get(ProjectStatus.WAITING_RATE_LIMIT.value, 0)
        restriction_text = "🟢 No observed Telegram spam/restriction error."
        if restrictions:
            restriction_text = f"⚠️ Observed restriction/rate error: <code>{self._esc(truncate(restrictions[0], 120))}</code>"
        text = (
            "<b>👤 Worker Account Status</b>\n\n"
            f"{account_line}\n\n"
            f"📱 Phone hint: <code>{self._esc(str(profile['phone_hint'] or 'Unknown'))}</code>\n"
            f"🗓️ Session added: <code>{self._esc(str(profile['created_at']))}</code>\n"
            f"🔄 Last session update: <code>{self._esc(str(profile['updated_at']))}</code>\n"
            f"⏳ Projects currently in FloodWait: {waiting}\n"
            f"{restriction_text}\n\n"
            "ℹ️ Telegram does not provide a reliable advance spam-block status API; this panel reports live session health and observed delivery/rate-limit errors."
        )
        await self._edit_reply(
            event,
            text,
            buttons=[[Button.inline("🔄 Refresh", b"worker:status")], [Button.inline("🛠️ Admin Panel", b"admin"), Button.inline("📂 Projects", b"project:list")]],
        )

    async def _edit_admin_panel(self, event, user_id: int) -> None:
        summary = self.database.project_status_summary(user_id)
        profile = self.database.worker_profile_summary(user_id)
        total = sum(summary.values())
        running = summary.get(ProjectStatus.RUNNING.value, 0) + summary.get(ProjectStatus.WAITING_RATE_LIMIT.value, 0)
        paused = summary.get(ProjectStatus.PAUSED.value, 0)
        completed = summary.get(ProjectStatus.COMPLETED.value, 0)
        failed = summary.get(ProjectStatus.FAILED.value, 0)
        worker_state = "🟢 Session connected" if profile and profile["connected"] else "🔴 No worker session"
        global_stats = self.database.global_admin_summary()
        text = (
            "<b>🛠️ Admin Control Center</b>\n\n"
            f"🌐 Public users: {global_stats['users']}\n"
            f"🔐 Connected worker sessions: {global_stats['worker_sessions']}\n"
            f"📁 All projects: {global_stats['projects']}\n"
            f"🟢 All running/rate-limited: {global_stats['running']}\n\n"
            "<b>📌 Your Projects</b>\n"
            f"📁 Total: {total}\n"
            f"🟢 Running / rate-limited: {running}\n"
            f"⏸️ Paused: {paused}\n"
            f"✅ Completed: {completed}\n"
            f"⚠️ Failed: {failed}\n"
            f"👷 Active worker tasks: {sum(1 for task in self.workers.tasks.values() if not task.done())}\n"
            f"👤 Your worker: {worker_state}\n\n"
            "Use the buttons below to inspect projects and worker health."
        )
        await self._edit_reply(
            event,
            text,
            buttons=[
                [Button.inline("🔄 Refresh Dashboard", b"admin:refresh")],
                [Button.inline("📡 Active Projects", b"admin:active"), Button.inline("👥 Worker Sessions", b"admin:workers")],
                [Button.inline("📂 My Projects", b"project:list"), Button.inline("👤 My Worker", b"worker:status")],
                [Button.inline("➕ New Project", b"project:new")],
            ],
        )

    async def _edit_admin_workers(self, event) -> None:
        rows = self.database.admin_worker_profiles()
        lines = ["<b>👥 Public Worker Sessions</b>", ""]
        if not rows:
            lines.append("No worker sessions connected yet.")
        else:
            for row in rows:
                lines.append(
                    f"🟢 <code>{row['owner_id']}</code> · <code>{self._esc(str(row['phone_hint'] or 'Unknown'))}</code>\n"
                    f"   Updated: <code>{self._esc(str(row['updated_at']))}</code>"
                )
        await self._edit_reply(event, "\n".join(lines), buttons=[[Button.inline("⬅️ Admin Panel", b"admin")]])

    async def _edit_admin_active_projects(self, event) -> None:
        rows = self.database.admin_active_projects()
        lines = ["<b>📡 Active Public Projects</b>", ""]
        if not rows:
            lines.append("No active projects right now.")
        else:
            for row in rows:
                lines.append(
                    f"🔄 <b>{self._esc(str(row['name']))}</b> · <code>{self._esc(str(row['status']))}</code>\n"
                    f"   Owner: <code>{row['owner_id']}</code>\n"
                    f"   {self._esc(str(row['source_name'] or 'Source'))} → {self._esc(str(row['destination_name'] or 'Destination'))}"
                )
        await self._edit_reply(event, "\n".join(lines), buttons=[[Button.inline("⬅️ Admin Panel", b"admin")]])

    @staticmethod
    def _projects_buttons(projects: list[Project]):
        rows = [[Button.inline(f"📁 {project.name[:25]} · {project.status.value}", f"view:{project.id}".encode())] for project in projects]
        rows.append([Button.inline("🚀 New Backup Project", b"project:new")])
        return rows

    @staticmethod
    def _confirm_buttons(project_id: str):
        return [[Button.inline("✅ Confirm Project", f"confirm:{project_id}".encode())], [Button.inline("❌ Cancel", f"cancel:{project_id}".encode())]]

    async def _edit_project(self, event, project: Project, show_counters: bool = False) -> None:
        text = self._project_card(project)
        if show_counters:
            counters = self.database.counters(project.id)
            text += f"\n\nCompleted: {counters.completed:,}\nFailed: {counters.failed:,}\nTransferred: {readable_bytes(counters.bytes_transferred)}"
        await self._edit_reply(event, text, parse_mode="html", buttons=self._project_buttons(project.id))

    @staticmethod
    def _project_buttons(project_id: str):
        return [
            [Button.inline("▶️ Start / Resume", f"start:{project_id}".encode()), Button.inline("⏸️ Pause", f"pause:{project_id}".encode())],
            [Button.inline("⏹️ Stop", f"stopask:{project_id}".encode()), Button.inline("📡 Live Status", f"status:{project_id}".encode())],
            [Button.inline("🧪 Verify Access", f"verify:{project_id}".encode()), Button.inline("🔍 Preview Scan", f"preview:{project_id}".encode())],
            [Button.inline("📜 Activity", f"activity:{project_id}".encode())],
            [Button.inline("⚙️ Settings", f"settings:{project_id}".encode()), Button.inline("📄 Full Report", f"report:{project_id}".encode())],
            [Button.inline("🔁 Retry Failed", f"retry:{project_id}".encode()), Button.inline("➕ Duplicate Setup", f"duplicate:{project_id}".encode())],
            [Button.inline("⚠️ Failed Files", f"failed:{project_id}".encode()), Button.inline("🗑️ Delete", f"deleteask:{project_id}".encode())],
            [Button.inline("📂 My Projects", b"project:list"), Button.inline("🛠️ Admin", b"admin")],
        ]

    @staticmethod
    def _media_filter_types() -> list[tuple[MediaType, str]]:
        return [
            (MediaType.DOCUMENT, "📄 Files"),
            (MediaType.PHOTO, "🖼️ Photos"),
            (MediaType.VIDEO, "🎬 Videos"),
            (MediaType.AUDIO, "🎵 Audio"),
            (MediaType.VOICE, "🎙️ Voice"),
            (MediaType.VIDEO_NOTE, "⭕ Video notes"),
            (MediaType.GIF, "🌀 GIFs"),
            (MediaType.STICKER, "🎭 Stickers"),
        ]

    async def _edit_media_filters(self, event, project: Project) -> None:
        enabled = {str(item) for item in project.settings.media_types}
        rows = []
        for media_type, label in self._media_filter_types():
            mark = "✅" if media_type.value in enabled else "⬜"
            rows.append([Button.inline(f"{mark} {label}", f"filter:{media_type.value}:{project.id}".encode())])
        rows.append([Button.inline("⬅️ Settings", f"settings:{project.id}".encode())])
        await self._edit_reply(
            event,
            f"<b>🎛️ Media Filters — {self._esc(project.name)}</b>\n\n"
            "Choose exactly which media/file types the plan scan and transfer should include. Changing filters clears the current sending plan.",
            buttons=rows,
        )

    async def _toggle_media_filter(self, event: events.CallbackQuery.Event, user_id: int, data: str) -> None:
        _, media_value, project_id = data.split(":", 2)
        project = self.database.get_project(project_id, user_id)
        if not project:
            await event.answer("Project not found", alert=True)
            return
        values = [str(item) for item in project.settings.media_types]
        if media_value in values:
            if len(values) == 1:
                await event.answer("Keep at least one media type enabled", alert=True)
                return
            values.remove(media_value)
        else:
            values.append(media_value)
        self.database.update_project_settings(project.id, replace(project.settings, media_types=values))
        await self._edit_media_filters(event, self.database.get_project(project.id, user_id))

    async def _edit_settings(self, event, project: Project) -> None:
        settings = project.settings
        text = (
            f"<b>⚙️ Settings — {self._esc(project.name)}</b>\n\n"
            "⚡ Transfer: Telegram server-side fresh send\n"
            f"📦 Content mode: {settings.content_mode}\n"
            f"🧵 Forum topic clone: {'On' if settings.clone_forum_topics else 'Off'}\n"
            f"📺 Forum → channel sections: {'On' if settings.forum_to_channel_segments else 'Off'}\n"
            f"📝 Captions: {'Preserved' if settings.preserve_captions else 'Removed'}\n"
            f"🔄 Continuous sync: {'Enabled' if settings.continuous_sync else 'Disabled'}\n"
            f"⏲️ Idle stop timer: {settings.idle_stop_seconds}s\n\n"
            "When sync finds no media, it waits once for the timer, scans one final time, then stops if still idle."
        )
        await self._edit_reply(
            event,
            text,
            parse_mode="html",
            buttons=[
                [Button.inline("🎛️ Media Filters", f"mediafilter:{project.id}".encode())],
                [Button.inline(f"📝 Captions: {'On' if settings.preserve_captions else 'Off'}", f"caption:{project.id}".encode())],
                [Button.inline(f"🔄 Sync: {'On' if settings.continuous_sync else 'Off'}", f"sync:{project.id}".encode())],
                [Button.inline("⬅️ Back", f"view:{project.id}".encode())],
            ],
        )

    @staticmethod
    def _project_card(project: Project) -> str:
        if project.settings.forum_to_channel_segments:
            forum_mode = "Sequential channel sections"
        elif project.settings.clone_forum_topics:
            forum_mode = "Forum topic clone"
        else:
            forum_mode = "Normal chat"
        status_label = "🛡️ Telegram pace protection" if project.status == ProjectStatus.WAITING_RATE_LIMIT else project.status.value
        base = (
            f"<b>📁 {TelegramControlBot._esc(project.name)}</b>\n"
            f"🔄 Status: <code>{status_label}</code>\n"
            f"📥 Source: {TelegramControlBot._esc(project.source_name or project.source_ref)}\n"
            f"📤 Destination: {TelegramControlBot._esc(project.destination_name or project.destination_ref)}\n"
            f"🧭 Start: {project.scan_mode.value}\n"
            f"📦 Content: {project.settings.content_mode}\n"
            "⚡ Transfer: Telegram server-side fresh send\n"
            f"🧵 Forum mode: {forum_mode}\n"
            f"📝 Captions: {'On' if project.settings.preserve_captions else 'Off'} · "
            f"🔄 Sync: {'On' if project.settings.continuous_sync else 'Off'}"
        )
        scheduler = "\n⏳ Scheduler: waiting for a fair worker slot" if project.status == ProjectStatus.QUEUED else ""
        error = f"\n\n❌ Last error: <code>{TelegramControlBot._esc(truncate(project.last_error, 260))}</code>" if project.last_error else ""
        return base + scheduler + error

    @staticmethod
    def _help_text() -> str:
        return (
            "<b>ℹ️ Commands</b>\n\n"
            "/start — 🏠 Main menu\n"
            "/connect — 🔐 Connect worker account\n"
            "/new — 🚀 New backup project\n"
            "/projects — 📂 Project list\n"
            "/help — ℹ️ Help\n\n"
            "⚡ Media is sent as a fresh Telegram message without forwarding and without local file download."
        )

    @staticmethod
    def _esc(value: str) -> str:
        return html.escape(value, quote=False)
