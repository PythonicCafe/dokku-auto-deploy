import stat

from dokku_auto_deploy.properties import GLOBAL, Properties


def test_same_layout_as_dokku(tmp_path):
    properties = Properties(tmp_path)
    properties.set("app", "branch", "main")
    path = tmp_path / "app" / "branch"
    assert path.read_text() == "main"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in (tmp_path / "app").iterdir()] == ["branch"]


def test_get_set_delete(tmp_path):
    properties = Properties(tmp_path)
    assert properties.get("app", "branch") is None
    properties.set("app", "branch", "main")
    properties.set("app", "branch", "develop")
    assert properties.get("app", "branch") == "develop"
    properties.delete("app", "branch")
    properties.delete("app", "branch")
    assert properties.get("app", "branch") is None


def test_empty_value_is_unset(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "branch").write_text("")
    assert Properties(tmp_path).get("app", "branch") is None


def test_apps_exclude_global(tmp_path):
    properties = Properties(tmp_path)
    assert properties.apps() == []
    for app in ("b", GLOBAL, "a"):
        properties.set(app, "notify", "none")
    assert properties.apps() == ["a", "b"]


def test_default_root_follows_dokku_lib_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DOKKU_LIB_ROOT", str(tmp_path))
    assert Properties().root == tmp_path / "config" / "auto-deploy"
