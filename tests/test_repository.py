import pytest

from dokku_auto_deploy.repository import Repository, RepositoryError, detect_forge, netrc_password, normalize_url


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        "value, expected",
        [
            pytest.param("https://github.com/Org/proj", "https://github.com/Org/proj", id="plain"),
            pytest.param("https://github.com/Org/proj.git", "https://github.com/Org/proj", id="dot-git"),
            pytest.param(" https://github.com/Org/proj/ ", "https://github.com/Org/proj", id="slash-and-spaces"),
            pytest.param("https://gitlab.com/group/sub/proj", "https://gitlab.com/group/sub/proj", id="nested"),
            pytest.param("http://127.0.0.1:3000/Org/proj", "http://127.0.0.1:3000/Org/proj", id="port"),
        ],
    )
    def test_valid(self, value, expected):
        assert normalize_url(value) == expected

    @pytest.mark.parametrize(
        "value, message",
        [
            pytest.param("Org/proj", "web URL", id="no-scheme"),
            pytest.param("git@github.com:Org/proj.git", "web URL", id="ssh"),
            pytest.param("https://github.com/Org", "owner/name", id="no-name"),
            pytest.param("https://github.com//proj", "owner/name", id="empty-owner"),
            pytest.param("https://bot:secret@github.com/Org/proj", "credentials", id="credentials"),
            pytest.param("https://github.com/Org/proj?tab=code", "query", id="query"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(RepositoryError, match=message):
            normalize_url(value)


def test_detect_forge_from_well_known_hosts_only():
    assert detect_forge("https://github.com/Org/proj") == "github"
    assert detect_forge("https://gitlab.com/group/proj") == "gitlab"
    assert detect_forge("https://codeberg.org/Org/proj") == "forgejo"
    assert detect_forge("https://git.example.com/Org/proj") is None


class TestRepository:
    def test_github_com(self):
        repository = Repository("https://github.com/Org/proj", "github")
        assert (repository.host, repository.path) == ("github.com", "Org/proj")
        assert repository.clone_url == "https://github.com/Org/proj.git"
        assert repository.api_url == "https://api.github.com"

    def test_github_enterprise_api_is_on_the_same_host(self):
        repository = Repository("https://git.example.com:8443/Org/proj", "github")
        assert repository.host == "git.example.com"
        assert repository.api_url == "https://git.example.com:8443/api/v3"

    @pytest.mark.parametrize("forge", ["github", "forgejo"])
    def test_owner_and_name_only(self, forge):
        with pytest.raises(RepositoryError, match="owner/name"):
            Repository("https://git.example.com/Org/sub/proj", forge)

    def test_unknown_forge(self):
        with pytest.raises(RepositoryError, match="unknown forge"):
            Repository("https://github.com/Org/proj", "svn")


class TestNetrc:
    def test_password_of_the_host(self, tmp_path):
        path = tmp_path / ".netrc"
        path.write_text("machine github.com\nlogin bot\npassword GH\nmachine gitlab.com login bot password GL\n")
        assert netrc_password(path, "github.com") == "GH"
        assert netrc_password(path, "gitlab.com") == "GL"
        assert netrc_password(path, "codeberg.org") is None

    def test_missing_file(self, tmp_path):
        assert netrc_password(tmp_path / ".netrc", "github.com") is None

    def test_entry_without_password(self, tmp_path):
        path = tmp_path / ".netrc"
        path.write_text("machine github.com login bot password\n")
        assert netrc_password(path, "github.com") is None

    def test_invalid_file(self, tmp_path):
        path = tmp_path / ".netrc"
        path.write_text("machine github.com login bot bogus x\n")
        with pytest.raises(RepositoryError, match=r"\.netrc"):
            netrc_password(path, "github.com")


def test_gitlab_allows_nested_groups_and_self_managed_hosts():
    repository = Repository("https://git.example.com/group/sub/proj", "gitlab")
    assert (repository.path, repository.api_url) == ("group/sub/proj", "https://git.example.com/api/v4")
