"""Select and run a Frontier Wave with cross-run Touches reservations."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import signal
from functools import partial
from pathlib import Path
from typing import Any, Iterator

from . import pipeline
from .brief import classify_blockers, is_ready_for_frontier, parse_touches, touches_overlap
from .config import _cache_root
from .git_ops import canonical_repo_identity
from .harness import HarnessSpec, default_reviewer_for, parse_harness_spec
from .linear import LinearClient
from .process import check_shutdown, is_shutdown_requested, kill_all_active_children, log, request_shutdown, sha256_text
from .secrets import redact_secrets


def collect_runnable_tickets(
    client: LinearClient,
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    runnable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for iss in issues:
        ok, reason = is_ready_for_frontier(iss)
        if not ok:
            skipped.append({"id": iss["identifier"], "title": iss["title"], "reason": reason})
            continue
        inv_rels = client.get_issue_inverse_relations(iss["id"])
        iss["inverseRelations"] = {"nodes": inv_rels}
        active_blks, canceled_blks = classify_blockers(iss)
        if active_blks or canceled_blks:
            blocked.append(
                {
                    "id": iss["identifier"],
                    "title": iss["title"],
                    "active_blockers": active_blks,
                    "canceled_blockers": canceled_blks,
                }
            )
        else:
            runnable.append(iss)
    runnable.sort(key=lambda x: (x.get("priority") or 99, x["identifier"]))
    return runnable, blocked, skipped


def extract_active_touches(started_issues: list[dict[str, Any]]) -> tuple[set[str], bool]:
    """Collect Touches currently held by in-flight (In Progress / In Review) sibling tickets for cross-wave serialization."""
    active_touches: set[str] = set()
    active_unscoped = False
    for iss in started_issues:
        if (iss.get("children") or {}).get("nodes"):
            continue
        t = parse_touches(iss.get("description") or "")
        if t:
            active_touches |= t
        else:
            lbls = {l["name"].lower() for l in (iss.get("labels") or {}).get("nodes", [])}
            if "agent-ready" in lbls:
                active_unscoped = True
    return active_touches, active_unscoped


def select_non_overlapping_wave(
    runnable: list[dict[str, Any]],
    *,
    limit: int | None = None,
    active_claimed_touches: set[str] | None = None,
    active_has_unscoped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Pick ONE conflict-free wave to run against current base branch, also respecting Touches already held by
    in-flight (In Progress / In Review) sibling tickets across waves!
    """
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    claimed_touches: set[str] = set(active_claimed_touches or set())
    has_unscoped = bool(active_has_unscoped)

    for iss in runnable:
        if limit is not None and len(selected) >= limit:
            deferred.append({"id": iss["identifier"], "reason": "limit_reached"})
            continue
        t = parse_touches(iss.get("description") or "")
        if not t:
            if not selected and not claimed_touches and not has_unscoped:
                selected.append(iss)
                has_unscoped = True
            else:
                deferred.append({"id": iss["identifier"], "reason": "undeclared_touches_deferred"})
        else:
            conflicts = touches_overlap(t, claimed_touches)
            if has_unscoped or conflicts:
                deferred.append({"id": iss["identifier"], "reason": f"overlapping_touches:{conflicts or ['unscoped']}"})
            else:
                selected.append(iss)
                claimed_touches |= t
    return selected, deferred


@contextlib.contextmanager
def reserve_touches(repo_identity: str, touches: set[str]) -> Iterator[bool]:
    """Hold shared ancestors and exclusive resources for the lifetime of a batch ticket."""
    lock_root = _cache_root() / "locks" / f"touches-{repo_identity}"
    lock_root.mkdir(parents=True, exist_ok=True)
    modes = {"": fcntl.LOCK_SH if touches else fcntl.LOCK_EX}
    for resource in touches:
        parts = resource.split("/")
        for depth in range(1, len(parts)):
            modes.setdefault("/".join(parts[:depth]), fcntl.LOCK_SH)
        modes[resource] = fcntl.LOCK_EX
    with contextlib.ExitStack() as locks:
        for resource, mode in sorted(modes.items()):
            fh = locks.enter_context((lock_root / sha256_text(resource)).open("a"))
            try:
                fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def run_batch_safely(
    client: LinearClient,
    repo_root: Path,
    repo_project: dict[str, Any] | None,
    runnable: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    active_claimed_touches: set[str] | None = None,
    active_has_unscoped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    worker = parse_harness_spec(args.worker)
    reviewer = parse_harness_spec(args.reviewer) if args.reviewer else default_reviewer_for(worker)
    if worker.vendor == reviewer.vendor:
        log(f"Warning: worker ({worker.label}) and reviewer ({reviewer.label}) share vendor '{worker.vendor}'.")

    wave, deferred = select_non_overlapping_wave(
        runnable,
        limit=getattr(args, "limit", None),
        active_claimed_touches=active_claimed_touches,
        active_has_unscoped=active_has_unscoped,
    )
    repo_identity = canonical_repo_identity(repo_root)
    run_ticket = partial(_run_wave_ticket, client, repo_root, repo_project, args, repo_identity, worker, reviewer)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_parallel))
    try:
        futures = {pool.submit(run_ticket, issue) for issue in wave}
        results = _collect_wave_results(futures, deferred)
    except (KeyboardInterrupt, SystemExit):
        request_shutdown(signal.SIGTERM)
        kill_all_active_children(wait_sigkill=True)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return results, deferred


def _run_wave_ticket(
    client: LinearClient,
    repo_root: Path,
    repo_project: dict[str, Any] | None,
    args: argparse.Namespace,
    repo_identity: str,
    worker: HarnessSpec,
    reviewer: HarnessSpec,
    issue: dict[str, Any],
) -> dict[str, Any]:
    identifier = issue["identifier"]
    touches = parse_touches(issue.get("description") or "")
    if is_shutdown_requested():
        return {"issue": identifier, "status": "cancelled_by_signal"}
    try:
        with reserve_touches(repo_identity, touches) as reserved:
            if not reserved:
                return {"issue": identifier, "status": "deferred", "reason": "overlapping_touches_in_another_batch"}
            # The preceding wave can finish between selection and taking the local reservation.
            project_id = (issue.get("project") or repo_project or {}).get("id")
            if not project_id:
                raise RuntimeError("Batch ticket has no Linear project for Touches reconciliation.")
            started = client.list_project_issues(project_id, state_type="started")
            active, unscoped = extract_active_touches(started)
            if unscoped or (active and not touches) or touches_overlap(touches, active):
                return {"issue": identifier, "status": "deferred", "reason": "overlapping_active_touches"}
            return pipeline.execute_ticket(
                client,
                repo_root,
                repo_project,
                identifier,
                worker,
                reviewer,
                verify_cmd_override=args.verify_cmd,
                setup_cmd_override=args.setup_cmd,
                max_rework=args.max_rework,
                verify_timeout=args.verify_timeout,
                setup_timeout=args.setup_timeout,
                worker_timeout=args.worker_timeout,
                review_timeout=args.review_timeout,
                no_push=args.no_push,
                allow_no_verify=args.allow_no_verify,
                allow_gate_edits=args.allow_gate_edits,
                allow_test_edits=args.allow_test_edits,
                allow_dirty_docs=args.allow_dirty_docs,
                allow_branch_only=args.allow_branch_only,
                skip_baseline=args.skip_baseline,
                skip_project_check=args.skip_project_check,
                force_rerun=args.force_rerun,
                fresh=args.fresh,
                auto_claimed=True,
                reserved_touches=touches,
            )
    except (KeyboardInterrupt, SystemExit):
        return {"issue": identifier, "status": "cancelled_by_signal"}
    except Exception as exc:
        return {"issue": identifier, "status": "error", "error": redact_secrets(str(exc))}


def _collect_wave_results(
    pending: set[concurrent.futures.Future[dict[str, Any]]],
    deferred: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cancelled_for_shutdown = False
    while pending:
        done, pending = concurrent.futures.wait(
            pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
        )
        for fut in done:
            result = fut.result()
            if result["status"] == "deferred":
                deferred.append({"id": result["issue"], "reason": result["reason"]})
            else:
                results.append(result)
        if is_shutdown_requested() and not cancelled_for_shutdown:
            cancelled_for_shutdown = True
            for fut in list(pending):
                if fut.cancel():
                    pending.discard(fut)
            kill_all_active_children(wait_sigkill=False)
    if is_shutdown_requested():
        check_shutdown()
    return results
