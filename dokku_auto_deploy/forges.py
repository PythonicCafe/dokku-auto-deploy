"""The `Forge` implementation for a repository."""

from dokku_auto_deploy.forge import Forge
from dokku_auto_deploy.forgejo import Forgejo
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.gitlab import GitLab
from dokku_auto_deploy.repository import Repository

FORGE_CLASSES: dict[str, type[GitHub] | type[GitLab] | type[Forgejo]] = {
    "github": GitHub,
    "gitlab": GitLab,
    "forgejo": Forgejo,
}


def make_forge(repository: Repository, token: str) -> Forge:
    return FORGE_CLASSES[repository.forge](repository, token)
