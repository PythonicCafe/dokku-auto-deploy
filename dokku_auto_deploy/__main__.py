"""Run `python -m dokku_auto_deploy` with the same entry point as the `dokku-auto-deploy` console script."""

import sys

from dokku_auto_deploy.cli import main

if __name__ == "__main__":
    sys.exit(main())
