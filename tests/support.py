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
    from factory.oss import records

    # A test run inside a contribution shift inherits the scheduler's run start; tests that need one set it themselves.
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GIT_", "GH_")) and k != "FACTORY_RUN_STARTED_AT"}
    with tempfile.TemporaryDirectory() as home:
        # Keep workstation Git settings out without weakening publication's GIT_CONFIG_* guard.
        environment.update(HOME=home, XDG_CONFIG_HOME=str(pathlib.Path(home) / ".config"), GIT_TERMINAL_PROMPT="0",
                           FACTORY_GH_PAUSE=str(pathlib.Path(home) / "github-pause.json"))
        with patch.dict(CONFIG, copy.deepcopy(DEFAULTS), clear=True), \
                patch.object(privacy, "identity_markers", return_value=[]), \
                patch.object(slop, "identity_markers", return_value=[]), \
                patch.object(records, "ROOT", pathlib.Path(home)), \
                patch.dict(os.environ, environment, clear=True):
            yield


def setup_module(unittest):
    isolation = isolated_config()
    isolation.__enter__()
    unittest.addModuleCleanup(isolation.__exit__, None, None, None)


def is_pulls_read(args):
    """A GitHub REST read of a repository's pull request list (gh api -X GET repos/<owner>/<name>/pulls ...)."""
    return len(args) > 3 and args[0] == "api" and str(args[3]).endswith("/pulls")


def rest_pulls(prs, login="fixture-owner"):
    """REST pulls-list entries for fixtures written in gh pr list's shape (url, state, createdAt, mergedAt)."""
    rows = []
    for pr in prs:
        merged = pr.get("mergedAt") or ("2026-11-01T00:00:00Z" if pr.get("state") == "MERGED" else None)
        rows.append({"html_url": pr["url"], "state": "open" if pr.get("state", "OPEN") == "OPEN" else "closed",
                     "merged_at": merged, "created_at": pr.get("createdAt", "2026-11-01T00:00:00Z"), "user": {"login": login}})
    return rows
