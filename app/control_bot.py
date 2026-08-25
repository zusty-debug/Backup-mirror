from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field, replace

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from .config import Settings
from .database import Database
from .models import Project, ProjectSettings, ProjectStatus, ScanMode
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
        await self.client.edit_message(chat_id, message_id, self._brand(text), parse_mode="html", buttons=None)

    @staticmethod
    def _brand(text: str) -> str:
        footer = "<i>Developed by — @xzusty</i>"
        return text if footer in text else f"{text}\n\n{footer}"

    async def _reply(self, event, text: str, *args, **kwargs):
        kwargs.setdefault("parse_mode", "html")
        return await event.respond(self._brand(text), *args, **kwargs)

    async def _edit_reply(self, event, text: str, *args, **kwargs):
        kwargs.setdefault("parse_mode", "html")
        return await event.edit(self._brand(text), *args, **kwargs)

    def _allowed(self, user_id: int | None) -> bool:
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
        if data in {"admin", "admin:refresh"}:
            await event.answer()
            await self._edit_admin_panel(event, user_id)
            return
        if data == "help":
            await event.answer()
            await self._reply(event, self._help_text(), parse_mode="html")
            return
        if data.startswith("mode:"):
            await event.answer()
            await self._choose_mode(event, user_id, data.split(":", 1)[1])
            return
        action, _, project_id = data.partition(":")
        project = self.database.get_project(project_id, user_id) if project_id else None
        project_actions = {
            "confirm", "cancel", "view", "start", "pause", "stopask", "stop", "status",
            "settings", "caption", "sync", "report", "failed", "deleteask", "delete",
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
            status = await self.client.send_message(
                event.chat_id,
                self._brand(f"<b>📡 Live backup status</b>\n📁 Project: <b>{self._esc(project.name)}</b>\n🚀 Preparing…"),
                parse_mode="html",
            )
            self.database.set_status_message(project.id, int(event.chat_id), int(status.id))
            started = await self.workers.start(project.id)
            await event.answer("Backup started" if started else "Already running")
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
            flow.stage = "destination"
            await self._reply(event, "Send the destination channel/group username, numeric ID, or Telegram link.")
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
            flow.stage = "mode"
            await self._reply(event, 
                "Choose scan mode.",
                buttons=[
                    [Button.inline("Full Backup", b"mode:full")],
                    [Button.inline("New Files Only / Sync", b"mode:new")],
                ],
            )
        elif flow.stage == "message_id":
            try:
                flow.data["start_message_id"] = int(text)
                if int(text) <= 0:
                    raise ValueError
            except ValueError:
                await self._reply(event, "Send a positive message ID.")
                return
            await self._create_project(event, user_id, flow)

    async def _choose_mode(self, event: events.CallbackQuery.Event, user_id: int, choice: str) -> None:
        flow = self.flows.get(user_id)
        if not flow or flow.stage != "mode":
            await event.answer("Start a new project again", alert=True)
            return
        if choice == "full":
            flow.data["scan_mode"] = ScanMode.FULL
            flow.data["settings"] = ProjectSettings()
            await self._create_project(event, user_id, flow)
        elif choice == "new":
            flow.data["scan_mode"] = ScanMode.NEW_FILES_ONLY
            flow.data["settings"] = ProjectSettings(continuous_sync=True)
            await self._create_project(event, user_id, flow)

    async def _create_project(self, event, user_id: int, flow: Flow) -> None:
        project = Project.draft(
            owner_id=user_id,
            profile_id=self.gateway.default_profile_id(user_id),
            name=str(flow.data["name"]),
            source_ref=str(flow.data["source_ref"]),
            destination_ref=str(flow.data["destination_ref"]),
            scan_mode=flow.data.get("scan_mode", ScanMode.FULL),
            settings=flow.data.get("settings", ProjectSettings()),
        )
        project.start_message_id = flow.data.get("start_message_id")
        self.database.create_project(project)
        try:
            source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
            self.database.update_project_resolution(project.id, int(source.id), self.gateway.entity_name(source), int(destination.id), self.gateway.entity_name(destination))
            if project.scan_mode == ScanMode.NEW_FILES_ONLY:
                latest = await self.gateway.latest_message_id(project.profile_id, project.source_ref)
                if latest:
                    self.database.update_project_checkpoint(project.id, latest)
        except (TelegramGatewayError, ProfileNotConnectedError) as exc:
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
                [Button.inline("🛠️ Admin Panel", b"admin"), Button.inline("ℹ️ Help", b"help")],
            ]
        )
        await self._reply(event, text, parse_mode="html", buttons=buttons)

    async def _send_projects(self, event, user_id: int) -> None:
        projects = self.database.list_projects(user_id)
        await self._reply(event, "<b>My Projects</b>" if projects else "No projects yet.", parse_mode="html", buttons=self._projects_buttons(projects))

    async def _edit_projects(self, event, user_id: int) -> None:
        projects = self.database.list_projects(user_id)
        await self._edit_reply(event, "<b>My Projects</b>" if projects else "No projects yet.", parse_mode="html", buttons=self._projects_buttons(projects))

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
        text = (
            "<b>🛠️ Admin Control Center</b>\n\n"
            f"📁 Total projects: {total}\n"
            f"🟢 Running / rate-limited: {running}\n"
            f"⏸️ Paused: {paused}\n"
            f"✅ Completed: {completed}\n"
            f"⚠️ Failed: {failed}\n"
            f"👷 Active worker tasks: {sum(1 for task in self.workers.tasks.values() if not task.done())}\n"
            f"👤 Worker account: {worker_state}\n\n"
            "Use the buttons below to inspect projects and worker health."
        )
        await self._edit_reply(
            event,
            text,
            buttons=[
                [Button.inline("🔄 Refresh Dashboard", b"admin:refresh")],
                [Button.inline("📂 My Projects", b"project:list"), Button.inline("👤 Worker Status", b"worker:status")],
                [Button.inline("➕ New Project", b"project:new")],
            ],
        )

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
            [Button.inline("⚙️ Settings", f"settings:{project_id}".encode()), Button.inline("📄 Full Report", f"report:{project_id}".encode())],
            [Button.inline("⚠️ Failed Files", f"failed:{project_id}".encode()), Button.inline("🗑️ Delete", f"deleteask:{project_id}".encode())],
            [Button.inline("📂 My Projects", b"project:list"), Button.inline("🛠️ Admin", b"admin")],
        ]

    async def _edit_settings(self, event, project: Project) -> None:
        settings = project.settings
        text = (
            f"<b>⚙️ Settings — {self._esc(project.name)}</b>\n\n"
            "⚡ Transfer: Telegram server-side fresh send\n"
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
                [Button.inline(f"📝 Captions: {'On' if settings.preserve_captions else 'Off'}", f"caption:{project.id}".encode())],
                [Button.inline(f"🔄 Sync: {'On' if settings.continuous_sync else 'Off'}", f"sync:{project.id}".encode())],
                [Button.inline("⬅️ Back", f"view:{project.id}".encode())],
            ],
        )

    @staticmethod
    def _project_card(project: Project) -> str:
        return (
            f"<b>📁 {TelegramControlBot._esc(project.name)}</b>\n"
            f"🔄 Status: <code>{project.status.value}</code>\n"
            f"📥 Source: {TelegramControlBot._esc(project.source_name or project.source_ref)}\n"
            f"📤 Destination: {TelegramControlBot._esc(project.destination_name or project.destination_ref)}\n"
            f"🧭 Mode: {project.scan_mode.value}\n"
            "⚡ Transfer: Telegram server-side fresh send\n"
            f"📝 Captions: {'On' if project.settings.preserve_captions else 'Off'} · "
            f"🔄 Sync: {'On' if project.settings.continuous_sync else 'Off'}"
        )

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
