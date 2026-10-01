import sys

import pytest

from dokku_auto_deploy.dokku import app_url, git_sync, is_locked, run_streaming, runs_commit


def test_run_streaming_returns_output_and_code_while_streaming():
    chunks = []
    code, output = run_streaming([sys.executable, "-c", "print('a'); print('b'); exit(3)"], 10, chunks.append)
    assert (code, output) == (3, "a\nb\n")
    assert b"".join(chunks) == b"a\nb\n"


def test_run_streaming_kills_on_timeout():
    code, output = run_streaming([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"], 1)
    assert code != 0
    assert output.startswith("start\n") and "killed after 1s" in output


def test_is_locked(fake_dokku):
    fake_dokku.set(locked=True)
    assert is_locked("app") is True
    fake_dokku.set(locked=False)
    assert is_locked("app") is False


def test_git_sync_uses_exact_sha(fake_dokku):
    fake_dokku.set(exit_code=0, output="ok\n")
    assert git_sync("app", "Org/proj", "abc") == (True, "ok\n")
    assert fake_dokku.syncs == ["git:sync --build app https://github.com/Org/proj.git abc"]


@pytest.mark.parametrize(
    "metadata, expected",
    [
        pytest.param("abc123", True, id="git-push"),
        pytest.param("https://github.com/Org/proj.git#abc123", True, id="git-sync"),
        pytest.param("https://github.com/Org/proj.git#def456", False, id="other-commit"),
        pytest.param("", False, id="never-deployed"),
        pytest.param("registry.example.com/app:abc123", False, id="image-deploy"),
    ],
)
def test_runs_commit_reads_the_last_successful_deploy(fake_dokku, metadata, expected):
    fake_dokku.set(deploy_source=metadata)
    assert runs_commit("app", "abc123") is expected
    assert "apps:report app --app-deploy-source-metadata" in fake_dokku.calls


def test_app_url_is_the_first_url(fake_dokku):
    fake_dokku.set(url="https://app.example.com")
    assert app_url("app") == "https://app.example.com"
    assert "url app" in fake_dokku.calls


def test_app_url_is_none_without_domains(fake_dokku):
    fake_dokku.set(url="")
    assert app_url("app") is None
