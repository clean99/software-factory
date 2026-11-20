"""Run unittest discovery suites in separate parallel processes."""
import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def discovery_suites(directory):
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory))
    if loader.errors:
        raise ValueError("\n".join(loader.errors))
    return list(suite)


def discover(directory):
    groups = {}
    for index, suite in enumerate(discovery_suites(directory)):
        ids = list(test_ids(suite))
        if ids:
            groups[str(index)] = ids
    if not groups:
        raise ValueError("No tests discovered")
    return groups


def run_module(index, ids, root):
    started = time.monotonic()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(root / "tests"), str(root), environment.get("PYTHONPATH"))))
    result = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), "--worker", index],
                            cwd=root, env=environment, input=json.dumps(ids), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    return {"module": ids[0].rsplit(".", 2)[0], "tests": len(ids), "test_ids": ids,
            "seconds": time.monotonic() - started, "exit_code": result.returncode}, result.stdout


def run_worker(index, directory, expected_ids):
    # Rediscover rather than reload IDs: load_tests may configure cases or return custom suites.
    suites = discovery_suites(directory)
    if not 0 <= index < len(suites) or list(test_ids(suites[index])) != expected_ids:
        raise ValueError("Worker discovery differs from the scheduled suite")
    result = unittest.TextTestRunner(durations=5).run(suites[index])
    return 0 if result.wasSuccessful() else 1


def run(groups, root, jobs, stream):
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = [pool.submit(run_module, index, ids, root) for index, ids in groups.items()]
        for future in concurrent.futures.as_completed(pending):
            record, output = future.result()
            records.append(record)
            print(f"\n=== {record['module']} ({record['seconds']:.2f}s, exit={record['exit_code']}) ===",
                  file=stream, flush=True)
            print(output, end="", file=stream, flush=True)
    return sorted(records, key=lambda record: record["module"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--json", type=pathlib.Path, help="write module timings and exit codes")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    started = time.monotonic()
    root = pathlib.Path.cwd() if args.worker is not None else ROOT
    sys.path.insert(0, str(root))
    try:
        if args.worker is not None:
            return run_worker(args.worker, root / "tests", json.load(sys.stdin))
        groups = discover(root / "tests")
    except (ImportError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    records = run(groups, root, args.jobs, sys.stdout)
    report = {"success": all(row["exit_code"] == 0 for row in records),
              "tests": sum(row["tests"] for row in records), "modules": records,
              "wall_seconds": time.monotonic() - started, "jobs": args.jobs}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n{'OK' if report['success'] else 'FAILED'}: {report['tests']} tests, "
          f"{len(records)} modules, {report['wall_seconds']:.2f}s", flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
