import contextlib
import copy
import json
import os
import pathlib
import tempfile
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS = json.loads((ROOT / "factory.config.json").read_text())
DEFAULTS.update(github_user="fixture-owner", git_name="Contributor",
                git_email="contributor@users.noreply.github.com", git_email_previous=[],
                private_terms=[], private_repos=[], standing_approval_note="Synthetic fixture approval.",
                public={"repository": "example/factory", "dashboard_url": "https://example.github.io/factory/",
                        "disclosure_since": "2026-11-20"})
_read_text = pathlib.Path.read_text


def config_text(path, *args, **kwargs):
    if path == ROOT / ".factory.local.json":
        return "{}"
    return _read_text(path, *args, **kwargs)


with patch.object(pathlib.Path, "read_text", config_text):
    from factory.util import CONFIG


@contextlib.contextmanager
def isolated_config():
    from factory.core import privacy, slop

    environment = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GH_"))}
    with tempfile.TemporaryDirectory() as home:
        # Keep workstation Git settings out without weakening publication's GIT_CONFIG_* guard.
        environment.update(HOME=home, XDG_CONFIG_HOME=str(pathlib.Path(home) / ".config"), GIT_TERMINAL_PROMPT="0")
        with patch.dict(CONFIG, copy.deepcopy(DEFAULTS), clear=True), \
                patch.object(privacy, "identity_markers", return_value=[]), \
                patch.object(slop, "identity_markers", return_value=[]), \
                patch.dict(os.environ, environment, clear=True):
            yield


def setup_module(unittest):
    isolation = isolated_config()
    isolation.__enter__()
    unittest.addModuleCleanup(isolation.__exit__, None, None, None)
