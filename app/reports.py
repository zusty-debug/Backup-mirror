from __future__ import annotations

from pathlib import Path

from .database import Database
from .models import Project
from .utils import readable_bytes


def build_project_report(database: Database, project: Project, report_dir: Path) -> Path:
    counters = database.counters(project.id)
    failed = database.failed_items(project.id, limit=1000)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"backup-report-{project.id}.md"
    lines = [
        "# Telegram Media Backup Report",
        "",
        f"- **Project:** {project.name}",
        f"- **Project ID:** `{project.id}`",
        f"- **Status:** `{project.status.value}`",
        f"- **Source:** {project.source_name or project.source_ref} (`{project.source_chat_id or 'unresolved'}`)",
        f"- **Destination:** {project.destination_name or project.destination_ref} (`{project.destination_chat_id or 'unresolved'}`)",
        f"- **Scan mode:** {project.scan_mode.value}",
        f"- **Created:** {project.created_at}",
        f"- **Last updated:** {project.updated_at}",
        "",
        "## Settings",
        "",
        f"- Media types: {', '.join(str(item) for item in project.settings.media_types)}",
        f"- Preserve captions: {project.settings.preserve_captions}",
        f"- Preserve albums: {project.settings.preserve_albums}",
        f"- Skip duplicates: {project.settings.skip_duplicates}",
        f"- Continuous sync: {project.settings.continuous_sync}",
        f"- Checksum enabled: {project.settings.checksum_enabled}",
        f"- Transfer mode: {'Telegram server-side fresh send' if project.settings.server_side_copy else 'Download and re-upload'}",
        "",
        "## Results",
        "",
        f"- Eligible media/files recorded: {counters.eligible:,}",
        f"- Successfully copied: {counters.completed:,}",
        f"- Failed: {counters.failed:,}",
        f"- Total transferred: {readable_bytes(counters.bytes_transferred)}",
        "",
        "## Failed files",
        "",
    ]
    if not failed:
        lines.append("No failed transfer records.")
    else:
        lines.extend(["| Source message | File | Attempts | Error |", "|---:|---|---:|---|"])
        for item in failed:
            name = (item["file_name"] or "Unknown").replace("|", "\\|")
            error = (item["error"] or "Unknown error").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {item['source_message_id']} | {name} | {item['attempts']} | {error} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "This report records media downloaded and uploaded as new destination messages. "
            "It does not contain Telegram API hashes, bot tokens, session data, login codes, or passwords.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
