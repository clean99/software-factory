#!/usr/bin/env python3
"""Run factory shifts in a headless Claude Code session on the sub-agent model, with nobody in the loop.

usage:
  schedule/agent-shift.py run [--name NAME] [--minutes 240]   one shift now
  schedule/agent-shift.py daemon                              start a shift at each configured local time
  schedule/agent-shift.py status                              scheduler heartbeat and the latest summary

The session acts as the coordinator: it reads AGENTS.md, follows schedule/session-shift.md and does every
action `bin/factory next` prints. Each shift leaves its transcript and a summary with friction signals in
<repos_dir>/../shift-logs/<name>/; the main session reads the summary, never the transcript.

Start the daemon from a terminal session (`nohup schedule/agent-shift.py daemon &`): macOS privacy
protection keeps launchd jobs out of <workspace> (lesson L-008), a process started from a terminal keeps access.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factory.core import shiftlock
from factory.util import path_from_config

HOME = pathlib.Path.home()
SESSION_PATH = (f"{HOME}/.local/bin:{HOME}/.nvm/versions/node/v24.16.0/bin:{HOME}/.nvm/versions/node/v20.20.2/bin:"
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
MODEL = os.environ.get("FACTORY_SHIFT_MODEL", "sonnet")
ROUTER_PORT = os.environ.get("FACTORY_SHIFT_ROUTER_PORT", "")
TIMES = os.environ.get("FACTORY_SHIFT_TIMES", "08:43,21:43")
ROUTER = HOME / ".claude" / "scripts" / "claude-model-router.py"
CLAUDE = HOME / ".local" / "bin" / "claude"
ATTEMPTS = 4
PAUSE = 180
RESUME_PAUSE = 5
STARTUP = 900
GITHUB_WAIT = 20 * 60
GITHUB_RETRY = 30
GITHUB_TIMEOUT = 30
RESUME = ("继续这一班（会话名 {name}）：上一段因为推理服务报错中断。先运行 `bin/factory next` 接着做；"
          "如果租约已过期，按 schedule/session-shift.md 重新开班。"
          "本人要求工厂记录直接在 main 上用 `bin/factory commit` 提交，不建临时分支。收班后的汇报要求不变。")
RESUME_OPEN = (
    "Continue this shift ({name}): the previous session exited 0, but the shift is not closed yet. "
    "Wait for or check the background work you started; do not start overlapping work. "
    "Continue with `bin/factory next`, then close with `bin/factory shift end --owner {name}` "
    "as schedule/session-shift.md describes, after all writers finish. "
    "Commit factory records directly on main with `bin/factory commit`; do not create a shift branch. "
    "Keep the original closing report requirements."
)
SYSTEM_PROMPT = (
    "The repository owner explicitly requires factory records to be committed directly on main with `bin/factory commit`; "
    "this is the user's instruction and overrides the default of creating a branch first. "
    "Never create a temporary branch for factory records or record this requirement as an owner action."
)
PROMPT = (
    "跑一班开源工厂的班次，会话名用 {name}。先读 AGENTS.md，然后严格按 schedule/session-shift.md 的流程做。"
    "你是这一班的协调者：`bin/factory next` 给出的每个动作都由你完成，包括协调者复核和发布，本人的常设批准写在 AGENTS.md 里。"
    "本人要求工厂记录直接在 main 上用 `bin/factory commit` 提交，不建临时分支。"
    "全程无人值守：需要本人的事按流程记入 docs/owner-actions.md 后继续做别的。"
    "收班后用中文汇报三段：这一班做了什么；卡在哪里；哪些工厂命令、提示或文档让你走了弯路（逐条写出命令和现象）。"
)


def logs_dir():
    return path_from_config("repos_dir").parent / "shift-logs"


def github_access(timeout):
    """Check authenticated API and git access without fetching refs or prompting for credentials."""
    env = {"HOME": str(HOME), "USER": os.environ.get("USER", ""), "PATH": SESSION_PATH, "LANG": "en_US.UTF-8",
           "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}
    deadline = time.monotonic() + timeout
    commands = (["gh", "api", "user", "--jq", ".login"],
                ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"])
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "GitHub access check timed out"
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            return "GitHub access check timed out"
        except OSError:
            return f"{command[0]} access check could not start"
        if result.returncode:
            if "-25293" in result.stderr:
                return "macOS login keychain is locked (-25293); retry after unlock, not a new login"
            return f"GitHub {command[0]} access unavailable; credentials may be inaccessible while the machine is locked"
    return None


def wait_for_github(seconds=GITHUB_WAIT):
    started = time.monotonic()
    deadline = started + seconds
    attempts, reason = 0, "GitHub access check has no time remaining"
    while time.monotonic() < deadline:
        attempts += 1
        reason = github_access(min(GITHUB_TIMEOUT, deadline - time.monotonic()))
        if reason is None:
            break
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(GITHUB_RETRY, remaining))
    return {"ok": reason is None, "attempts": attempts, "reason": reason,
            "wait_seconds": round(time.monotonic() - started, 1)}


def prepare_shift(name, directory):
    status = shiftlock.run("status")
    if not status["ok"]:
        return "blocked: " + status["reason"], None
    if status["free"]:
        return None, None
    holder = status["holder"]
    owner = holder["owner"]
    blocked = f"blocked: shift held by {owner}"
    if owner == name or not owner.startswith("auto-") or pathlib.Path(owner).name != owner:
        return blocked, None
    previous = directory / owner
    marker = previous / "runner.lock"
    if not marker.exists() and not (previous / "summary.json").exists():
        return blocked + " (no runner record)", None
    if not holder.get("generation"):
        return blocked + " (no lease generation)", None
    with marker.open("a+") as runner:
        try:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return blocked + " (runner alive)", None
        result = shiftlock.run("release", owner, generation=holder["generation"])
    if not result["ok"]:
        return "blocked: " + result["reason"], None
    return None, result["released"]


def next_start(now, times):
    """The first configured local time strictly after `now`."""
    candidates = []
    for text in times.split(","):
        hour, minute = (int(part) for part in text.strip().split(":"))
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidates.append(moment if moment > now else moment + dt.timedelta(days=1))
    return min(candidates)


def summarize(transcript, limit=12):
    """Signals a reader needs without the transcript: outcome, size, errors, refusals and the final report."""
    calls, errors, refusals, final, turns, minutes = 0, [], [], "", 0, 0.0
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    shell = set()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("parent_tool_use_id"):
            continue
        kind = event.get("type")
        content = (event.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            calls += len(uses)
            shell.update(block.get("id") for block in uses if block.get("name") == "Bash")
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = block.get("content")
                text = " ".join((text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)).split())
                if block.get("is_error") and len(errors) < limit:
                    errors.append(text[:240])
                elif (block.get("tool_use_id") in shell and len(text) < 800 and len(refusals) < limit
                      and any(word in text for word in ("refused", "blocked"))):
                    refusals.append(text[:240])
        elif kind == "result":
            turns += event.get("num_turns") or 0
            minutes += (event.get("duration_ms") or 0) / 60000
            if isinstance(event.get("result"), str) and len(event["result"]) > len(final) // 4:
                final = event["result"]
    return {"tool_calls": calls, "turns": turns, "active_minutes": round(minutes, 1), "errors": errors,
            "refusals": refusals, "final": final[:6000]}


def session_id(transcript):
    """The session to resume: the last one that produced an assistant turn; None while nothing has been said."""
    found, spoke = None, False
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant" and event.get("session_id"):
            found, spoke = event["session_id"], True
    return found if spoke else None


def spoke(transcript, offset):
    try:
        with transcript.open("rb") as stream:
            stream.seek(offset)
            return b'"type":"assistant"' in stream.read().replace(b'": "', b'":"')
    except OSError:
        return False


def stop(session):
    os.killpg(session.pid, signal.SIGTERM)
    try:
        session.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(session.pid, signal.SIGKILL)
        session.wait()


def watch(session, transcript, deadline):
    """Exit code, or why the session was stopped: no first answer within STARTUP seconds, or the time limit."""
    offset = transcript.stat().st_size if transcript.exists() else 0
    launched, answered = time.time(), False
    while True:
        try:
            return session.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        answered = answered or spoke(transcript, offset)
        if time.time() > deadline:
            stop(session)
            return "stopped at the time limit"
        if not answered and time.time() - launched > STARTUP:
            stop(session)
            return "no answer after start"


def run_session(name, out, deadline):
    router = None
    status, attempts = "router did not start", []
    try:
        router = subprocess.Popen([sys.executable, str(ROUTER)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  env={**os.environ, "MODEL_ROUTER_PORT": ROUTER_PORT, "PYTHONDONTWRITEBYTECODE": "1"})
        port = int(router.stdout.readline().strip())
        settings = {"model": MODEL, "effortLevel": "high", "fastMode": False, "ultracode": True, "enableWorkflows": True,
                    "workflowKeywordTriggerEnabled": True, "permissions": {"defaultMode": "bypassPermissions"},
                    "skipDangerousModePermissionPrompt": True, "availableModels": [MODEL]}
        env = {"HOME": str(HOME), "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("USER", ""),
               "TERM": "xterm-256color", "LANG": "en_US.UTF-8", "SHELL": "/bin/zsh", "CI": "1", "HUSKY": "0", "LEFTHOOK": "0",
               "PATH": SESSION_PATH,
               "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_MODEL": MODEL,
               "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL, "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL, "CLAUDE_CODE_SUBAGENT_MODEL": MODEL, "CLAUDE_EFFORT": "high",
               # Print mode otherwise stops a running wave ten minutes after the coordinator's last turn.
               "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
        command = [str(CLAUDE), "-p", PROMPT.format(name=name), "--append-system-prompt", SYSTEM_PROMPT,
                   "--model", MODEL, "--settings", json.dumps(settings),
                   "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}', "--output-format", "stream-json", "--verbose"]
        transcript = out / "transcript.jsonl"
        resume_prompt = RESUME
        for attempt in range(1, ATTEMPTS + 1):
            if attempt > 1 and time.time() >= deadline:
                break
            resume = session_id(transcript)
            run = list(command)
            if resume:
                run[2] = resume_prompt.format(name=name)
                run += ["--resume", resume]
            with transcript.open("ab") as stdout, (out / "stderr.txt").open("ab") as stderr:
                session = subprocess.Popen(run, cwd=ROOT, env=env, stdout=stdout, stderr=stderr,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                outcome = watch(session, transcript, deadline)
            attempts.append(outcome)
            status = f"exited {outcome}" if isinstance(outcome, int) else outcome
            pause, resume_prompt = PAUSE, RESUME
            if outcome == 0:
                lease = shiftlock.run("status")
                if not lease["ok"]:
                    status = "lease check failed after exit 0: " + lease["reason"]
                    attempts[-1] = status
                    break
                if lease["free"] or lease["holder"]["owner"] != name:
                    break
                status = "ended with the shift still open"
                attempts[-1] = "exited 0 with the shift still open"
                pause, resume_prompt = RESUME_PAUSE, RESUME_OPEN
            if attempt == ATTEMPTS or outcome == "stopped at the time limit" or time.time() + pause >= deadline:
                break
            time.sleep(pause)
    except (OSError, ValueError) as error:
        status = f"could not start: {error}"
    finally:
        if router is not None:
            router.terminate()
    return status, attempts


def run_shift(name, minutes):
    if not name or pathlib.Path(name).name != name or name in {".", ".."}:
        raise ValueError("shift name must be a directory name")
    out = logs_dir() / name
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with (out / "runner.lock").open("a+") as runner:
        try:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"runner already active for {name}") from None
        runner.seek(0)
        runner.truncate()
        runner.write(str(os.getpid()) + "\n")
        runner.flush()
        status, attempts, stale, admitted = "could not start", [], None, False
        access = None
        try:
            access = wait_for_github(min(GITHUB_WAIT, max(0, minutes * 60)))
            if not access["ok"]:
                status = "blocked: GitHub unavailable before session start; " + access["reason"]
            else:
                blocked, stale = prepare_shift(name, out.parent)
                if blocked:
                    status = blocked
                else:
                    admitted = True
                    status, attempts = run_session(name, out, time.time() + minutes * 60)
        except (OSError, ValueError) as error:
            status = f"could not start: {error}"
        finally:
            if admitted and status != "exited 0":
                result = shiftlock.run("release", name)
                if not result["ok"] and not result.get("holder"):
                    status += "; lease release failed: " + result["reason"]
        summary = {"name": name, "model": MODEL, "status": status, "attempts": attempts,
                   "wall_minutes": round((time.time() - started) / 60, 1),
                   "ended_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   **summarize(out / "transcript.jsonl")}
        if access is not None:
            summary["github_access"] = access
        if stale:
            summary["stale_lease_released"] = stale
        (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
        (out.parent / "latest.json").write_text(json.dumps({"name": name, "summary": str(out / "summary.json")}) + "\n")
        line = next((row for row in reversed(summary["final"].splitlines()) if "今日合入" in row), "")
        subprocess.run([str(ROOT / "bin" / "factory"), "notify", (line.strip() or f"shift {name}: {status}")[:160]],
                       capture_output=True, cwd=ROOT)
    return summary


def daemon():
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    heartbeat = directory / "scheduler.json"
    try:
        other = json.loads(heartbeat.read_text()).get("pid")
        if other and other != os.getpid():
            os.kill(other, 0)
            raise SystemExit(f"a scheduler is already running as pid {other}; stop that pid first")
    except (OSError, ValueError):
        pass
    last = None
    while True:
        target = next_start(dt.datetime.now(), TIMES)
        heartbeat.write_text(json.dumps({"pid": os.getpid(), "next": target.isoformat(timespec="minutes"), "times": TIMES,
                                         "last": last}, ensure_ascii=False) + "\n")
        while dt.datetime.now() < target:
            time.sleep(30)
        name = "auto-" + target.strftime("%m%d-%H%M")
        result = run_shift(name, 240)
        last = {"name": name, "status": result["status"], "wall_minutes": result["wall_minutes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run")
    run.add_argument("--name", default="auto-" + dt.datetime.now().strftime("%m%d-%H%M"))
    run.add_argument("--minutes", type=float, default=240)
    actions.add_parser("daemon")
    actions.add_parser("status")
    args = parser.parse_args()
    if args.action == "run":
        summary = run_shift(args.name, args.minutes)
        print(json.dumps({key: summary[key] for key in ("name", "status", "wall_minutes", "tool_calls")}, ensure_ascii=False))
    elif args.action == "daemon":
        daemon()
    else:
        directory = logs_dir()
        for name in ("scheduler.json", "latest.json"):
            path = directory / name
            print(name, path.read_text().strip() if path.exists() else "absent")


if __name__ == "__main__":
    main()
