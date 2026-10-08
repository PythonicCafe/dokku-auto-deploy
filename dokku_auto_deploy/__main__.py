"""`python3 -m dokku_auto_deploy auto-deploy:<command> ...` runs a command without Dokku (development)."""

import sys

from dokku_auto_deploy.cli import main

if __name__ == "__main__":
    sys.exit(main())
