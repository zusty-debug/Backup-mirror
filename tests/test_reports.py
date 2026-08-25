from pathlib import Path

from app.database import Database
from app.models import Project
from app.reports import build_project_report


def test_report_excludes_secrets_and_has_project_summary(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    database.ensure_user(7)
    profile_id = database.ensure_profile(7)
    project = Project.draft(
        owner_id=7,
        profile_id=profile_id,
        name="My report",
        source_ref="source",
        destination_ref="destination",
    )
    database.create_project(project)
    output = build_project_report(database, project, tmp_path / "reports")
    report = output.read_text()
    assert "My report" in report
    assert "API Hash" not in report
    database.close()
