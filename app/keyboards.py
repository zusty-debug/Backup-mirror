from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import MediaType, Project, ProjectSettings


def main_menu(connected: bool) -> InlineKeyboardMarkup:
    rows = []
    if not connected:
        rows.append([InlineKeyboardButton(text="Connect Worker Account", callback_data="account:connect")])
    rows.extend(
        [
            [InlineKeyboardButton(text="New Backup Project", callback_data="project:new")],
            [InlineKeyboardButton(text="My Projects", callback_data="project:list")],
            [InlineKeyboardButton(text="Help", callback_data="help")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scan_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Full Backup", callback_data="newmode:full")],
            [InlineKeyboardButton(text="New Files Only / Sync", callback_data="newmode:new")],
            [InlineKeyboardButton(text="From Message ID", callback_data="newmode:from")],
            [InlineKeyboardButton(text="Date Range", callback_data="newmode:date")],
        ]
    )


def confirmation_keyboard(project_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Confirm Project", callback_data=f"newconfirm:{project_id}")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"newcancel:{project_id}")],
        ]
    )


def projects_keyboard(projects: list[Project]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{project.name[:32]} · {project.status.value}", callback_data=f"project:view:{project.id}"
            )
        ]
        for project in projects
    ]
    rows.append([InlineKeyboardButton(text="New Backup Project", callback_data="project:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def project_keyboard(project: Project) -> InlineKeyboardMarkup:
    project_id = project.id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Start / Resume", callback_data=f"run:start:{project_id}"),
                InlineKeyboardButton(text="Pause", callback_data=f"run:pause:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="Stop", callback_data=f"run:stop:{project_id}"),
                InlineKeyboardButton(text="Status", callback_data=f"project:status:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="Settings", callback_data=f"settings:view:{project_id}"),
                InlineKeyboardButton(text="Full Report", callback_data=f"report:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="Failed Files", callback_data=f"failed:{project_id}"),
                InlineKeyboardButton(text="Delete", callback_data=f"delete:ask:{project_id}"),
            ],
            [InlineKeyboardButton(text="‹ My Projects", callback_data="project:list")],
        ]
    )


def settings_keyboard(project_id: str, settings: ProjectSettings) -> InlineKeyboardMarkup:
    rows = []
    for media_type in MediaType:
        enabled = settings.allows(media_type)
        mark = "✓" if enabled else "✗"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {media_type.value.title().replace('_', ' ')}",
                    callback_data=f"set:media:{media_type.value}:{project_id}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"Captions: {'On' if settings.preserve_captions else 'Off'}", callback_data=f"set:captions:{project_id}"
                ),
                InlineKeyboardButton(
                    text=f"Albums: {'On' if settings.preserve_albums else 'Off'}", callback_data=f"set:albums:{project_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"Skip duplicates: {'On' if settings.skip_duplicates else 'Off'}", callback_data=f"set:dupes:{project_id}"
                ),
                InlineKeyboardButton(
                    text=f"Continuous sync: {'On' if settings.continuous_sync else 'Off'}", callback_data=f"set:sync:{project_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"Checksums: {'On' if settings.checksum_enabled else 'Off'}", callback_data=f"set:checksum:{project_id}"
                )
            ],
            [InlineKeyboardButton(text="‹ Project", callback_data=f"project:view:{project_id}")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stop_confirmation_keyboard(project_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yes, Stop", callback_data=f"run:stopconfirm:{project_id}")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"project:view:{project_id}")],
        ]
    )


def delete_confirmation_keyboard(project_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yes, Delete", callback_data=f"delete:confirm:{project_id}")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"project:view:{project_id}")],
        ]
    )
