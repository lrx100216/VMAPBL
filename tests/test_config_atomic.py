from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core import config


def test_save_roi_preserves_existing_file_when_replace_fails(tmp_path):
    path = tmp_path / "settings.yaml"
    original = "# preserve me\nroi: {}\n"
    path.write_text(original, encoding="utf-8")
    with patch("core.config.os.replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            config.save_roi("bed", [[0, 0], [3, 0], [3, 3]], settings_path=path)
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".settings.yaml.*.tmp"))


def test_delete_final_roi_keeps_yaml_parseable_and_comments(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("# preserve me\nroi:\n  bed: [[0, 0], [3, 0], [3, 3]]\nother: 1\n", encoding="utf-8")
    assert config.delete_roi("bed", settings_path=path)
    text = path.read_text(encoding="utf-8")
    assert "# preserve me" in text
    assert yaml.safe_load(text) == {"roi": {}, "other": 1}


def test_backup_restore_and_invalid_backup_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    original = "# original\nroi: {}\n"
    path.write_text(original, encoding="utf-8")
    config.save_roi("bed", [[0, 0], [3, 0], [3, 3]], settings_path=path)
    backup = path.with_name("settings.yaml.bak")
    assert backup.read_text(encoding="utf-8") == original
    config.restore_settings_backup(path)
    assert path.read_text(encoding="utf-8") == original
    backup.write_text("roi: [broken", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        config.restore_settings_backup(path)
    assert path.read_text(encoding="utf-8") == original


def test_live_replace_failure_preserves_current_and_backup(tmp_path, monkeypatch):
    import os
    path = tmp_path / "settings.yaml"
    original = "roi: {}\n"
    path.write_text(original, encoding="utf-8")
    real_replace = os.replace

    def fail_live(source, destination):
        if Path(destination) == path:
            raise OSError("replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", fail_live)
    with pytest.raises(OSError):
        config.save_roi("bed", [[0, 0], [3, 0], [3, 3]], settings_path=path)
    assert path.read_text(encoding="utf-8") == original
    assert path.with_name("settings.yaml.bak").read_text(encoding="utf-8") == original
