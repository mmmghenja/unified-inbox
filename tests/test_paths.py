from hub import paths


def test_data_dir_is_private(tmp_path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir(mode=0o755)
    monkeypatch.setenv("HUB_DATA_DIR", str(target))

    assert paths.data_dir() == target
    assert target.stat().st_mode & 0o777 == 0o700


def test_config_dir_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "a" / "cfg"))

    assert paths.config_dir().stat().st_mode & 0o777 == 0o700
