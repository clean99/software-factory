"""Extract per-role execution identities from workflow transcripts."""
import json
import pathlib
import re
import shlex

from factory.util import ROOT, Item, read_json, write_json


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return "\n".join(_text(value) for value in content.values())
    if isinstance(content, list):
        return "\n".join(_text(part) for part in content)
    return ""


def _item_path(value):
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    # Preserve real names ending in punctuation; tolerate punctuation copied from prose only when absent.
    if not path.exists():
        trimmed = path.with_name(path.name.rstrip(".,;:")) if path.name.rstrip(".,;:") else path
        if trimmed.exists():
            path = trimmed
    return path


def _same_item(value, item):
    try:
        path = _item_path(value)
        return path.resolve() in (item.dir, item.dir / "item.json")
    except (OSError, ValueError, RuntimeError):
        return False


def _words(prompt):
    """Shell words of a prompt; a stray quote in one line (prose, a harness preamble) spoils only that line."""
    text = prompt.replace("`", " ")
    try:
        return shlex.split(text)
    except ValueError:
        pass
    words = []
    for line in text.splitlines():
        try:
            words += shlex.split(line)
        except ValueError:
            words += [word.strip("'\"") for word in line.split()]
    return words


def _names_item(prompt, item):
    words = _words(prompt)
    values = []
    for i, word in enumerate(words):
        value = words[i + 1] if word == "--item" and i + 1 < len(words) else None
        if word.startswith("--item="):
            value = word.partition("=")[2]
        if value:
            values.append(value)
    # Structured prose allows unquoted spaces in the path; a following clause is not its value.
    values.extend(match[1].strip().strip("'\"") for match in
                  re.finditer(r"(?<![\w-])item\s*[:=]\s*([^\n;]+)", prompt))
    if values:
        return all(_same_item(value, item) for value in values)
    names = {str(item.dir), str(item.dir / "item.json")}
    for base in (ROOT.resolve(), pathlib.Path.home()):
        try:
            relative = item.dir.relative_to(base)
        except ValueError:
            continue
        names.add(("~/" if base == pathlib.Path.home() else "") + str(relative))
    return any(re.search(r"(?<![\w./-])" + re.escape(name) + r"(?=$|[\s'\"`),;:\]}]|/|\.(?=\s|$))", prompt)
               for name in names)


def _role(prompt):
    from factory.core.briefs import ROLES
    words = _words(prompt)
    for i, word in enumerate(words[:-1]):
        if word == "brief" and words[i + 1] in ROLES:
            return words[i + 1]
    match = re.search(r"\brole\s*[:=]\s*([a-z-]+)", prompt, re.I)
    return match[1].lower() if match and match[1].lower() in ROLES else "unclassified"


def _bare_item(prompt):
    # The documented task form is role=review Review <path>, not role=scout Compare with <path>.
    match = re.search(r"\b(?:role\s*[:=]\s*|brief\s+)[a-z-]+\s+"
                      r"(?:(?:review|implement|fix|follow|record|takeover|scout)\s+)?"
                      r"(['\"]?(?:/|~/|runs/)[^\n;]+)", prompt, re.I)
    return match[1].strip().strip("'\"") if match else None


def _has_item(prompt):
    return bool(re.search(r"--item(?:[=\s]|$)|\bitem\s*[:=]", prompt) or _bare_item(prompt))


def _brief_target(prompt):
    if _role(prompt) == "unclassified":
        return None
    words = _words(prompt)
    for i, word in enumerate(words):
        for flag in ("--repo", "--pr"):
            if word == flag and i + 1 < len(words):
                return flag, words[i + 1]
            if word.startswith(flag + "="):
                return flag, word.partition("=")[2]
    return None


def _task_text(prompt):
    # Harness context can quote other items and briefs; it is not this worker's assignment.
    return re.sub(r"<(system-reminder|task-notification)\b[^>]*>.*?</\1>", "", prompt, flags=re.S)


def _target_matches(target, item):
    if target is None:
        return True
    flag, value = target
    if flag == "--repo":
        return value.lower() == item.meta.get("repo", "").lower()
    return value == item.meta.get("pr_url")


def _assignment_text(prompt):
    """A role line and adjacent assignment fields, not unrelated paths in the task's context."""
    lines = prompt.replace("\\\n", " ").splitlines()
    field = re.compile(r"^\s*(?:item\s*[:=]|--(?:item|repo|pr)(?:[=\s]|$))")
    for index, line in enumerate(lines):
        if _role(line) == "unclassified":
            continue
        start, end = index, index + 1
        while start > 0 and field.match(lines[start - 1]):
            start -= 1
        while end < len(lines) and field.match(lines[end]):
            end += 1
        return "\n".join(lines[start:end])
    return ""


def _assigned_item(prompt, item):
    scope = _assignment_text(prompt)
    if not scope or not _target_matches(_brief_target(scope), item):
        return False
    if re.search(r"--item(?:[=\s]|$)|\bitem\s*[:=]", scope):
        return _names_item(scope, item)
    value = _bare_item(scope)
    return value is not None and _same_item(value, item)


def _brief_call(block):
    if block.get("name", "").rsplit(".", 1)[-1] != "Bash":
        return ""
    data = block.get("input") or {}
    command = data.get("command") if isinstance(data, dict) else None
    if not isinstance(command, str):
        return ""
    # Accept one literal invocation. Do not borrow arguments from another shell command.
    command = command.replace("\\\n", " ").strip()
    if any(char in command for char in ";\n&|<>()`$"):
        return ""
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return ""
    if len(words) >= 3 and pathlib.PurePosixPath(words[0]).name == "factory" and words[1] == "brief":
        return shlex.join(words)
    return ""


def _item_file_call(block, item):
    if block.get("name", "").rsplit(".", 1)[-1] not in {"Write", "Edit"}:
        return False
    data = block.get("input") or {}
    value = data.get("file_path") if isinstance(data, dict) else None
    if not isinstance(value, str):
        return False
    try:
        path = _item_path(value).resolve()
        return path.is_relative_to(item.dir) and path != item.dir and path.name != "item.json"
    except (OSError, ValueError, RuntimeError):
        return False


def _new_call(block):
    data = block.get("input")
    command = data.get("command") if isinstance(data, dict) else None
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(pathlib.PurePosixPath(word).name == "factory" and words[i + 1] == "new"
               for i, word in enumerate(words[:-1]))


def _created_item(block, item, new_calls):
    if block.get("is_error"):
        return False
    call = block.get("tool_use_id") or block.get("tool_call_id")
    if call and call not in new_calls:
        return False
    # Older transcripts retain the creation output but omit tool-call identifiers.
    for match in re.finditer(r"^item: (.+)$", _text(block.get("content")), re.M):
        if _same_item(match[1].strip(), item):
            return True
    return False


def collect(item, transcripts, diagnostics=None):
    if not isinstance(item, Item):
        item = Item(item)
    directory = pathlib.Path(transcripts).expanduser()
    if not directory.is_dir():
        raise ValueError(f"Transcript directory {directory} does not exist; pass the workflow run's transcript directory")
    paths = sorted(directory.rglob("*.jsonl"))
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in directory.iterdir())
    found = {"directory": str(directory), "entries": entries[:20], "entries_total": len(entries),
             "jsonl_files": len(paths), "jsonl_paths": [str(p.relative_to(directory)) for p in paths[:20]],
             "message_rows": 0, "malformed_rows": 0, "assistant_model_rows": 0, "item_segments": 0}
    if diagnostics is not None:
        diagnostics.update(layout=("**/*.jsonl with JSON rows containing message.role (or row.type), message.content and assistant message.model; "
                                   "a role line with its item path or adjacent --item/item= fields must match the item's repository; "
                                   "simple factory brief invocations and item-output Write/Edit calls also count; "
                                   "harness reminders, reference reads and loose mentions are excluded; scouts may return a successful factory new result"), found=found)
    result = []
    rejected = []
    if diagnostics is not None:
        diagnostics["rejected"] = rejected
    seen = set()
    for path in paths:
        assigned = False
        scout = False
        started = False
        target = None
        role = "unclassified"
        models = []
        new_calls = set()
        explicit_other = False
        identity = str(path.relative_to(directory).with_suffix(""))

        def finish():
            if not assigned or role == "unclassified" or not models:
                if role == "unclassified":
                    reason = "no recognized role"
                elif not _target_matches(target, item):
                    reason = "repository or PR target differs from the item"
                elif explicit_other:
                    reason = "assigned to a different item"
                elif not assigned:
                    reason = "no item-scoped assignment or tool call; mentions alone are not work evidence"
                else:
                    reason = "no assistant model in the matching segment"
                rejected.append({"transcript": str(path.relative_to(directory)), "agent": identity,
                                 "role": role, "reason": reason})
                return
            found["item_segments"] += 1
            for model in models:
                key = (role, identity, model)
                if key in seen:
                    continue
                seen.add(key)
                result.append(dict(zip(("role", "agent", "model"), key)))

        with path.open(errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    found["malformed_rows"] += 1
                    continue
                if not isinstance(row, dict):
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                found["message_rows"] += 1
                kind = message.get("role") or row.get("type")
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                if kind == "user":
                    prompt = content if isinstance(content, str) else "\n".join(
                        _text(block.get("text")) for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text")
                    prompt = _task_text(prompt)
                    scope = _assignment_text(prompt)
                    next_role = _role(scope)
                    next_target = _brief_target(scope)
                    same_item = _assigned_item(scope, item) or (assigned and not scope and _names_item(prompt, item))
                    names_item = same_item or _has_item(scope)
                    changed_item = names_item and not (assigned and same_item)
                    changed_target = next_target is not None and next_target != target
                    changed_role = next_role != "unclassified" and next_role != role
                    if prompt.strip() and (not started or changed_item or changed_target or changed_role):
                        if started:
                            finish()
                        models = []
                        new_calls = set()
                        if names_item or changed_target:
                            assigned = same_item and _target_matches(next_target, item)
                            explicit_other = names_item and not same_item
                        if next_role != "unclassified" or changed_item or changed_target:
                            role = next_role
                        scout = role == "scout" and not assigned and not names_item
                        target = next_target
                        started = True
                        identity = str(row.get("agentId") or row.get("agent_id") or identity)
                if kind == "assistant":
                    model = message.get("model")
                    if isinstance(model, str) and model:
                        found["assistant_model_rows"] += 1
                    for block in blocks:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        command = _brief_call(block)
                        if command:
                            next_role = _role(command)
                            next_target = _brief_target(command)
                            same_item = _assigned_item(command, item)
                            if next_role != role or next_target != target or same_item != assigned:
                                if started and role != "unclassified":
                                    finish()
                                    models = []
                                assigned = same_item
                                role, target = next_role, next_target
                                explicit_other = _has_item(command) and not same_item
                                scout = role == "scout" and not explicit_other and not assigned
                                new_calls = set()
                                started = True
                        elif role != "unclassified" and not explicit_other and _target_matches(target, item) and _item_file_call(block, item):
                            assigned = True
                        if _new_call(block):
                            new_calls.add(block.get("id"))
                    if isinstance(model, str) and model:
                        models.append(model)
                if scout and _target_matches(target, item):
                    if kind == "tool":
                        outputs = [message]
                    elif kind == "user":
                        outputs = [block for block in blocks
                                   if isinstance(block, dict) and block.get("type") == "tool_result"]
                    else:
                        outputs = []
                    if any(_created_item(block, item, new_calls) for block in outputs):
                        assigned = True
        finish()
    return result


def models(item):
    """Model per role as the transcripts show it (the last agent of each role); empty without provenance.json."""
    try:
        rows = read_json(item.file("provenance.json"), [])
    except (OSError, ValueError):
        return {}
    found = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("role"), str) and isinstance(row.get("model"), str) and row["model"].strip():
            found[row["role"]] = row["model"].strip()
    return found


def cmd_provenance(args):
    item = Item(_item_path(args.item))
    diagnostics = {}
    try:
        rows = collect(item, args.transcripts, diagnostics)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    path = item.file("provenance.json")
    result = {"path": str(path), "agents": len(rows), "written": bool(rows), **diagnostics}
    if rows:
        write_json(path, rows)
    else:
        result["reason"] = ("No matching sessions; existing provenance left untouched. Supply the original session transcripts, "
                            "not summary.json or command logs; see docs/repairs.md#execution-provenance for coordinator work.")
    print(json.dumps(result, ensure_ascii=False))


def register(sub):
    parser = sub.add_parser("provenance", help="Record execution identities from workflow transcripts")
    parser.add_argument("--item", required=True)
    parser.add_argument("--transcripts", required=True)
    parser.set_defaults(fn=cmd_provenance)
