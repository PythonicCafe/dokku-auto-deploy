import pytest

from dokku_auto_deploy import __version__
from dokku_auto_deploy.cli import main
from dokku_auto_deploy.config import CONFIG_TEMPLATE

VALID = """
[settings]
state-file = "{state}"
github-token-file = "{token}"

[[repo]]
repository = "Org/proj"
workflow = ".github/workflows/ci.yml"
stg = {{}}
prd = {{ app = "proj-producao" }}
"""


def write_config(tmp_path, text=None):
    token = tmp_path / "github-token"
    token.write_text("GH\n")
    path = tmp_path / "config.toml"
    path.write_text((text or VALID).format(state=tmp_path / "state.json", token=token))
    return path


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


class TestConfigCommand:
    def test_init_writes_template_and_refuses_to_overwrite(self, tmp_path, capsys):
        path = tmp_path / "etc" / "config.toml"
        assert main(["-c", str(path), "config", "init"]) == 0
        assert path.read_text() == CONFIG_TEMPLATE
        path.write_text("# mine\n")
        assert main(["-c", str(path), "config", "init"]) == 1
        assert path.read_text() == "# mine\n"
        assert main(["-c", str(path), "config", "init", "--force"]) == 0
        assert path.read_text() == CONFIG_TEMPLATE

    def test_show_lists_resolved_targets_on_stdout(self, tmp_path, capsys):
        assert main(["-c", str(write_config(tmp_path)), "config", "show"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out == [
            "Org/proj develop -> proj-stg (workflow: .github/workflows/ci.yml, notify: -)",
            "Org/proj main -> proj-producao (workflow: .github/workflows/ci.yml, notify: -)",
        ]

    def test_show_with_invalid_config_exits_3(self, tmp_path, capsys):
        path = tmp_path / "config.toml"
        path.write_text('[[repo]]\nrepository = "x"\n')
        assert main(["-c", str(path), "config", "show"]) == 3
        assert "owner/name" in capsys.readouterr().err


class TestPollCommand:
    def test_without_repositories_exits_3(self, tmp_path, capsys):
        path = tmp_path / "config.toml"
        path.write_text(CONFIG_TEMPLATE)
        assert main(["-c", str(path), "poll"]) == 3
        assert "no repositories" in capsys.readouterr().err

    def test_missing_github_token_exits_3(self, tmp_path, capsys):
        path = write_config(tmp_path)
        (tmp_path / "github-token").unlink()
        assert main(["-c", str(path), "poll"]) == 3
        assert "github-token" in capsys.readouterr().err

    def test_force_unknown_app_exits_3(self, tmp_path, capsys):
        assert main(["-c", str(write_config(tmp_path)), "poll", "--force", "nope"]) == 3
        assert "nope" in capsys.readouterr().err

    def test_polls_every_target(self, tmp_path, fake_api, fake_dokku, monkeypatch, capsysbinary):
        monkeypatch.setenv("DOKKU_AUTO_DEPLOY_GITHUB_API", fake_api.url)
        for branch, sha in (("develop", "d1"), ("main", "m1")):
            fake_api.routes[f"GET /repos/Org/proj/branches/{branch}"] = (200, {"commit": {"sha": sha}})
        runs = [{"id": 1, "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success"}]
        fake_api.routes["GET /repos/Org/proj/actions/runs"] = (200, {"workflow_runs": runs})
        fake_dokku.set(output="-----> built\n")
        assert main(["-c", str(write_config(tmp_path)), "poll"]) == 0
        assert fake_dokku.syncs == [
            "git:sync --build proj-stg https://github.com/Org/proj.git d1",
            "git:sync --build proj-producao https://github.com/Org/proj.git m1",
        ]
        assert capsysbinary.readouterr().out == b"-----> built\n" * 2
