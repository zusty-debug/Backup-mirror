from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .config import Settings
from .database import Database
from .keyboards import (
    confirmation_keyboard,
    delete_confirmation_keyboard,
    main_menu,
    project_keyboard,
    projects_keyboard,
    scan_mode_keyboard,
    settings_keyboard,
    stop_confirmation_keyboard,
)
from .models import Project, ProjectSettings, ProjectStatus, ScanMode
from .reports import build_project_report
from .telegram_gateway import ProfileNotConnectedError, TelegramGateway, TelegramGatewayError
from .utils import readable_bytes, truncate
from .worker import WorkerManager


class OwnerOnly(BaseFilter):
    def __init__(self, owner_ids: tuple[int, ...]) -> None:
        self.owner_ids = set(owner_ids)

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and event.from_user.id in self.owner_ids)


class ConnectFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()


class ProjectFlow(StatesGroup):
    source = State()
    destination = State()
    name = State()
    mode = State()
    message_id = State()
    date_start = State()
    date_end = State()
    confirm = State()


class BotController:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        gateway: TelegramGateway,
        workers: WorkerManager,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.workers = workers
        self.bot = bot
        self.router = Router(name="mirror-control")
        self.router.message.filter(OwnerOnly(settings.owner_ids))
        self.router.callback_query.filter(OwnerOnly(settings.owner_ids))
        self._register()

    def _register(self) -> None:
        r = self.router
        r.message.register(self.start, Command("start"))
        r.message.register(self.show_help, Command("help"))
        r.message.register(self.begin_connect_command, Command("connect"))
        r.message.register(self.list_projects_command, Command("projects"))
        r.message.register(self.begin_new_project, Command("new"))
        r.message.register(self.login_phone, ConnectFlow.phone)
        r.message.register(self.login_code, ConnectFlow.code)
        r.message.register(self.login_password, ConnectFlow.password)
        r.message.register(self.project_source, ProjectFlow.source)
        r.message.register(self.project_destination, ProjectFlow.destination)
        r.message.register(self.project_name, ProjectFlow.name)
        r.message.register(self.project_message_id, ProjectFlow.message_id)
        r.message.register(self.project_start_date, ProjectFlow.date_start)
        r.message.register(self.project_end_date, ProjectFlow.date_end)
        r.callback_query.register(self.main_account_connect, F.data == "account:connect")
        r.callback_query.register(self.begin_new_project_callback, F.data == "project:new")
        r.callback_query.register(self.list_projects_callback, F.data == "project:list")
        r.callback_query.register(self.select_scan_mode, F.data.startswith("newmode:"))
        r.callback_query.register(self.confirm_new_project, F.data.startswith("newconfirm:"))
        r.callback_query.register(self.cancel_new_project, F.data.startswith("newcancel:"))
        r.callback_query.register(self.view_project, F.data.startswith("project:view:"))
        r.callback_query.register(self.status_project, F.data.startswith("project:status:"))
        r.callback_query.register(self.start_project, F.data.startswith("run:start:"))
        r.callback_query.register(self.pause_project, F.data.startswith("run:pause:"))
        r.callback_query.register(self.stop_project_prompt, F.data.startswith("run:stop:"))
        r.callback_query.register(self.stop_project_confirm, F.data.startswith("run:stopconfirm:"))
        r.callback_query.register(self.view_settings, F.data.startswith("settings:view:"))
        r.callback_query.register(self.update_setting, F.data.startswith("set:"))
        r.callback_query.register(self.full_report, F.data.startswith("report:"))
        r.callback_query.register(self.failed_files, F.data.startswith("failed:"))
        r.callback_query.register(self.delete_prompt, F.data.startswith("delete:ask:"))
        r.callback_query.register(self.delete_confirm, F.data.startswith("delete:confirm:"))
        r.callback_query.register(self.show_help_callback, F.data == "help")

    async def start(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        self.database.ensure_user(message.from_user.id)
        connected = self.gateway.has_session(message.from_user.id)
        await message.answer(
            "<b>Telegram Media Backup Bot</b>\n\n"
            "Create a project, validate its source/destination, and copy selected media as newly uploaded files.\n"
            f"Worker account: <b>{'connected' if connected else 'not connected'}</b>",
            reply_markup=main_menu(connected),
        )

    async def show_help(self, message: Message) -> None:
        await message.answer(self._help_text())

    async def show_help_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(self._help_text())

    async def begin_connect_command(self, message: Message, state: FSMContext) -> None:
        await self._begin_connect(message, state)

    async def main_account_connect(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await self._begin_connect(callback.message, state)

    async def _begin_connect(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(ConnectFlow.phone)
        await message.answer("Send the worker account phone number in international format, e.g. <code>+15551234567</code>.")

    async def login_phone(self, message: Message, state: FSMContext) -> None:
        try:
            phone_hint = await self.gateway.begin_login(message.from_user.id, message.text or "")
        except TelegramGatewayError as exc:
            await message.answer(f"Could not request a login code: <code>{self._escape(str(exc))}</code>")
            return
        await state.set_state(ConnectFlow.code)
        await message.answer(f"Telegram sent a login code to the worker account ending in <code>{phone_hint}</code>. Send that code here.")

    async def login_code(self, message: Message, state: FSMContext) -> None:
        try:
            connected = await self.gateway.finish_login_code(message.from_user.id, message.text or "")
            await self._delete_secret_message(message)
        except TelegramGatewayError as exc:
            await message.answer(f"Login was not completed: <code>{self._escape(str(exc))}</code>")
            return
        if connected:
            await state.clear()
            await message.answer("Worker account connected. You can now create a backup project.", reply_markup=main_menu(True))
        else:
            await state.set_state(ConnectFlow.password)
            await message.answer("This account requires two-step verification. Send the password to complete connection.")

    async def login_password(self, message: Message, state: FSMContext) -> None:
        try:
            await self.gateway.finish_login_password(message.from_user.id, message.text or "")
            await self._delete_secret_message(message)
        except TelegramGatewayError as exc:
            await message.answer(f"Login was not completed: <code>{self._escape(str(exc))}</code>")
            return
        await state.clear()
        await message.answer("Worker account connected. You can now create a backup project.", reply_markup=main_menu(True))

    async def list_projects_command(self, message: Message) -> None:
        await self._send_project_list(message, message.from_user.id)

    async def list_projects_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._edit_project_list(callback, callback.from_user.id)

    async def _send_project_list(self, message: Message, owner_id: int) -> None:
        projects = self.database.list_projects(owner_id)
        if not projects:
            await message.answer("No backup projects yet.", reply_markup=projects_keyboard([]))
            return
        await message.answer("<b>My Projects</b>", reply_markup=projects_keyboard(projects))

    async def _edit_project_list(self, callback: CallbackQuery, owner_id: int) -> None:
        projects = self.database.list_projects(owner_id)
        text = "<b>My Projects</b>" if projects else "No backup projects yet."
        await self._edit(callback, text, reply_markup=projects_keyboard(projects))

    async def begin_new_project(self, message: Message, state: FSMContext) -> None:
        await self._begin_new_project(message, state, message.from_user.id)

    async def begin_new_project_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await self._begin_new_project(callback.message, state, callback.from_user.id)

    async def _begin_new_project(self, message: Message, state: FSMContext, owner_id: int) -> None:
        if not self.gateway.has_session(owner_id):
            await message.answer("Connect the worker account first using <code>/connect</code>.")
            return
        if self.database.count_projects(owner_id) >= self.settings.max_projects_per_owner:
            await message.answer("The configured project limit has been reached.")
            return
        await state.clear()
        await state.set_state(ProjectFlow.source)
        await message.answer("Send the source channel/group username, ID, or Telegram link.")

    async def project_source(self, message: Message, state: FSMContext) -> None:
        reference = (message.text or "").strip()
        if not reference:
            await message.answer("Send a source chat username, numeric ID, or Telegram link.")
            return
        await state.update_data(source_ref=reference)
        await state.set_state(ProjectFlow.destination)
        await message.answer("Send the destination channel/group username, ID, or Telegram link.")

    async def project_destination(self, message: Message, state: FSMContext) -> None:
        reference = (message.text or "").strip()
        if not reference:
            await message.answer("Send a destination chat username, numeric ID, or Telegram link.")
            return
        await state.update_data(destination_ref=reference)
        await state.set_state(ProjectFlow.name)
        await message.answer("Give this backup project a name.")

    async def project_name(self, message: Message, state: FSMContext) -> None:
        name = " ".join((message.text or "").split())
        if not 1 <= len(name) <= 100:
            await message.answer("Project name must be between 1 and 100 characters.")
            return
        await state.update_data(name=name)
        await state.set_state(ProjectFlow.mode)
        await message.answer("Choose the scan mode.", reply_markup=scan_mode_keyboard())

    async def select_scan_mode(self, callback: CallbackQuery, state: FSMContext) -> None:
        mode = callback.data.split(":", 1)[1]
        await callback.answer()
        if mode == "full":
            await state.update_data(scan_mode=ScanMode.FULL.value, settings=ProjectSettings())
            await self._create_draft_and_preflight(callback.message, callback.from_user.id, state)
        elif mode == "new":
            settings = ProjectSettings(continuous_sync=True)
            await state.update_data(scan_mode=ScanMode.NEW_FILES_ONLY.value, settings=settings)
            await self._create_draft_and_preflight(callback.message, callback.from_user.id, state)
        elif mode == "from":
            await state.update_data(scan_mode=ScanMode.FROM_MESSAGE_ID.value, settings=ProjectSettings())
            await state.set_state(ProjectFlow.message_id)
            await callback.message.answer("Send the first source message ID to include.")
        elif mode == "date":
            await state.update_data(scan_mode=ScanMode.DATE_RANGE.value, settings=ProjectSettings())
            await state.set_state(ProjectFlow.date_start)
            await callback.message.answer("Send the start date in YYYY-MM-DD format.")
        else:
            await callback.message.answer("Unknown scan mode.")

    async def project_message_id(self, message: Message, state: FSMContext) -> None:
        try:
            message_id = int((message.text or "").strip())
            if message_id <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Send a positive numeric source message ID.")
            return
        await state.update_data(start_message_id=message_id)
        await self._create_draft_and_preflight(message, message.from_user.id, state)

    async def project_start_date(self, message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        try:
            date.fromisoformat(value)
        except ValueError:
            await message.answer("Use YYYY-MM-DD, for example 2026-01-01.")
            return
        await state.update_data(start_date=value)
        await state.set_state(ProjectFlow.date_end)
        await message.answer("Send the end date in YYYY-MM-DD format.")

    async def project_end_date(self, message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        data = await state.get_data()
        try:
            if date.fromisoformat(value) < date.fromisoformat(data["start_date"]):
                raise ValueError
        except ValueError:
            await message.answer("Use YYYY-MM-DD and make sure it is not before the start date.")
            return
        await state.update_data(end_date=value)
        await self._create_draft_and_preflight(message, message.from_user.id, state)

    async def _create_draft_and_preflight(self, message: Message, owner_id: int, state: FSMContext) -> None:
        data = await state.get_data()
        settings = data.get("settings") or ProjectSettings()
        if isinstance(settings, dict):
            settings = ProjectSettings(**settings)
        project = Project.draft(
            owner_id=owner_id,
            profile_id=self.gateway.default_profile_id(owner_id),
            name=data["name"],
            source_ref=data["source_ref"],
            destination_ref=data["destination_ref"],
            scan_mode=ScanMode(data["scan_mode"]),
            settings=settings,
        )
        project.start_message_id = data.get("start_message_id")
        project.start_date = data.get("start_date")
        project.end_date = data.get("end_date")
        self.database.create_project(project)
        try:
            source, destination = await self.gateway.preflight(project.profile_id, project.source_ref, project.destination_ref)
            self.database.update_project_resolution(
                project.id,
                int(source.id),
                self.gateway.entity_name(source),
                int(destination.id),
                self.gateway.entity_name(destination),
            )
            if project.scan_mode == ScanMode.NEW_FILES_ONLY:
                # New-files-only begins at the source tail. Use Full Backup plus
                # the Continuous Sync setting when an initial historical copy is wanted.
                latest_id = await self.gateway.latest_message_id(project.profile_id, project.source_ref)
                if latest_id:
                    self.database.update_project_checkpoint(project.id, latest_id)
        except (TelegramGatewayError, ProfileNotConnectedError) as exc:
            self.database.delete_project(project.id, owner_id)
            await state.clear()
            await message.answer(f"Project validation failed: <code>{self._escape(str(exc))}</code>")
            return
        project = self.database.get_project(project.id, owner_id)
        await state.update_data(project_id=project.id)
        await state.set_state(ProjectFlow.confirm)
        await message.answer(self._project_confirmation(project), reply_markup=confirmation_keyboard(project.id))

    async def confirm_new_project(self, callback: CallbackQuery, state: FSMContext) -> None:
        project_id = callback.data.split(":", 1)[1]
        project = self.database.get_project(project_id, callback.from_user.id)
        if project is None:
            await callback.answer("Project no longer exists.", show_alert=True)
            return
        await callback.answer("Project created")
        await state.clear()
        await self._edit(callback, self._project_card(project), reply_markup=project_keyboard(project))

    async def cancel_new_project(self, callback: CallbackQuery, state: FSMContext) -> None:
        project_id = callback.data.split(":", 1)[1]
        self.database.delete_project(project_id, callback.from_user.id)
        await state.clear()
        await callback.answer("Project cancelled")
        await self._edit_project_list(callback, callback.from_user.id)

    async def view_project(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project:
            await callback.answer()
            await self._edit(callback, self._project_card(project), reply_markup=project_keyboard(project))

    async def status_project(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project:
            await callback.answer()
            counters = self.database.counters(project.id)
            text = self._project_card(project) + (
                f"\n\n<b>Recorded totals</b>\n"
                f"Completed: {counters.completed:,}\nSkipped: {counters.skipped:,}\n"
                f"Failed: {counters.failed:,}\nTransferred: {readable_bytes(counters.bytes_transferred)}"
            )
            await self._edit(callback, text, reply_markup=project_keyboard(project))

    async def start_project(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project is None:
            return
        if not self.gateway.has_session(callback.from_user.id):
            await callback.answer("Worker session is not connected.", show_alert=True)
            return
        status_message = await callback.message.answer(f"<b>Backup status</b>\nProject: <b>{self._escape(project.name)}</b>\nPreparing…")
        self.database.set_status_message(project.id, status_message.chat.id, status_message.message_id)
        try:
            started = await self.workers.start(project.id)
        except Exception as exc:
            await callback.answer("Unable to start project", show_alert=True)
            await status_message.edit_text(f"Unable to start: <code>{self._escape(str(exc))}</code>")
            return
        await callback.answer("Backup started" if started else "Backup is already running")
        project = self.database.get_project(project.id, callback.from_user.id)
        await self._edit(callback, self._project_card(project), reply_markup=project_keyboard(project))

    async def pause_project(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project is None:
            return
        if self.workers.is_running(project.id):
            self.workers.request_pause(project.id)
            notice = "Pause requested; current transfer will finish safely."
        else:
            self.database.update_project_status(project.id, ProjectStatus.PAUSED)
            notice = "Project paused."
        await callback.answer(notice)
        updated = self.database.get_project(project.id)
        await self._edit(callback, self._project_card(updated), reply_markup=project_keyboard(updated))

    async def stop_project_prompt(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project:
            await callback.answer()
            await self._edit(
                callback,
                f"<b>Stop backup?</b>\n\n{self._escape(project.name)}\n\nProgress is retained and can be resumed later.",
                reply_markup=stop_confirmation_keyboard(project.id),
            )

    async def stop_project_confirm(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project is None:
            return
        if self.workers.is_running(project.id):
            self.workers.request_stop(project.id)
            notice = "Stop requested; current transfer will finish safely."
        else:
            self.database.update_project_status(project.id, ProjectStatus.STOPPED)
            notice = "Project stopped. Progress is retained."
        await callback.answer(notice)
        project = self.database.get_project(project.id)
        await self._edit(callback, self._project_card(project), reply_markup=project_keyboard(project))

    async def view_settings(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project:
            await callback.answer()
            await self._edit(callback, self._settings_text(project), reply_markup=settings_keyboard(project.id, project.settings))

    async def update_setting(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("Invalid setting", show_alert=True)
            return
        kind = parts[1]
        if kind == "media":
            if len(parts) != 4:
                await callback.answer("Invalid media setting", show_alert=True)
                return
            media_value, project_id = parts[2], parts[3]
        else:
            project_id = parts[2]
            media_value = None
        project = await self._owned_project(callback, project_id)
        if project is None:
            return
        settings = project.settings
        if kind == "media" and media_value:
            values = [str(value) for value in settings.media_types]
            if media_value in values:
                if len(values) == 1:
                    await callback.answer("At least one media type must remain enabled.", show_alert=True)
                    return
                values.remove(media_value)
            else:
                values.append(media_value)
            settings = replace(settings, media_types=values)
        elif kind == "captions":
            settings = replace(settings, preserve_captions=not settings.preserve_captions)
        elif kind == "albums":
            settings = replace(settings, preserve_albums=not settings.preserve_albums)
        elif kind == "dupes":
            settings = replace(settings, skip_duplicates=not settings.skip_duplicates)
        elif kind == "sync":
            settings = replace(settings, continuous_sync=not settings.continuous_sync)
        elif kind == "checksum":
            settings = replace(settings, checksum_enabled=not settings.checksum_enabled)
        else:
            await callback.answer("Unknown setting", show_alert=True)
            return
        self.database.update_project_settings(project.id, settings)
        project = self.database.get_project(project.id)
        await callback.answer("Setting updated")
        await self._edit(callback, self._settings_text(project), reply_markup=settings_keyboard(project.id, project.settings))

    async def full_report(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 1)[1])
        if project is None:
            return
        path = build_project_report(self.database, project, self.settings.report_dir)
        await callback.answer("Report generated")
        await callback.message.answer_document(FSInputFile(path), caption=f"Full report — {project.name}")

    async def failed_files(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 1)[1])
        if project is None:
            return
        failed = self.database.failed_items(project.id, limit=50)
        await callback.answer()
        if not failed:
            await callback.message.answer(f"<b>{self._escape(project.name)}</b> has no failed-file records.")
            return
        lines = [f"<b>Failed files — {self._escape(project.name)}</b>"]
        for item in failed:
            lines.append(
                f"• #{item['source_message_id']} — <code>{self._escape(truncate(item['file_name'] or 'Unknown', 45))}</code>\n"
                f"  {self._escape(truncate(item['error'] or 'Unknown error', 110))}"
            )
        await callback.message.answer("\n".join(lines))

    async def delete_prompt(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project:
            await callback.answer()
            await self._edit(
                callback,
                f"<b>Delete project?</b>\n\n{self._escape(project.name)}\n\nThis removes project history and transfer records.",
                reply_markup=delete_confirmation_keyboard(project.id),
            )

    async def delete_confirm(self, callback: CallbackQuery) -> None:
        project = await self._owned_project(callback, callback.data.split(":", 2)[2])
        if project is None:
            return
        if self.workers.is_running(project.id):
            await callback.answer("Stop the active backup before deleting it.", show_alert=True)
            return
        self.database.delete_project(project.id, callback.from_user.id)
        await callback.answer("Project deleted")
        await self._edit_project_list(callback, callback.from_user.id)

    async def _owned_project(self, callback: CallbackQuery, project_id: str) -> Project | None:
        project = self.database.get_project(project_id, callback.from_user.id)
        if project is None:
            # Callback answers must be sent or Telegram shows a spinner indefinitely.
            # Project IDs are always checked against the callback sender.
            await callback.answer("Project not found", show_alert=True)
            return None
        return project

    @staticmethod
    async def _delete_secret_message(message: Message) -> None:
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    async def _edit(self, callback: CallbackQuery, text: str, **kwargs: Any) -> None:
        try:
            await callback.message.edit_text(text, **kwargs)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                await callback.message.answer(text, **kwargs)

    @staticmethod
    def _project_confirmation(project: Project) -> str:
        return (
            "<b>Backup Project</b>\n\n"
            f"Name: <b>{BotController._escape(project.name)}</b>\n"
            f"Source: {BotController._escape(project.source_name or project.source_ref)}\n"
            f"Destination: {BotController._escape(project.destination_name or project.destination_ref)}\n"
            f"Mode: {project.scan_mode.value}\n"
            "Media/files only\n"
            f"Captions: {'Preserve' if project.settings.preserve_captions else 'Remove'}\n"
            "Forwarding: Disabled\n"
            "Sender information: Not copied"
        )

    @staticmethod
    def _project_card(project: Project) -> str:
        return (
            f"<b>{BotController._escape(project.name)}</b>\n"
            f"Status: <code>{project.status.value}</code>\n"
            f"Source: {BotController._escape(project.source_name or project.source_ref)}\n"
            f"Destination: {BotController._escape(project.destination_name or project.destination_ref)}\n"
            f"Mode: {project.scan_mode.value}\n"
            f"Captions: {'On' if project.settings.preserve_captions else 'Off'} · "
            f"Sync: {'On' if project.settings.continuous_sync else 'Off'}"
            + (f"\nLast error: <code>{BotController._escape(truncate(project.last_error, 160))}</code>" if project.last_error else "")
        )

    @staticmethod
    def _settings_text(project: Project) -> str:
        return (
            f"<b>Settings — {BotController._escape(project.name)}</b>\n\n"
            "Toggle allowed media and transfer behavior. Changes apply to the next item boundary."
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "<b>Commands</b>\n"
            "/start — main menu\n"
            "/connect — connect the worker account\n"
            "/new — create a backup project\n"
            "/projects — list projects\n"
            "/help — show this help\n\n"
            "Each project downloads selected source media to a temporary location and uploads it as a new destination message. "
            "Text-only messages and forwarding are not used. Use a project card for Start, Pause, Stop, Resume, settings, failures, and reports."
        )

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
