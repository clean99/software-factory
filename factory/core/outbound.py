import json
import os
import pathlib
import re
import shlex
import signal
import sys
from urllib.parse import urlsplit

from factory.core.publish_commands import READS, WRITES, dynamic, gh_kind, git_parts, github, options, private_destination, push_urls


GH_MESSAGE = "GitHub write blocked; use bin/factory gh (or the item's submit/reply/open-issue/propose command)."
PUSH_MESSAGE = "GitHub push blocked; use bin/factory push (or the item's submit command)."
FACTORY = pathlib.Path(__file__).resolve().parents[2] / "bin" / "factory"


def unwrap(words, cwd):
    env = dict(os.environ)
    changed = False
    while words:
        if words[0] in {"then", "do", "else", "elif", "if", "!", "{", "}"}:
            words.pop(0)
            continue
        if re.match(r"^[A-Za-z_]\w*=", words[0]):
            key, value = words.pop(0).split("=", 1)
            env[key] = value
            changed = True
            continue
        exe = pathlib.Path(words[0]).name
        if exe not in {"sudo", "env", "command", "nohup", "time"}:
            break
        words.pop(0)
        while words and words[0].startswith("-"):
            flag = words.pop(0)
            if flag == "--":
                break
            if exe == "command" and flag in {"-v", "-V"}:
                return [], cwd, env
            if exe == "env" and flag in {"-i", "--ignore-environment"}:
                env = {}
                changed = True
            takes_value = ((exe == "env" and flag in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
                           or (exe == "sudo" and flag in {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-D", "--chdir"})
                           or (exe == "time" and flag in {"-f", "--format", "-o", "--output"}))
            if takes_value and words:
                value = words.pop(0)
                if exe == "env" and flag in {"-u", "--unset"}:
                    env.pop(value, None)
                    changed = True
                elif flag in {"--chdir", "-D"} or (exe == "env" and flag == "-C"):
                    cwd = cwd / value if cwd is not None and not dynamic(value) else None
                elif exe == "env" and flag in {"-S", "--split-string"}:
                    words[:0] = shlex.split(value)
            elif (exe == "env" and flag.startswith(("--split-string=", "-S"))):
                value = flag.split("=", 1)[1] if flag.startswith("--") else flag[2:]
                words[:0] = shlex.split(value)
            elif (flag.startswith("--chdir=") or (exe == "env" and flag.startswith("-C")) or (exe == "sudo" and flag.startswith("-D"))):
                value = flag.split("=", 1)[1] if flag.startswith("--") else flag[2:]
                cwd = cwd / value if cwd is not None and not dynamic(value) else None
    return words, cwd, env if changed else None


def http_write(exe, args):
    if exe == "curl" and any(arg in {"--next", "-:"} for arg in args):
        requests = [[]]
        for arg in args:
            if arg in {"--next", "-:"}:
                requests.append([])
            else:
                requests[-1].append(arg)
        return any(http_write(exe, request) for request in requests)
    urls, methods = [], []
    data = query = False
    bodies = []
    i = 0
    while i < len(args):
        arg = args[i]
        key, equal, value = arg.partition("=")
        if arg in {"-H", "--header", "-o", "--output", "-u", "--user", "-A", "--user-agent", "-e", "--referer", "--proxy", "--cacert", "--cert", "--key", "--connect-timeout", "--max-time"}:
            i += 2
            continue
        if arg in {"-I", "--head"}:
            methods.append("HEAD")
        elif arg in {"-X", "--request", "--method", "--url"}:
            i += 1
            value = args[i] if i < len(args) else ""
            if arg == "--url":
                urls.append(value)
            else:
                methods.append(value.upper())
        elif key in {"--request", "--method"} and equal:
            methods.append(value.upper())
        elif arg.startswith("-X"):
            methods.append(arg[2:].upper())
        elif arg.startswith(("https://", "http://", "api.github.com", "uploads.github.com")):
            urls.append(arg)
        elif key == "--url" and equal:
            urls.append(value)
        elif arg in {"-G", "--get"}:
            query = True
        elif (key in {"--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "--form", "--form-string", "--upload-file", "--post-data", "--post-file", "--body-data", "--body-file"}
              or arg.startswith(("-d", "-F", "-T"))):
            data = True
            if equal:
                bodies.append(value)
            elif arg in {"-d", "-F", "-T"} or key.startswith("--"):
                i += 1
                bodies.append(args[i] if i < len(args) else "")
            else:
                bodies.append(arg[2:])
        elif exe in {"http", "xh", "https"}:
            if arg.upper() in {"GET", "HEAD", "OPTIONS", "POST", "PATCH", "PUT", "DELETE"}:
                methods.append(arg.upper())
            elif ("=" in arg and "==" not in arg) or ":=" in arg or arg.startswith("@"):
                data = True
        i += 1
    hosts = []
    for url in urls:
        try:
            hosts.append(urlsplit(url if "://" in url else "https://" + url).hostname)
        except ValueError:
            pass
    if not any(h in {"api.github.com", "uploads.github.com"} for h in hosts):
        return False
    if any(urlsplit(url if "://" in url else "https://" + url).path.rstrip("/") == "/graphql" for url in urls) and (not methods or methods[-1] == "POST"):
        from factory.core.publish_commands import graphql_operations
        for body in bodies:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and isinstance(payload.get("query"), str) and "mutation" in graphql_operations(payload["query"]):
                    return True
            except ValueError:
                pass
        return False
    return ((methods and methods[-1] not in {"GET", "HEAD", "OPTIONS"}) or (data and not query and not (methods and methods[-1] in {"GET", "HEAD", "OPTIONS"})))


def gh_write(args):
    try:
        opts, words = options(args)
    except ValueError:
        return False
    if gh_kind(args) == "read":
        return False
    if words[:2] and len(words) > 1 and words[1] in READS.get(words[0], set()):
        return False
    if words[:2] == ["api", "graphql"]:
        methods = [v.upper() for key, v in opts if key in {"-X", "--method"}]
        if methods and methods[-1] not in {"GET", "HEAD", "OPTIONS", "POST"}:
            return True
        from factory.core.publish_commands import graphql_operations
        return any("mutation" in graphql_operations(value.partition("=")[2])
                   for key, value in opts if key in {"-f", "--raw-field", "-F", "--field"} and value.startswith("query="))
    if words[:1] == ["api"]:
        return gh_kind(args) == "write"
    extra = {"pr": {"close", "reopen", "merge", "ready", "lock", "unlock"},
             "issue": {"close", "reopen", "delete", "transfer", "lock", "unlock", "pin", "unpin"},
             "gist": {"edit", "delete", "rename"}, "release": {"delete", "delete-asset"},
             "repo": {"delete", "rename", "archive", "unarchive", "fork"},
             "label": {"create", "edit", "delete", "clone"},
             "variable": {"set", "delete"}, "secret": {"set", "delete"},
             "project": {"create", "edit", "close", "copy", "delete", "field-create", "field-delete", "item-add", "item-create", "item-edit", "item-delete", "item-archive", "link", "unlink"}}
    return len(words) > 1 and words[1] in WRITES.get(words[0], set()) | extra.get(words[0], set())


def segment(words, cwd, depth):
    words, cwd, env = unwrap(list(words), cwd)
    if not words:
        return []
    exe, args = pathlib.Path(words[0]).name, words[1:]
    if cwd is not None and (cwd / words[0]).resolve() == FACTORY:
        return []
    if exe in {"bash", "sh", "zsh", "dash"}:
        if env is not None:
            return []
        for i, arg in enumerate(args):
            if arg.startswith("-") and "c" in arg[1:] and i + 1 < len(args):
                return check(args[i + 1], cwd, depth + 1)
        return []
    if exe == "gh":
        try:
            return [GH_MESSAGE] if gh_write(args) else []
        except ValueError:
            return []
    if exe == "git":
        try:
            repo, prefix, rest = git_parts(args, cwd)
            if not rest:
                return []
            if rest[0] != "push" or any(a in {"--help", "-h"} for a in rest[1:]):
                return []
            urls = push_urls(rest[1:], repo, prefix, env)
            return [PUSH_MESSAGE] if any(github(url) and not private_destination(url) for url in urls) else []
        except (OSError, ValueError, TimeoutError):
            return []
    if exe in {"curl", "wget", "http", "https", "xh"} and http_write(exe, args):
        return [GH_MESSAGE]
    return []


def word_end(source, start):
    quote = None
    i = start
    while i < len(source):
        char = source[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in " \t\r\n;&|()<>":
            break
        i += 1
    if quote:
        raise ValueError("unclosed shell quote")
    return i


def heredoc_source(command):
    pending, pieces = [], []
    i = copied = arithmetic = 0
    while i < len(command):
        char = command[i]
        if char == "#":
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
        elif char == "\n":
            i += 1
            if pending:
                pieces.append(command[copied:i])
                for delimiter, tabs in pending:
                    while i < len(command):
                        end = command.find("\n", i)
                        end = len(command) if end < 0 else end + 1
                        line = command[i:end].removesuffix("\n")
                        if (line.lstrip("\t") if tabs else line) == delimiter:
                            break
                        i = end
                    else:
                        raise ValueError("unclosed heredoc")
                    i = end
                pending = []
                copied = i
        elif command.startswith("((", i):
            arithmetic += 1
            i += 2
        elif arithmetic and command.startswith("))", i):
            arithmetic -= 1
            i += 2
        elif command.startswith("<<<", i):
            i += 3
        elif arithmetic and command.startswith("<<", i):
            i += 2
        elif command.startswith("<<", i):
            i += 2
            tabs = command.startswith("-", i)
            i += int(tabs)
            while i < len(command):
                if command[i] in " \t":
                    i += 1
                elif command.startswith("\\\n", i):
                    i += 2
                else:
                    break
            end = word_end(command, i)
            raw = command[i:end].replace("\\\n", "")
            words = shlex.split(raw)
            if len(words) != 1:
                raise ValueError("unresolved heredoc delimiter")
            pending.append((words[0], tabs))
            i = end
        elif char in " \t\r;&|()<>":
            i += 1
        else:
            i = word_end(command, i)
    if pending:
        raise ValueError("unclosed heredoc")
    pieces.append(command[copied:])
    return "".join(pieces)


def shell_tokens(source):
    i = 0
    while i < len(source):
        char = source[i]
        if char in " \t\r":
            i += 1
        elif char == "#":
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif char in ";&|()\n<>":
            end = i + 1
            while end < len(source) and source[end] == char and char in ";&|<>":
                end += 1
            yield source[i:end], char in ";&|()\n"
            i = end
        else:
            end = word_end(source, i)
            yield "".join(shlex.split(source[i:end])), False
            i = end


def check(command, cwd, depth=0):
    if depth > 8:
        return []
    command = command.replace("\\\n", "")
    try:
        if "<<" in command:
            command = heredoc_source(command)
        tokens = list(shell_tokens(command))
    except ValueError:
        return []
    found, part, stack = [], [], []
    conditional = False

    def flush():
        nonlocal cwd, part
        if part:
            found.extend(segment(part, cwd, depth))
            words, _, _ = unwrap(list(part), cwd)
            if words and words[0] == "cd":
                value = words[-1] if len(words) > 1 else "~"
                if conditional or words != part:
                    cwd = None
                else:
                    cwd = (cwd / os.path.expanduser(value)).resolve() if cwd is not None and not dynamic(value) else None
            part = []

    for token, operator in tokens:
        if operator:
            flush()
            conditional = "&&" in token or "||" in token
            for char in token:
                if char == "(":
                    stack.append(cwd)
                elif char == ")" and stack:
                    cwd = stack.pop()
        else:
            part.append(token)
    flush()
    return list(dict.fromkeys(found))


def main():
    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(5)
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        command = event.get("tool_input", {}).get("command", "")
        if not isinstance(command, str) or len(command) > 2_000_000:
            raise ValueError("invalid command")
        found = check(command, pathlib.Path(event.get("cwd") or os.getcwd()))
    except Exception:
        found = []
    finally:
        signal.alarm(0)
    if found:
        print(found[0], file=sys.stderr)
        return 2
    return 0
