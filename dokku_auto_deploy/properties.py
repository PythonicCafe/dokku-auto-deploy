"""Dokku's plugin property store, read and written the way Dokku itself does (`plugins/common/properties.go`).

One file per key at `<DOKKU_LIB_ROOT>/config/<plugin>/<app>/<key>` holding the raw value, mode 0600; global values
live under an app named `--global`. Using the same layout lets the bash triggers manage the same data with Dokku's
`fn-plugin-property-*` functions (clone on rename, destroy on delete).
"""

import os
import tempfile
from pathlib import Path

PLUGIN = "auto-deploy"
GLOBAL = "--global"


def lib_root() -> Path:
    return Path(os.environ.get("DOKKU_LIB_ROOT", "/var/lib/dokku"))


def dokku_root() -> Path:
    """Home of the dokku user, where `dokku git:auth` keeps `.netrc`."""
    return Path(os.environ.get("DOKKU_ROOT") or Path("~dokku").expanduser())


def data_dir() -> Path:
    return lib_root() / "data" / PLUGIN


class Properties:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else lib_root() / "config" / PLUGIN

    def get(self, app: str, key: str) -> str | None:
        """Value of `key`, or None when unset (Dokku treats an empty value as unset too)."""
        try:
            return (self.root / app / key).read_text() or None
        except FileNotFoundError:
            return None

    def set(self, app: str, key: str, value: str) -> None:
        """Write atomically (temp file + rename), so a concurrent reader never sees half a value."""
        directory = self.root / app
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=directory, prefix=f".{key}.", delete=False) as temp:
            temp.write(value)
        Path(temp.name).chmod(0o600)
        Path(temp.name).replace(directory / key)

    def delete(self, app: str, key: str) -> None:
        (self.root / app / key).unlink(missing_ok=True)

    def apps(self) -> list[str]:
        """Apps with at least one property (the global pseudo-app excluded), sorted."""
        if not self.root.is_dir():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_dir() and path.name != GLOBAL)
