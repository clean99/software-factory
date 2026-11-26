"""Run proof commands and store signed observations of their output and exit status."""
import argparse
import codecs
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque

from factory.core import clones, verdicts
from factory.util import ROOT, CommandError, Item, git, read_json, run, write_json

ENV = {"CI": "1", "HUSKY": "0", "LEFTHOOK": "0"}


def scrub_home(text):
    return text.replace(str(pathlib.Path.home()), "~")


def _scrub_argv(argv):
    home = str(pathlib.Path.home())
    scrubbed, replacements = [], {}
    for index, arg in enumerate(argv):
        parts = arg.split(home)
        value, positions = parts[0], []
        for part in parts[1:]:
            positions.append(len(value))
            value += "~" + part
        scrubbed.append(value)
        if positions:
            replacements[str(index)] = positions
    return scrubbed, replacements


def _restore_argv(receipt):
    argv = list(receipt["argv"])
    home = str(pathlib.Path.home())
    replacements = receipt.get("argv_home")
    if not isinstance(replacements, dict):
        raise ValueError("receipt lacks reversible argv metadata; capture it again with factory run")
    for index, positions in replacements.items():
        index = int(index)
        for position in reversed(positions):
            if argv[index][position:position + 1] != "~":
                raise ValueError("receipt has invalid argv metadata; capture it again with factory run")
            argv[index] = argv[index][:position] + home + argv[index][position + 1:]
    return argv


def clean(receipt):
    """Whether the captured command observed one unchanged, tracked-clean commit."""
    return (receipt.get("clean") is True and bool(receipt.get("head_before"))
            and receipt.get("head_before") == receipt.get("head_after") == receipt.get("cwd_head")
            and receipt.get("tracked_dirty_before") is False and receipt.get("tracked_dirty_after") is False)


def restore_home(text):
    """Reverse scrub_home() for a stored cwd, wherever the home directory appeared in it.

    When the home directory is a symlink, the resolved cwd contains the home
    path in its middle rather than as a prefix, so "~" is not always leading.
    """
    home = str(pathlib.Path.home())
    return str(text).replace("~", home)


def command_cwd(item, cwd=None):
    clone = item.clone.resolve()
    path = pathlib.Path(restore_home(cwd)) if cwd else clone
    return (path if path.is_absolute() else clone / path).resolve()


def command_matches(argv, command):
    if (not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg.strip() for arg in argv)
            or not isinstance(command, str) or not command.strip()):
        return False
    if (len(argv) == 3 and pathlib.Path(argv[0]).name in ("sh", "bash", "zsh")
            and argv[1] in ("-c", "-lc") and argv[2].strip() == command.strip()):
        return True
    try:
        return argv == shlex.split(command)
    except ValueError:
        return False


def redgreen_command(item):
    """Read the designated regression command from the current red-to-green proof."""
    fix = "Run bin/factory redgreen first to record a successful test command and cwd bound to the item head."
    try:
        data = read_json(item.file("redgreen.json"))
        head = item.head_sha()
        if (not isinstance(data, dict) or data.get("ok") is not True or data.get("state", "complete") != "complete"
                or any(not isinstance(data.get(key), str) or not data[key].strip() for key in ("cmd", "head_sha"))
                or not re.fullmatch(r"[0-9a-fA-F]{7,40}", data["head_sha"])
                or not verdicts.bound(item, head, data["head_sha"])
                or not isinstance(data.get("cwd", "."), str) or not data.get("cwd", ".").strip()):
            raise ValueError(fix)
        return scrub_home(data["cmd"]), command_cwd(item, data.get("cwd", "."))
    except (OSError, ValueError, CommandError, KeyError):
        raise ValueError(fix) from None


def _id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + secrets.token_hex(2)


def _path(item, receipt_id):
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", receipt_id):
        raise ValueError("invalid receipt id; use an id printed by factory receipts")
    return item.file("receipts") / f"{receipt_id}.json"


def _key(create=False):
    path = ROOT / ".receipt-key"
    if create and not path.exists():
        with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".receipt-key-", delete=False) as stream:
            pending = pathlib.Path(stream.name)
            stream.write(secrets.token_bytes(32))
        try:
            os.link(pending, path)
        except FileExistsError:
            pass
        finally:
            pending.unlink()
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError(".receipt-key must contain 32 bytes; restore the original key or rerun the proofs with a new key")
    return key


def _canonical(receipt):
    return json.dumps({k: v for k, v in receipt.items() if k != "mac"},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def save(item, fields):
    receipt = dict(fields)
    receipt.setdefault("id", _id())
    path = _path(item, receipt["id"])
    if isinstance(receipt.get("cwd"), str):
        receipt["cwd"] = scrub_home(receipt["cwd"])
    if "argv" in receipt and "argv_home" not in receipt:
        receipt["argv"], receipt["argv_home"] = _scrub_argv(receipt["argv"])
    if "log_tail" in receipt:
        receipt["log_tail"] = [scrub_home(line)[:400] for line in receipt["log_tail"][-40:]]
    receipt["mac"] = hmac.new(_key(create=True), _canonical(receipt), hashlib.sha256).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, receipt)
    return receipt


def load(item):
    receipts = []
    for path in sorted(item.file("receipts").glob("*.json")):
        receipt = get(item, path.stem)
        if receipt is not None:
            receipts.append(receipt)
    return sorted(receipts, key=lambda receipt: receipt["id"])


def get(item, receipt_id):
    try:
        receipt = read_json(_path(item, receipt_id))
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) and receipt.get("id") == receipt_id else None


def valid(receipt):
    if not isinstance(receipt, dict) or not isinstance(receipt.get("mac"), str):
        return False
    try:
        mac = hmac.new(_key(), _canonical(receipt), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, receipt["mac"])
    except (OSError, ValueError, TypeError):
        return False


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _capture(proc, log, timeout):
    deadline = time.monotonic() + timeout if timeout is not None else None
    timed_out = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map() or proc.poll() is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                _kill(proc)
                timed_out, deadline, remaining = True, None, None
            for key, _ in selector.select(0.1 if remaining is None else min(0.1, remaining)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                log.write(chunk)
                log.flush()
                sys.stdout.write(decoder.decode(chunk))
                sys.stdout.flush()
    sys.stdout.write(decoder.decode(b"", final=True))
    sys.stdout.flush()
    return "timeout" if timed_out else proc.wait()


def _log_details(path):
    digest = hashlib.sha256()
    tail = deque(maxlen=40)
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            tail.append(scrub_home(line.decode("utf-8", errors="replace").rstrip("\r\n"))[:400])
    return digest.hexdigest(), list(tail)


def _state(cwd):
    head = git(cwd, "rev-parse", "HEAD", check=False) or None
    dirty = [run(["git", "-C", str(cwd), "diff", *args], check=False).returncode != 0
             for args in (("--quiet", "HEAD"), ("--cached", "--quiet"))]
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard", "-z", check=False)
    return head, any(dirty), len([path for path in untracked.split("\0") if path])


def run_cmd(item, argv, cwd=None, label="", timeout=None, expect_fail=False):
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("pass a non-empty argv list; use ['bash', '-lc', script] for a shell command")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive number of seconds")
    cwd = command_cwd(item, cwd)
    if cwd.is_relative_to(item.clone.resolve()):
        clones.ensure(item)
    head = item.head_sha()
    directory = item.file("receipts")
    directory.mkdir(parents=True, exist_ok=True)
    _key(create=True)
    while True:
        receipt_id = _id()
        log_path = directory / f"{receipt_id}.log"
        try:
            log = log_path.open("xb")
            break
        except FileExistsError:
            continue
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    clock = time.monotonic()
    with log:
        head_before, dirty_before, untracked_before = _state(cwd)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **ENV},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            raise CommandError(f"cannot run {scrub_home(argv[0])}: {scrub_home(str(error))}; check the executable and cwd") from error
        try:
            exit_code = _capture(proc, log, timeout)
        except BaseException:
            _kill(proc)
            proc.wait()
            raise
        finally:
            proc.stdout.close()
    duration = time.monotonic() - clock
    head_after, dirty_after, untracked_after = _state(cwd)
    digest, tail = _log_details(log_path)
    return save(item, {"id": receipt_id, "label": label, "argv": argv, "cwd": str(cwd),
                       "cwd_head": head_before, "item_head": head, "exit": exit_code, "expect_fail": expect_fail,
                       "head_before": head_before, "head_after": head_after,
                       "tracked_dirty_before": dirty_before, "tracked_dirty_after": dirty_after,
                       "clean": bool(head_before) and head_before == head_after and not dirty_before and not dirty_after,
                       "untracked_before": untracked_before, "untracked_after": untracked_after,
                       "untracked": max(untracked_before, untracked_after), "timeout": timeout,
                       "started": started, "duration_s": round(duration, 6), "log_sha256": digest,
                       "log_tail": tail, "env": dict(ENV)})


def rerun(item, receipt_id, timeout=None):
    previous = get(item, receipt_id)
    if not valid(previous):
        raise ValueError("receipt is missing or invalid; run the proof again with factory run")
    argv = _restore_argv(previous)
    receipt = run_cmd(item, argv, cwd=previous["cwd"], label=previous.get("label", ""),
                      timeout=previous.get("timeout") if timeout is None else timeout,
                      expect_fail=previous.get("expect_fail", False))
    receipt["rerun_of"] = receipt_id
    receipt["match"] = {"exit": receipt["exit"] == previous["exit"],
                        "output": receipt["log_sha256"] == previous["log_sha256"]}
    return save(item, receipt)


def _result(item, receipt):
    print(f"{receipt['id']} exit={receipt['exit']} clean={str(clean(receipt)).lower()} "
          f"untracked: {receipt.get('untracked', 0)} log={item.file('receipts') / (receipt['id'] + '.log')}")
    exit_code = receipt["exit"]
    matches = type(exit_code) is int and (exit_code > 0 if receipt.get("expect_fail") else exit_code == 0)
    raise SystemExit(0 if matches else 1)


def cmd_run(args):
    item = Item(args.item)
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    _result(item, run_cmd(item, argv, cwd=args.cwd, label=args.label, timeout=args.timeout, expect_fail=args.expect_fail))


def cmd_rerun(args):
    item = Item(args.item)
    _result(item, rerun(item, args.id, timeout=args.timeout))


def cmd_receipts(args):
    print("id\tlabel\texit\thead\tvalid")
    for receipt in load(Item(args.item)):
        print(f"{receipt['id']}\t{receipt.get('label', '')}\t{receipt.get('exit')}\t"
              f"{(receipt.get('cwd_head') or '-')[:12]}\t{str(valid(receipt)).lower()}")


def register(sub):
    parser = sub.add_parser("run", help="run a proof command and capture a signed receipt")
    parser.add_argument("--item", required=True)
    parser.add_argument("--cwd", help="working directory (relative paths are based on the item's clone)")
    parser.add_argument("--label", default="")
    parser.add_argument("--timeout", type=float, help="maximum running time in seconds")
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    parser.set_defaults(fn=cmd_run)
    parser = sub.add_parser("rerun", help="repeat a signed proof command and compare its result")
    parser.add_argument("--item", required=True)
    parser.add_argument("id")
    parser.add_argument("--timeout", type=float, help="override the original running-time limit in seconds")
    parser.set_defaults(fn=cmd_rerun)
    parser = sub.add_parser("receipts", help="list the item's proof receipts and their signatures")
    parser.add_argument("--item", required=True)
    parser.set_defaults(fn=cmd_receipts)
