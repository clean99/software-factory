import contextlib
import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess

from factory.core.privacy import redact


CONFIG_FILE = pathlib.Path(__file__).with_name("gitleaks.toml")
# Raw reports of successful text scans, kept only while a reuse() block is open.
_reuse = {"depth": 0, "reports": {}, "runs": 0}


@contextlib.contextmanager
def reuse():
    """Within one operation, scan identical bytes once per scanner binary and rule file.

    Only a complete report is kept; waivers, carried findings and labels are applied on every call."""
    _reuse["depth"] += 1
    try:
        yield
    finally:
        _reuse["depth"] -= 1
        if not _reuse["depth"]:
            _reuse["reports"].clear()


def report_key(executable, text):
    if not _reuse["depth"]:
        return None
    try:
        binary = pathlib.Path(executable).resolve()
        info = binary.stat()
        rules = hashlib.sha256(CONFIG_FILE.read_bytes()).hexdigest()
    except OSError:
        return None
    return (str(binary), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, rules,
            hashlib.sha256(text.encode()).hexdigest())


def waivers(item_dir):
    if item_dir is None:
        return set()
    path = pathlib.Path(item_dir) / "gitleaks-allow.json"
    if not path.exists():
        return set()
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or any(not isinstance(r, str) or not r.strip() for r in rows):
        raise ValueError("gitleaks-allow.json must be a list of exact fingerprints")
    return set(rows)


def scan(*, text=None, repo=None, revisions=None, item_dir=None, label="text", fingerprints=(), carried=None):
    executable = shutil.which("gitleaks")
    if not executable:
        return ["gitleaks is required; install gitleaks before publishing"]
    try:
        allowed = waivers(item_dir) | set(fingerprints)
    except (OSError, ValueError):
        return ["cannot read item gitleaks-allow.json; expected a fingerprint list"]
    key = report_key(executable, text) if text is not None else None
    reused = _reuse["reports"].get(key)
    command = [executable, "stdin"] if text is not None else [executable, "git", str(repo), "--log-opts", revisions]
    command += ["--config", str(CONFIG_FILE), "--gitleaks-ignore-path", os.devnull,
                "--ignore-gitleaks-allow", "--redact=100", "--no-banner", "--no-color",
                "--report-format", "json", "--report-path", "-", "--exit-code", "23", "--timeout", "8"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLEAKS_", "GIT_"))}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        if reused is not None:
            rows = copy.deepcopy(reused)
        else:
            result = subprocess.run(command, input=text, text=True, capture_output=True,
                                    cwd=CONFIG_FILE.parent, env=env, timeout=10)
            _reuse["runs"] += 1
            if result.returncode not in (0, 23):
                return ["gitleaks scan failed; publishing blocked"]
            rows = json.loads(result.stdout or "[]")
            if not isinstance(rows, list) or (result.returncode == 23 and not rows):
                raise ValueError("incomplete report")
        report = copy.deepcopy(rows)
        found = []
        for row in rows:
            if carried and carried(row):
                continue
            fingerprint = row["Fingerprint"]
            if text is not None:
                fingerprint = hashlib.sha256(text.encode()).hexdigest() + fingerprint
            if fingerprint in allowed:
                continue
            found.append(redact(f"{label}: gitleaks {row['RuleID']} at line {row['StartLine']}; fingerprint {fingerprint}"))
        if key is not None:
            # Stored only after every row was readable, so a malformed report is never reused.
            _reuse["reports"][key] = report
        return found
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return ["gitleaks scan incomplete or timed out; publishing blocked"]
