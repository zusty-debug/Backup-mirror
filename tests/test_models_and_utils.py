from app.models import MediaType, ProjectSettings
from app.utils import readable_bytes, safe_filename, truncate


def test_settings_round_trip_and_defaults() -> None:
    settings = ProjectSettings(preserve_captions=True, media_types=[MediaType.PHOTO.value])
    restored = ProjectSettings.from_json(settings.to_json())
    assert restored.preserve_captions is True
    assert restored.allows(MediaType.PHOTO)
    assert not restored.allows(MediaType.DOCUMENT)


def test_safe_file_name_removes_path_and_control_characters() -> None:
    assert safe_filename("../../bad\\name\x00.pdf", "fallback.pdf") == "bad_name_.pdf"
    assert safe_filename(None, "fallback.pdf") == "fallback.pdf"


def test_display_helpers() -> None:
    assert readable_bytes(1024) == "1.0 KB"
    assert truncate("a" * 10, 5) == "aaaa…"
