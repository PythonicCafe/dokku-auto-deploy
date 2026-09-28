import sys

from dokku_auto_deploy.dokku import git_sync, is_locked, run_streaming


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
