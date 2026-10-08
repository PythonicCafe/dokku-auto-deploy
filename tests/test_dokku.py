import sys
import time

import pytest

from dokku_auto_deploy.dokku import app_exists, app_url, git_sync, is_locked, lock_state, run_streaming, runs_commit
from tests.conftest import FAKE_APP


def test_run_streaming_returns_output_and_code_while_streaming():
    chunks = []
    code, output = run_streaming([sys.executable, "-c", "print('a'); print('b'); exit(3)"], 10, chunks.append)
    assert (code, output) == (3, "a\nb\n")
    assert b"".join(chunks) == b"a\nb\n"


def test_run_streaming_kills_on_timeout():
    code, output = run_streaming([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"], 1)
    assert code != 0
    assert output.startswith("start\n") and "killed after 1s" in output


def test_run_streaming_timeout_kills_child_processes_too():
    """Like `dokku` running a build: the children keep stdout open after the parent is gone."""
    script = "import subprocess, time; print('start', flush=True); subprocess.Popen(['sleep', '30']); time.sleep(30)"
    start = time.perf_counter()
    code, output = run_streaming([sys.executable, "-c", script], 1)
    assert time.perf_counter() - start < 10
    assert code != 0 and "killed after 1s" in output


class TestBuildSupervision:
    """The build must never be left running unsupervised: it holds the app's deploy lock."""

    @staticmethod
    def script(marker, ignore_sigint=False):
        """A build that prints a line, then writes `marker` 1s later (unless it was stopped)."""
        ignore = "import signal; signal.signal(signal.SIGINT, signal.SIG_IGN); " if ignore_sigint else ""
        return [
            sys.executable,
            "-c",
            f"{ignore}import time, pathlib; print('start', flush=True); time.sleep(1); pathlib.Path({str(marker)!r}).touch()",
        ]

    def test_failing_output_callback_does_not_stop_following_the_build(self, tmp_path):
        def broken(line):
            raise BrokenPipeError("terminal gone")

        code, output = run_streaming(self.script(tmp_path / "done"), 10, broken)
        assert (code, output) == (0, "start\n")
        assert (tmp_path / "done").exists()

    def test_interrupt_stops_the_build(self, tmp_path):
        def interrupt(line):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_streaming(self.script(tmp_path / "done"), 10, interrupt)
        time.sleep(1.5)
        assert not (tmp_path / "done").exists()

    def test_build_ignoring_the_interrupt_is_killed_after_the_grace_period(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dokku_auto_deploy.dokku.INTERRUPT_GRACE", 0.2)

        def interrupt(line):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_streaming(self.script(tmp_path / "done", ignore_sigint=True), 10, interrupt)
        time.sleep(1.5)
        assert not (tmp_path / "done").exists()


class TestLockState:
    @pytest.mark.parametrize(
        ("lock", "state", "locked"),
        [
            (None, "free", False),
            ("manual", "manual", True),
            ("deploying", "deploying", True),
            ("orphan", "orphan", False),
        ],
    )
    def test_lock_file_meaning(self, fake_dokku, lock, state, locked):
        fake_dokku.set(lock=lock)
        assert lock_state(FAKE_APP) == state
        assert is_locked(FAKE_APP) is locked

    def test_build_id_missing_from_builds_list_counts_as_deploying(self, fake_dokku):
        """Like Dokku before 0.38, which has no `builds:list`: without the record, waiting is the safe side."""
        fake_dokku.set(lock="orphan")
        (fake_dokku.directory / "builds.json").write_text("[]")
        assert lock_state(FAKE_APP) == "deploying"


def test_git_sync_uses_exact_sha(fake_dokku):
    fake_dokku.set(exit_code=0, output="ok\n")
    assert git_sync("app", "https://github.com/Org/proj.git", "abc") == (True, "ok\n", False)
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


def test_app_exists(fake_dokku):
    fake_dokku.remove_app("gone")
    assert app_exists("app") is True
    assert app_exists("gone") is False
