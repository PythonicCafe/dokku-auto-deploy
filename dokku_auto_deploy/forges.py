"""The `Forge` implementation for a repository."""

from dokku_auto_deploy.forge import Forge
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.repository import Repository


def make_forge(repository: Repository, token: str) -> Forge:
    return GitHub(repository, token)
