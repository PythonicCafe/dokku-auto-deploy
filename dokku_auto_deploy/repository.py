"""Repository URLs: which forge hosts them, where its API is, how to clone them and which credentials to use."""

import dataclasses
import netrc
import urllib.parse
from pathlib import Path

FORGES = ("github", "gitlab", "forgejo")
# Hosts whose forge is known; any other host needs the `forge` setting
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "forgejo"}


class RepositoryError(ValueError):
    pass


def normalize_url(value: str) -> str:
    """`https://host/owner/name(.git)(/)` -> `https://host/owner/name`; raises `RepositoryError` if it isn't one."""
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise RepositoryError(f"expected the repository web URL, like https://github.com/owner/name, got {value!r}")
    if parts.username or parts.password:
        raise RepositoryError(
            "the repository URL must not contain credentials (they would show up in `ps` during git:sync); "
            "use `dokku git:auth` instead"
        )
    if parts.query or parts.fragment:
        raise RepositoryError(f"the repository URL must not have a query string or fragment, got {value!r}")
    path = parts.path.strip("/").removesuffix(".git").strip("/")
    if len(path.split("/")) < 2 or any(not segment for segment in path.split("/")):
        raise RepositoryError(f"expected owner/name after the host in {value!r}")
    return f"{parts.scheme}://{parts.netloc}/{path}"


def detect_forge(url: str) -> str | None:
    return KNOWN_HOSTS.get(urllib.parse.urlsplit(url).hostname or "")


@dataclasses.dataclass(frozen=True)
class Repository:
    url: str  # Normalized by `normalize_url`
    forge: str

    def __post_init__(self) -> None:
        if self.forge not in FORGES:
            raise RepositoryError(f"unknown forge {self.forge!r} (expected one of: {', '.join(FORGES)})")
        if self.forge in ("github", "forgejo") and len(self.path.split("/")) != 2:
            raise RepositoryError(f"a {self.forge} repository URL has exactly owner/name after the host: {self.url}")

    @property
    def host(self) -> str:
        """Host name as `dokku git:auth` and `.netrc` know it (no port)."""
        return urllib.parse.urlsplit(self.url).hostname or ""

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path.strip("/")

    @property
    def clone_url(self) -> str:
        return f"{self.url}.git"

    @property
    def api_url(self) -> str:
        parts = urllib.parse.urlsplit(self.url)
        if self.forge == "gitlab":
            return f"{parts.scheme}://{parts.netloc}/api/v4"
        if self.forge == "forgejo":
            return f"{parts.scheme}://{parts.netloc}/api/v1"
        if self.host == "github.com":
            return "https://api.github.com"
        return f"{parts.scheme}://{parts.netloc}/api/v3"  # GitHub Enterprise Server


def _netrc_entry(path: Path, host: str) -> tuple[str, str] | None:
    try:
        entry = netrc.netrc(str(path)).authenticators(host)
    except FileNotFoundError:
        return None
    except netrc.NetrcParseError as exc:
        raise RepositoryError(f"cannot read {path}: {exc.msg} (line {exc.lineno})") from None
    if entry is None or not entry[2]:
        return None
    return entry[0], entry[2]


def netrc_password(path: Path, host: str) -> str | None:
    """Password stored for `host` in the netrc file at `path` (what `dokku git:auth <host> <user>` writes)."""
    entry = _netrc_entry(path, host)
    return entry[1] if entry else None


def netrc_login(path: Path, host: str) -> str | None:
    """Login stored with that password: says which account the token belongs to, without showing the token."""
    entry = _netrc_entry(path, host)
    return entry[0] if entry else None
