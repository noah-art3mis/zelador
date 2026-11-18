"""Tests for zelador.config — env credentials, data dir, config.yaml, Zotero dir discovery."""

from pathlib import Path

import pytest

from zelador import config


class TestCredentials:
    def test_reads_key_and_user_id_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZOTERO_API_KEY", "sekrit")
        monkeypatch.setenv("ZOTERO_USER_ID", "12345")
        creds = config.load_credentials(dotenv_path=tmp_path / ".env")
        assert creds.api_key == "sekrit"
        assert creds.user_id == "12345"

    def test_missing_key_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.setenv("ZOTERO_USER_ID", "12345")
        with pytest.raises(config.ConfigError, match="ZOTERO_API_KEY"):
            config.load_credentials(dotenv_path=tmp_path / ".env")

    def test_missing_user_id_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZOTERO_API_KEY", "sekrit")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        with pytest.raises(config.ConfigError, match="ZOTERO_USER_ID"):
            config.load_credentials(dotenv_path=tmp_path / ".env")


class TestConfigDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZELADOR_CONFIG_DIR", str(tmp_path / "cfg"))
        assert config.config_dir() == tmp_path / "cfg"

    def test_defaults_to_platform_config_dir(self, monkeypatch):
        import platformdirs

        monkeypatch.delenv("ZELADOR_CONFIG_DIR", raising=False)
        assert config.config_dir() == Path(platformdirs.user_config_dir("zelador"))

    def test_config_files_live_in_the_config_dir(self, isolated_config_dir):
        assert config.env_file() == isolated_config_dir / ".env"
        assert config.config_file() == isolated_config_dir / "config.yaml"
        assert config.taxonomy_file() == isolated_config_dir / "taxonomy.yaml"

    def test_credentials_load_from_config_dir_env(self, monkeypatch, isolated_config_dir):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        isolated_config_dir.mkdir()
        (isolated_config_dir / ".env").write_text("ZOTERO_API_KEY=k\nZOTERO_USER_ID=7\n")
        creds = config.load_credentials()
        assert (creds.api_key, creds.user_id) == ("k", "7")

    def test_missing_credentials_name_the_config_dir_env(self, monkeypatch, isolated_config_dir):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        with pytest.raises(config.ConfigError, match=str(isolated_config_dir / ".env")):
            config.load_credentials()

    def test_load_config_reads_config_dir_by_default(self, isolated_config_dir):
        isolated_config_dir.mkdir()
        (isolated_config_dir / "config.yaml").write_text("style: chicago\n")
        assert config.load_config().style == "chicago"


class TestDataDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "custom"))
        assert config.data_dir() == tmp_path / "custom"

    def test_defaults_to_platformdirs(self, monkeypatch):
        monkeypatch.delenv("ZELADOR_DATA_DIR", raising=False)
        path = config.data_dir()
        assert path.name == "zelador"

    def test_subdirs_created_on_demand(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "d"))
        backups = config.ensure_dir("backups")
        assert backups.is_dir()
        assert backups == tmp_path / "d" / "backups"


class TestConfigFile:
    def test_defaults_when_file_absent(self, tmp_path):
        cfg = config.load_config(tmp_path / "config.yaml")
        assert cfg.style == "apa"
        assert cfg.zotero_data_dir is None
        assert cfg.citekey_sources == []

    def test_reads_all_three_keys(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            "zotero_data_dir: /mnt/c/Users/x/Zotero\n"
            "citekey_sources: [refs.bib, 'notes/**/*.md']\n"
            "style: chicago-note-bibliography\n"
        )
        cfg = config.load_config(f)
        assert str(cfg.zotero_data_dir) == "/mnt/c/Users/x/Zotero"
        assert cfg.citekey_sources == ["refs.bib", "notes/**/*.md"]
        assert cfg.style == "chicago-note-bibliography"

    def test_scalar_citekey_sources_fails_loudly(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("citekey_sources: refs.bib\n")
        with pytest.raises(config.ConfigError, match="citekey_sources"):
            config.load_config(f)

    def test_unknown_key_fails_loudly(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("zotero_dir: /wrong\n")
        with pytest.raises(config.ConfigError, match="zotero_dir"):
            config.load_config(f)


class TestZoteroDirDiscovery:
    def test_config_override_wins(self, tmp_path):
        override = tmp_path / "MyZotero"
        override.mkdir()
        assert config.discover_zotero_dir(override=override) == override

    def test_home_zotero_on_linux(self, tmp_path, monkeypatch):
        (tmp_path / "Zotero").mkdir()
        (tmp_path / "Zotero" / "zotero.sqlite").touch()
        monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
        assert config.discover_zotero_dir(wsl=False) == tmp_path / "Zotero"

    def test_wsl_scans_mounted_profiles(self, tmp_path, monkeypatch):
        users = tmp_path / "mnt" / "c" / "Users"
        (users / "noah_" / "Zotero").mkdir(parents=True)
        (users / "noah_" / "Zotero" / "zotero.sqlite").touch()
        (users / "Public").mkdir()
        monkeypatch.setattr(config, "WINDOWS_USERS_ROOT", users)
        assert config.discover_zotero_dir(wsl=True) == users / "noah_" / "Zotero"

    def test_wsl_skips_profiles_without_a_database(self, tmp_path, monkeypatch):
        users = tmp_path / "mnt" / "c" / "Users"
        (users / "All Users" / "Zotero").mkdir(parents=True)  # junction, no database
        (users / "noah_" / "Zotero").mkdir(parents=True)
        (users / "noah_" / "Zotero" / "zotero.sqlite").touch()
        monkeypatch.setattr(config, "WINDOWS_USERS_ROOT", users)
        assert config.discover_zotero_dir(wsl=True) == users / "noah_" / "Zotero"

    def test_not_found_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
        with pytest.raises(config.ConfigError, match="[Zz]otero"):
            config.discover_zotero_dir(wsl=False)
