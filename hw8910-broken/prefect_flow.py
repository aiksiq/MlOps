"""Prefect orchestration with a content-addressed, restart-safe preparation cache."""
import argparse
from pathlib import Path

from prefect import flow, task

import lab
import release


def _preparation_cache_key(context, parameters):
    root = Path(parameters["root"])
    config = lab.read(root / "config.json")
    return release.cache_key(root / "input.txt", config)


@task(name="prepare-data", persist_result=True, cache_key_fn=_preparation_cache_key)
def prepare_task(root: str) -> dict:
    return lab.prepare(Path(root))


@task(name="validate-and-publish", retries=0)
def publish_task(root: str, report_name: str, prepared: dict) -> bool:
    return lab.publish_release(Path(root), report_name, prepared)


@flow(name="release-workflow")
def release_flow(
    workspace: str = ".lab",
    report_name: str = "report-B.json",
    fail_after_prepare: bool = False,
) -> bool:
    prepared = prepare_task(workspace)
    if fail_after_prepare:
        raise RuntimeError("Injected failure after preparation")
    return publish_task(workspace, report_name, prepared)


@task(name="verify-hw67-evaluation", retries=0)
def verify_hw67_task(hw67_root: str, workspace: str) -> dict:
    from hw67_integration import build_release

    manifest, report, passed = build_release(Path(hw67_root), Path(workspace))
    return {"manifest": manifest, "report": report, "passed": passed}


@task(name="publish-hw67-release", retries=0)
def publish_hw67_task(workspace: str, result: dict) -> str:
    if result["passed"] is not True:
        raise ValueError(result["report"]["evaluation"]["failure"])
    root = Path(workspace)
    release.publish(result["manifest"], result["report"], root / "accepted.json")
    lab._record_publication(root, result["manifest"], result["report"])
    return result["manifest"]["model_id"]


@flow(name="hw67-verified-model-release")
def hw67_release_flow(hw67_root: str = "../hw67-broken", workspace: str = ".release") -> str:
    result = verify_hw67_task(hw67_root, workspace)
    return publish_hw67_task(workspace, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".lab"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--report", default="report-B.json")
    run_parser.add_argument("--fail-after-prepare", action="store_true")
    hw67_parser = subparsers.add_parser("hw67")
    hw67_parser.add_argument("--hw67-root", type=Path, default=Path("../hw67-broken"))
    args = parser.parse_args()
    if args.command == "hw67":
        hw67_release_flow(str(args.hw67_root), str(args.workspace))
    else:
        release_flow(str(args.workspace), args.report, args.fail_after_prepare)


if __name__ == "__main__":
    main()
