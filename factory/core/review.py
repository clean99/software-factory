"""Validate evidence-backed reviews, preserve fix rounds and render human-readable copies."""
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from factory.core import receipts, timeline, verdicts
from factory.core.checks import check, matches_required
from factory.util import CommandError, Item, ROOT, git, parse_iso, read_json, write_json

PARTS = {"demand": "demand.json", "value": "value-review.json",
         "review": "review.json", "coordinator": "coordinator-review.json"}
VALUE_ANSWERS = ("source_is_real", "input_is_reachable_by_users", "contract_supports_it",
                 "not_by_design", "benefit_exceeds_review_cost")
DEMAND_KINDS = ("external-report", "maintainer-request", "live-capture", "documented-contract")


def classes():
    return read_json(ROOT / "lines/oss/risk-classes.json")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _object(value):
    return value if isinstance(value, dict) else {}


def _load(item, name, problems):
    try:
        data = read_json(item.file(name))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        problems.append(f"Add a valid JSON object in {name}.")
        return None
    return data


def _required(data, keys, name, problems):
    for key in keys:
        if not _text(data.get(key)):
            problems.append(f"Add non-empty {name}.{key}.")


def _source(item, value):
    if not _text(value) or ("://" in value and any(c.isspace() for c in value.strip())):
        return None
    value = value.strip()
    try:
        url = urlsplit(value)
        if url.scheme:
            if url.scheme.lower() not in ("http", "https") or not url.hostname or url.username:
                return None
            path = url.path.rstrip("/")
            ignored = {"ref", "ref_src", "source", "share", "notification_referrer_id", "view",
                       "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
            query = urlencode(sorted((key, value) for key, value in parse_qsl(url.query, keep_blank_values=True)
                                     if not key.lower().startswith("utm_") and key.lower() not in ignored))
            if url.hostname.lower() == "github.com":
                parts = path.split("/")
                if len(parts) >= 3:
                    parts[1:3] = [part.lower() for part in parts[1:3]]
                if len(parts) >= 5 and parts[3].lower() in ("issues", "pull", "discussions") and parts[4].isdigit():
                    space = "discussions" if parts[3].lower() == "discussions" else "issues"
                    return f"github.com/{parts[1]}/{parts[2]}/{space}/{int(parts[4])}"
                path = "/".join(parts)
            return url._replace(scheme=url.scheme.lower(), netloc=url.netloc.lower(), path=path, query=query, fragment="").geturl()
        path = pathlib.Path(value)
        if path.is_absolute():
            return None
        path = (item.dir / path).resolve()
        path.relative_to(item.dir.resolve())
        return str(path) if path.is_file() else None
    except (OSError, ValueError):
        return None


def validate_demand(item):
    problems = []
    data = _load(item, PARTS["demand"], problems)
    if data is None:
        return problems
    _required(data, ("claim", "workaround"), "demand", problems)
    who = _object(data.get("who_hits_it"))
    kind = who.get("kind")
    if kind not in DEMAND_KINDS:
        problems.append("Add demand.who_hits_it.kind as external-report, maintainer-request, live-capture or documented-contract.")
    source = _source(item, who.get("source"))
    if not source:
        problems.append("Add demand.who_hits_it.source as a URL or an existing item-relative evidence file.")
    _required(who, ("quote",), "demand.who_hits_it", problems)
    if type(who.get("self_filed")) is not bool:
        problems.append("Add demand.who_hits_it.self_filed as true or false.")
    contract = _object(data.get("contract"))
    contract_source = _source(item, contract.get("source"))
    if kind == "documented-contract" or contract.get("says") or contract.get("source"):
        _required(contract, ("says",), "demand.contract", problems)
        if not contract_source:
            problems.append("Add demand.contract.source as a URL or an existing item-relative evidence file.")
    if not isinstance(data.get("contract"), dict):
        problems.append("Add demand.contract with says and source fields, leaving them empty only when no contract is cited.")
    if type(data.get("error_path")) is not bool:
        problems.append("Add demand.error_path as true or false.")
    occurrence = _source(item, data.get("real_occurrence"))
    independent = bool(occurrence) and occurrence not in (source, contract_source)
    reported_occurrence = (bool(occurrence) and occurrence == source and who.get("self_filed") is False
                           and kind in ("external-report", "maintainer-request", "live-capture"))
    if data.get("error_path") is True or data.get("real_occurrence"):
        if not (independent or reported_occurrence):
            problems.append("Add demand.real_occurrence as a user report or live capture; a self-filed or documentation-only source needs a different corroborating resource, not provider documentation (L-066).")
    if who.get("self_filed") is True and not independent:
        problems.append("Add a second, external source in demand.real_occurrence, different from who_hits_it.source and contract.source; a self-filed issue alone is not demand.")
    if data.get("decision") not in verdicts.VALUE_VERDICTS:
        problems.append("Add demand.decision as build, propose or drop.")
    return problems


def validate_value(item):
    problems = []
    data = _load(item, PARTS["value"], problems)
    if data is None:
        return problems
    _required(data, ("reviewer",), "value-review", problems)
    sha = verdicts.demand_sha256(item)
    if not sha or data.get("demand_sha256") != sha:
        problems.append("value-review.json is stale or lacks demand_sha256 for the current demand.json; rerun the value review.")
    demand = _load(item, PARTS["demand"], problems)
    reviewer = str(data.get("reviewer") or "").strip().casefold()
    for label, author in (("item.json implementer", item.meta.get("implementer")),
                          ("demand.json author", (demand or {}).get("author"))):
        if _text(author) and reviewer == author.strip().casefold():
            problems.append(f"Add a value reviewer different from the {label}.")
    answers = _object(data.get("answers"))
    for key in VALUE_ANSWERS:
        answer = _object(answers.get(key))
        if type(answer.get("ok")) is not bool:
            problems.append(f"Add value-review.answers.{key}.ok as true or false.")
        _required(answer, ("note",), f"value-review.answers.{key}", problems)
        if answer.get("ok") is False and data.get("verdict") == "build":
            problems.append(f"Add a propose or drop verdict because value-review.answers.{key}.ok is false.")
    if data.get("verdict") not in verdicts.VALUE_VERDICTS:
        problems.append("Add value-review.verdict as build, propose or drop.")
    return problems


def _bound(item, head, sha):
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,40}", value) for value in (head, sha)):
        return False
    if len(head) < len(sha):
        head, sha = sha, head
    try:
        return verdicts.bound(item, head.lower(), sha.lower())
    except (OSError, CommandError):
        return False


def _binding(item, data, name, problems):
    try:
        head = item.head_sha()
    except (OSError, CommandError, KeyError):
        head = ""
    if not _bound(item, head, data.get("head")):
        problems.append(f"Add {name}.head bound to the current item head (or an identical re-commit).")
    _pr_binding(item, data, name, problems)


def _pr_binding(item, data, name, problems):
    try:
        sha = verdicts.pr_text_sha256(item)
    except (OSError, ValueError):
        sha = None
    if not sha or data.get("pr_sha256") != sha:
        problems.append(f"Bind {name}.pr_sha256 to the current PR text with bin/factory review stamp --item DIR {name.removesuffix('-review')} --head SHA after reviewing it.")


def _complete(data, name, problems):
    if type(data.get("complete", True)) is not bool:
        problems.append(f"Add {name}.complete as true or false.")
    if data.get("complete") is False and data.get("verdict") == "ship":
        problems.append(f"Finish the incomplete {name} before choosing ship, or use fix-needed or drop.")


def _receipt(item, receipt_id, head, label, problems, require_clean=False):
    try:
        receipt = receipts.get(item, receipt_id) if _text(receipt_id) else None
    except (OSError, ValueError):
        receipt = None
    if not isinstance(receipt, dict) or not receipts.valid(receipt):
        problems.append(f"Add a valid receipt for {label} using factory run and cite its id.")
        return None
    if head is not None and not _bound(item, head, receipt.get("cwd_head")):
        problems.append(f"Add a receipt for {label} whose cwd_head matches the reviewed head (or an identical re-commit).")
        return None
    if (head is not None or require_clean) and not receipts.clean(receipt):
        problems.append(f"For {label}, commit or revert stray tracked changes and run again to capture a clean, unchanged head.")
        return None
    return receipt


def _list(data, key, name, problems):
    value = data.get(key)
    if not isinstance(value, list):
        problems.append(f"Add {name}.{key} as a list.")
        return []
    return value


def validate_review(item):
    problems = []
    data = _load(item, PARTS["review"], problems)
    if data is None:
        return problems
    _required(data, ("reviewer",), "review", problems)
    _binding(item, data, "review", problems)
    _complete(data, "review", problems)
    expected = {row["id"] for row in classes()}
    seen = set()
    for row in _list(data, "obligations", "review", problems):
        row = _object(row)
        class_id = row.get("class")
        if not isinstance(class_id, str) or class_id not in expected:
            problems.append("Add an obligation with a class id from factory review classes.")
            continue
        if class_id in seen:
            problems.append(f"Add exactly one review obligation for {class_id}, merging duplicate entries.")
        seen.add(class_id)
        _required(row, ("why",), f"review.obligations.{class_id}", problems)
        if type(row.get("applies")) is not bool:
            problems.append(f"Add review.obligations.{class_id}.applies as true or false.")
        ids = _list(row, "receipts", f"review.obligations.{class_id}", problems)
        if row.get("applies") is True and not ids:
            problems.append(f"Add at least one receipt for the applying {class_id} obligation.")
        for receipt_id in ids:
            _receipt(item, receipt_id, data.get("head") if row.get("applies") is True else None,
                     class_id, problems, require_clean=True)
    for class_id in sorted(expected - seen):
        problems.append(f"Add a review obligation for {class_id}, with applies, why and receipts.")
    for index, finding in enumerate(_list(data, "findings", "review", problems), 1):
        finding = _object(finding)
        label = f"review.findings[{index}]"
        _required(finding, ("where", "summary"), label, problems)
        severity = finding.get("severity")
        if severity not in ("blocking", "should-fix", "nit", "pre-existing"):
            problems.append(f"Add {label}.severity as blocking, should-fix, nit or pre-existing.")
        if severity == "blocking" or finding.get("receipt"):
            _receipt(item, finding.get("receipt"), None, label, problems)
        if data.get("verdict") == "ship":
            if severity == "blocking":
                problems.append("Add a fix-needed or drop verdict while a blocking finding remains.")
            elif severity == "should-fix" and not _text(finding.get("accepted")):
                problems.append(f"Add {label}.accepted with a reason, or use fix-needed instead of ship.")
    if data.get("verdict") not in verdicts.VERDICTS:
        problems.append("Add review.verdict as ship, fix-needed or drop.")
    return problems


def _argv(value):
    return isinstance(value, list) and bool(value) and all(_text(arg) for arg in value)


def _test_receipts(item, problems=None):
    problems = [] if problems is None else problems
    source = "redgreen.json cmd"
    try:
        command, expected_cwd = receipts.redgreen_command(item)
        head = item.head_sha()
    except (OSError, ValueError, CommandError) as error:
        problems.append(str(error))
        return [], source
    declared = item.meta.get("test_cmd")
    declared_cwd = item.meta.get("test_cwd", ".")
    if ((declared is not None and (not _text(declared) or receipts.scrub_home(declared.strip()) != receipts.scrub_home(command.strip())))
            or ((declared is not None or "test_cwd" in item.meta) and (not _text(declared_cwd)
                or receipts.command_cwd(item, declared_cwd) != expected_cwd))):
        problems.append("item.json test_cmd or test_cwd differs from redgreen.json; rerun bin/factory redgreen first, then capture and rerun that command.")
    try:
        available = [row for row in receipts.load(item) if isinstance(row, dict) and receipts.valid(row)]
    except (OSError, ValueError):
        available = []
    matches = [row for row in available if receipts.clean(row) and _bound(item, head, row.get("cwd_head"))
               and receipts.command_matches(row.get("argv"), command) and _text(row.get("cwd"))
               and receipts.command_cwd(item, row["cwd"]) == expected_cwd]
    return matches, source


def _review_ids(data):
    ids = []
    for obligation in data.get("obligations", []):
        ids.extend(obligation.get("receipts", []))
    ids.extend(finding["receipt"] for finding in data.get("findings", []) if finding.get("receipt"))
    return ids


def validate_coordinator(item):
    problems = []
    data = _load(item, PARTS["coordinator"], problems)
    if data is None:
        return problems
    _binding(item, data, "coordinator-review", problems)
    _complete(data, "coordinator-review", problems)
    for key in ("diff", "tests", "pr_text", "demand"):
        if _object(data.get("read")).get(key) is not True:
            problems.append(f"Add coordinator-review.read.{key}: true after reading it.")
    review_problems = validate_review(item)
    problems.extend(review_problems)
    review_data = _load(item, PARTS["review"], []) if not review_problems else None
    cited_ids = set(_review_ids(review_data)) if review_data else set()
    required = item.meta.get("required_checks")
    required = required if isinstance(required, list) else []
    originals = set()
    for receipt_id in _list(data, "reruns", "coordinator-review", problems):
        rerun = _receipt(item, receipt_id, data.get("head"), "coordinator rerun", problems)
        if rerun is None:
            continue
        original_id = rerun.get("rerun_of")
        if not _text(original_id):
            problems.append("Add a coordinator rerun receipt with rerun_of by using factory rerun.")
        else:
            original = _receipt(item, original_id, data.get("head"), "rerun original", problems)
            if original is not None:
                if original_id not in cited_ids and not any(matches_required(item, original, entry) for entry in required):
                    problems.append("Cite a rerun original from the review or required_checks, then use factory rerun on its id.")
                elif any(rerun.get(key) != original.get(key) for key in ("argv", "cwd", "exit", "argv_home")):
                    problems.append("Capture a rerun with the same argv, cwd and exit as its original using factory rerun.")
                else:
                    originals.add(original_id)
        match = _object(rerun.get("match")).get("exit")
        if match is not True:
            problems.append("Add a coordinator rerun with CLI-computed match.exit true.")
        if match is False and data.get("verdict") != "fix-needed":
            problems.append("Add coordinator-review.verdict: fix-needed because a rerun has match.exit false.")
    tests, command_source = _test_receipts(item, problems)
    if not any(row.get("id") in originals for row in tests):
        problems.append(f"Add a rerun of a receipt for the red-to-green test command from {command_source}.")
    if review_data:
        for row in review_data["obligations"]:
            if row["applies"] and not originals.intersection(row["receipts"]):
                problems.append(f"Add a coordinator rerun of a receipt for the applying {row['class']} obligation.")
    counterexample = _object(data.get("counterexample"))
    _required(counterexample, ("what",), "coordinator-review.counterexample", problems)
    receipt = _receipt(item, counterexample.get("receipt"), data.get("head"), "coordinator counterexample", problems)
    if receipt:
        if not _argv(receipt.get("argv")):
            problems.append("Add a coordinator counterexample receipt with a non-empty argv.")
        elif review_data:
            cited = [receipts.get(item, receipt_id) for receipt_id in _review_ids(review_data)]
            if any(row and row.get("argv") == receipt["argv"] for row in cited):
                problems.append("Add a coordinator counterexample whose argv differs from every receipt cited in review.json.")
    if data.get("verdict") not in verdicts.VERDICTS:
        problems.append("Add coordinator-review.verdict as ship, fix-needed or drop.")
    return problems


VALIDATORS = {"demand": validate_demand, "value": validate_value,
              "review": validate_review, "coordinator": validate_coordinator}


def problems(item, part):
    """List protocol repairs, including unreadable evidence, without aborting other items."""
    if part not in VALIDATORS:
        return ["Choose demand, value, review or coordinator for the review check."]
    try:
        return VALIDATORS[part](item)
    except (OSError, ValueError, CommandError, KeyError, TypeError) as error:
        return [f"Repair {PARTS[part]} or its referenced evidence and rerun the check: {error}"]


def _gate(item, rep, part, check_id, required_verdict=None):
    errors = problems(item, part)
    if required_verdict:
        data = _load(item, PARTS[part], []) or {}
        if data.get("verdict") != required_verdict:
            errors.append(f"Add a {part} verdict of {required_verdict} before passing this gate.")
    rep.add(check_id, not errors, " ".join(errors[:3]) or f"{part} protocol checks passed.",
            f"bin/factory review check --item {shlex.quote(str(item.dir))} {part}")


@check("demand-evidence", "demand evidence", lessons=("L-061", "L-065", "L-066"), schema_min=2)
def demand_evidence(item, rep):
    _gate(item, rep, "demand", "demand-evidence")


@check("value-review", "value review", lessons=("L-061", "L-065", "L-066"), schema_min=2)
def value_review(item, rep):
    _gate(item, rep, "value", "value-review", "build")


@check("review-obligations", "review obligations", lessons=("L-015", "L-041", "L-048", "L-058", "L-072", "L-074"),
       modes=("review", "release"), schema_min=2)
def review_obligations(item, rep):
    _gate(item, rep, "review", "review-obligations", "ship")


@check("coordinator-proof", "coordinator proof", modes=("release",), schema_min=2)
def coordinator_proof(item, rep):
    _gate(item, rep, "coordinator", "coordinator-proof", "ship")


def _round_files(item):
    return sorted(path for pattern in ("review-round*.md", "review-round*.json")
                  for path in item.dir.glob(pattern) if path.is_file())


def _review_record(path):
    try:
        if path.suffix == ".json":
            data = _object(read_json(path))
        else:
            text = path.read_text()
            result = verdicts.VERDICT.search(text)
            head = verdicts.REVIEWED_SHA.search(text)
            data = {"verdict": result[1].lower() if result else None, "head": head[1] if head else None}
        return {**data, "finished_at": path.stat().st_mtime}
    except (OSError, ValueError):
        return {}


def _round_records(item):
    grouped = {}
    for path in _round_files(item):
        match = re.match(r"review-round(\d+)", path.name)
        if match and (verdicts.schema(item) >= 2 or path.suffix == ".md"):
            number = int(match[1])
            if number not in grouped or path.suffix == ".json":
                grouped[number] = path
    return [_review_record(grouped[number]) for number in sorted(grouped)]


def _current_review(item):
    try:
        result = verdicts.read_review(item)
        return {**_review_record(item.file(result["source"])), **result} if result else {}
    except (OSError, ValueError):
        return {}


def rounds(item):
    count = 0
    for result in _round_records(item):
        if result.get("verdict") in ("ship", "drop") and result.get("complete", True) is True:
            count = 0
        elif result.get("verdict") == "fix-needed":
            count += 1
    current = _current_review(item)
    if current.get("verdict") in ("ship", "drop") and current.get("complete", True) is True:
        return 0
    return count


def record_round(item):
    """Archive the current review before a fix starts, without overwriting an earlier round."""
    source = item.file("review.json" if verdicts.schema(item) >= 2 else "review.md")
    if not source.is_file():
        raise SystemExit(f"Add {source.name} before recording a fix round.")
    numbers = [int(match[1]) for path in _round_files(item)
               if (match := re.match(r"review-round(\d+)", path.name))]
    number = max([rounds(item), *numbers], default=0) + 1
    target = item.file(f"review-round{number}{source.suffix}")
    source.rename(target)
    failed_rounds = sum(result.get("verdict") == "fix-needed" for result in _round_records(item))
    timeline.mark(item, "review-round-recorded", head=item.head_sha(), failed_rounds=failed_rounds, round=number)
    return target


def _review_start(item):
    """First start in the current timeline cycle; rereading a brief is not a new attempt."""
    events = timeline.events(item)
    if not any(isinstance(row, dict) and row.get("event") == "review-started" and _text(row.get("head"))
               for row in events):
        return None
    head = item.head_sha()
    cycle, start = (head, 0), None
    for row in events:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if event == "review-started" and not _text(row.get("head")):
            continue
        try:
            at = parse_iso(row["at"]).timestamp()
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if event == "review-round-recorded":
            cycle = (row.get("head", head), row.get("failed_rounds", cycle[1]))
            start = None
        elif event == "takeover-decided" and row.get("head") == head and _text(row.get("decision")):
            cycle, start = (head, cycle[1]), None
        elif event == "review-started":
            identity = (row["head"], row.get("failed_rounds", cycle[1]))
            if identity != cycle:
                cycle, start = identity, None
            if start is None:
                start = at
    return start if cycle[0] == head else None


def _review_duration(item, result):
    start = _review_start(item)
    if start is None:
        return None
    end = time.time()
    if (result.get("verdict") in verdicts.VERDICTS and result.get("complete", True) is True
            and result.get("finished_at", 0) >= start and _bound(item, item.head_sha(), result.get("head"))):
        end = result["finished_at"]
    return end - start


def needs_takeover(item):
    current = _current_review(item)
    archives = _round_records(item)
    result = current or (archives[-1] if archives else {})
    if result.get("complete") is False:
        return True
    if verdicts.schema(item) >= 2:
        coordinator = _review_record(item.file(PARTS["coordinator"]))
        if (coordinator.get("complete") is False
                and coordinator.get("finished_at", 0) >= result.get("finished_at", 0)):
            return True
    if rounds(item) >= 1 and current.get("verdict") == "fix-needed":
        return True
    try:
        duration = _review_duration(item, result)
    except (OSError, ValueError, CommandError, KeyError):
        return True
    if duration is None:
        return False
    try:
        budget = float(item.meta.get("review_budget_minutes", 120))
        if not math.isfinite(budget) or budget <= 0:
            budget = 120
    except (TypeError, ValueError):
        budget = 120
    return duration > budget * 60


def render(item):
    """Write one-way Markdown views; schema-2 decisions remain in JSON."""
    if verdicts.schema(item) < 2:
        raise SystemExit("Keep the legacy Markdown reviews; review render requires a schema-2 item.")
    written = []
    for part in ("review", "coordinator", "value"):
        name = PARTS[part]
        if not item.file(name).exists():
            continue
        problems = []
        data = _load(item, name, problems)
        if problems:
            raise SystemExit(" ".join(problems))
        title = {"review": "Independent review", "coordinator": "Coordinator review", "value": "Value review"}[part]
        lines = [f"# {title}", "", f"Source: `{name}`. Edit the JSON, then render again.", ""]
        fields = ("reviewer", "demand_sha256", "complete", "verdict") if part == "value" else ("reviewer", "head", "pr_sha256", "complete", "verdict")
        for key in fields:
            if key in data:
                lines.extend([f"{key}: {data[key]}", ""])
        if part == "value":
            for key, answer in _object(data.get("answers")).items():
                answer = _object(answer)
                lines.extend([f"## {key.replace('_', ' ')}", "", f"Answer: {answer.get('ok')}", "", str(answer.get("note", "")), ""])
        elif part == "review":
            for row in data.get("obligations", []):
                lines.extend([f"## {row.get('class', '')}", "", f"Applies: {row.get('applies')}", "", str(row.get("why", "")), "",
                              "Receipts: " + ", ".join(row.get("receipts", [])), ""])
            lines.extend(["## Findings", ""])
            for finding in data.get("findings", []):
                lines.extend([f"- {finding.get('severity')}: {finding.get('where')} — {finding.get('summary')}",
                              f"  Receipt: {finding.get('receipt', '')}; accepted: {finding.get('accepted', '')}", ""])
        else:
            lines.extend(["## Read", ""])
            lines.extend(f"- {key}: {value}" for key, value in _object(data.get("read")).items())
            lines.extend(["", "## Reruns", "", *[f"- {rid}" for rid in data.get("reruns", [])], "",
                          "Red-to-green command: " + _test_receipts(item)[1], "", "## Counterexample", ""])
            example = _object(data.get("counterexample"))
            lines.extend([str(example.get("what", "")), "", f"Receipt: {example.get('receipt', '')}", ""])
        target = item.file(name.replace(".json", ".md"))
        target.write_text("\n".join(lines))
        written.append(target)
    if not written:
        raise SystemExit("Add review.json, coordinator-review.json or value-review.json before rendering.")
    return written


def cmd_check(args):
    item = Item(args.item)
    if verdicts.schema(item) < 2:
        print("Legacy item: keep using the existing Markdown review gate.")
        return
    parts = (args.part,) if args.part else PARTS
    errors = [problem for part in parts for problem in problems(item, part)]
    for problem in errors:
        print(problem)
    if "coordinator" in parts and not errors:
        print("Red-to-green command: " + _test_receipts(item)[1])
    if errors:
        raise SystemExit(1)


def cmd_classes(args):
    print(json.dumps(classes(), indent=2, ensure_ascii=False))


def cmd_render(args):
    for path in render(Item(args.item)):
        print(path)


def cmd_round(args):
    print(record_round(Item(args.item)))


def cmd_takeover(args):
    item = Item(args.item)
    if args.head != item.head_sha():
        raise SystemExit("Use the item's exact current head for --head after reviewing the takeover decision.")
    errors = []
    data = _load(item, "takeover.json", errors)
    if data is not None:
        if data.get("decision") not in ("repair", "park"):
            errors.append("Set takeover.json decision to repair or park.")
        _required(data, ("reason",), "takeover", errors)
        if data.get("decision") == "park":
            _required(data, ("reopen_when",), "takeover", errors)
    if errors:
        raise SystemExit(" ".join(errors))
    data["head"] = args.head
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    events = timeline.events(item)
    write_json(item.file("takeover.json"), data)
    if not any(isinstance(row, dict) and row.get("event") == "takeover-decided"
               and row.get("head") == args.head and row.get("decision_sha256") == digest for row in events):
        timeline.mark(item, "takeover-decided", head=args.head, decision=data["decision"], decision_sha256=digest)
    print(item.file("takeover.json"))


def _finished_review(item, path, args):
    """Times of a review that already finished at this head, so rebinding it to new PR text keeps its finish time."""
    record = _review_record(path)
    if (record.get("verdict") in verdicts.VERDICTS and record.get("complete", True) is True
            and _bound(item, args.head, record.get("head"))):
        try:
            status = path.stat()
        except OSError:
            return None
        return status.st_atime_ns, status.st_mtime_ns
    return None


def cmd_stamp(args):
    item = Item(args.item)
    legacy = verdicts.schema(item) < 2
    if legacy and args.part == "value":
        raise SystemExit("legacy items have no value review; keep their Markdown protocol and review Who hits it/Impact in verify.md against the original sources.")
    if args.part != "value" and not args.head:
        raise SystemExit("Pass --head with the item's exact current head after reading its diff and PR text.")
    if args.head is not None and args.head != item.head_sha():
        raise SystemExit("Use the item's exact current head for --head after reading its diff and PR text.")
    name = PARTS[args.part].replace(".json", ".md") if legacy else PARTS[args.part]
    path = item.file(name)
    finished = _finished_review(item, path, args) if args.part == "review" else None
    try:
        if legacy:
            with path.open("r+", encoding="utf-8", newline="") as stream:
                text = stream.read()
                binding = re.compile(r"^[ \t]*head[ \t]*:[^\r\n]*", re.I | re.M)
                line = f"head: {args.head}"
                if binding.search(text):
                    text = binding.sub(line, text, count=1)
                else:
                    newline = "\r\n" if "\r\n" in text else "\n"
                    text = line + newline + text
                stream.seek(0)
                stream.write(text)
                stream.truncate()
        else:
            errors = []
            data = _load(item, name, errors)
            if errors:
                raise SystemExit(" ".join(errors))
            if args.part == "value":
                sha = verdicts.demand_sha256(item)
                if not sha:
                    raise SystemExit("Write and review demand.json before stamping the value review.")
                data.pop("head", None)
                data.pop("pr_sha256", None)
                data["demand_sha256"] = sha
            else:
                sha = verdicts.pr_text_sha256(item)
                if not sha:
                    raise SystemExit("Write and read pr.md before stamping its review.")
                data.update(head=args.head, pr_sha256=sha)
            write_json(path, data)
        if finished is not None:
            os.utime(path, ns=finished)
    except (OSError, UnicodeDecodeError) as error:
        raise SystemExit(f"Cannot stamp {name}: {error}; write a readable review file first, then rerun stamp.") from None
    print(path)


def register(sub):
    parser = sub.add_parser("review", help="validate and render evidence-backed reviews")
    actions = parser.add_subparsers(dest="review_command", required=True)
    checker = actions.add_parser("check", help="list missing review evidence")
    checker.add_argument("--item", required=True)
    checker.add_argument("part", nargs="?", choices=tuple(PARTS))
    checker.set_defaults(fn=cmd_check)
    listing = actions.add_parser("classes", help="print risk classes and how to probe them")
    listing.set_defaults(fn=cmd_classes)
    stamp = actions.add_parser("stamp", help="bind value review to demand; bind code reviews to the current head and, on schema 2, PR text")
    stamp.add_argument("--item", required=True)
    stamp.add_argument("part", choices=("value", "review", "coordinator"))
    stamp.add_argument("--head", help="exact current head; required for review and coordinator")
    stamp.set_defaults(fn=cmd_stamp)
    takeover = actions.add_parser("takeover", help="bind takeover.json and record its decision once per head and content")
    takeover.add_argument("--item", required=True)
    takeover.add_argument("--head", required=True, help="exact current head covered by the decision")
    takeover.set_defaults(fn=cmd_takeover)
    for name, function in (("render", cmd_render), ("round", cmd_round)):
        action = actions.add_parser(name)
        action.add_argument("--item", required=True)
        action.set_defaults(fn=function)
