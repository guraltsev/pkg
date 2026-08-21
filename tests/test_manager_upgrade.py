"""Cover observable manager batch planning and failure continuation.

The tests use lightweight target records and real ``ActionResult`` values;
filesystem discovery, provider checks, elevation, and package installation are
outside this unit boundary.
"""

from types import SimpleNamespace

from gupkg.core import ActionResult, Scope
from gupkg.manager import execute_upgrade_plan, plan_upgrade_all


def _target(target_id: str, scope: Scope, *, installation="installed", health="healthy"):
    """Build a target record with the observable manager status fields."""
    selector = target_id.split(":", 1)[1]
    return SimpleNamespace(
        target_id=target_id,
        scope=scope,
        package=SimpleNamespace(selector=selector),
        installation_status=installation,
        health_status=health,
        update_status="unchecked",
    )


def _inventory(targets):
    """Build a complete two-scope inventory for planner behavior tests."""
    return SimpleNamespace(
        targets=targets,
        scopes=[
            SimpleNamespace(scope=Scope.USER, complete=True),
            SimpleNamespace(scope=Scope.MACHINE, complete=True),
        ],
    )


def test_upgrade_planner_skips_ineligible_targets_with_distinct_reasons() -> None:
    """Every ineligible installation state receives its documented skip reason."""
    targets = [
        _target("user:broken", Scope.USER, installation="broken"),
        _target("user:missing", Scope.USER, installation="not-installed"),
        _target("user:sick", Scope.USER, health="unhealthy"),
    ]

    plan = plan_upgrade_all(_inventory(targets), {Scope.USER}, lambda target: None)

    assert [(entry.target.target_id, entry.reason) for entry in plan.entries] == [
        ("user:broken", "broken"),
        ("user:missing", "uninstalled"),
        ("user:sick", "unhealthy"),
    ]


def test_upgrade_executor_continues_in_order_and_marks_fail_fast_work() -> None:
    """Batch execution preserves order and reports later work as not attempted."""
    targets = [_target("user:a", Scope.USER), _target("user:b", Scope.USER)]
    inventory = _inventory(targets)

    def check(target):
        target.update_status = "available"
        return ActionResult(True)

    plan = plan_upgrade_all(inventory, {Scope.USER}, check)
    calls = []

    def upgrade(target):
        calls.append(target.target_id)
        return ActionResult(False, errors=["provider failed"], exit_code=3)

    execute_upgrade_plan(plan, lambda target: None, upgrade, fail_fast=True)

    assert calls == ["user:a"]
    assert [entry.outcome for entry in plan.entries] == ["failed", "not-attempted"]
    assert plan.entries[1].reason == "fail-fast"


def test_upgrade_executor_cancellation_stops_before_the_next_package() -> None:
    """Cancellation prevents another package operation at a safe boundary."""
    targets = [_target("user:a", Scope.USER), _target("user:b", Scope.USER)]
    plan = plan_upgrade_all(
        _inventory(targets),
        {Scope.USER},
        lambda target: (setattr(target, "update_status", "available") or ActionResult(True)),
    )
    calls = []

    def upgrade(target):
        calls.append(target.target_id)
        return ActionResult(True, changed=True)

    cancelled = False

    def cancel_requested():
        return cancelled

    def first_upgrade(target):
        nonlocal cancelled
        cancelled = True
        return upgrade(target)

    execute_upgrade_plan(plan, lambda target: None, first_upgrade, cancel_requested=cancel_requested)

    assert calls == ["user:a"]
    assert plan.entries[1].outcome == "not-attempted"
    assert plan.entries[1].reason == "cancelled"
