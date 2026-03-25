#!/usr/bin/env python3
"""
Workload-Aware Rework Budget Governor CLI

A coordination utility that prevents rework loops from consuming the farm
by enforcing workload-aware rework budgets per task/class of tasks.

Commands:
    check     Check if a rework can proceed given metrics
    stats     View current usage statistics
    simulate  Simulate a rework decision scenario
    reset     Reset all budget tracking
    status    Show governor and workload status

Examples:
    # Check a rework decision with current workload
    rework-gov check --task-id task-123 --task-type tool_execution \\
        --failure signature=contract_lint_fail --proposed-action=retry \\
        --queue-length 50 --latency 2000

    # Simulate with JSON workload metrics file
    rework-gov simulate --scenario scenarios/retry_with_high_load.json

    # View statistics
    rework-gov stats --format json

    # Reset all budgets
    rework-gov reset
"""

import argparse
import json
import sys
import time
from typing import Any, Dict

from basic_rework_policy import BasicReworkPolicy
from rework_governor import ReworkGovernor
from rework_budget_types import FarmWorkloadMetrics, ReworkDecision


def cmd_check(args: argparse.Namespace) -> int:
    """Check if a rework can proceed."""
    policy = BasicReworkPolicy(
        max_rework_attempts=args.max_attempts or 3,
        cooldown_period_seconds=args.cooldown or 300,
        workload_threshold_queue_length=args.queue_threshold or 100,
        workload_threshold_latency_ms=args.latency_threshold or 5000.0,
    )
    governor = ReworkGovernor(policy)

    # Build workload metrics
    metrics = FarmWorkloadMetrics(
        queue_length=args.queue_length,
        avg_tool_call_latency_ms=args.latency_ms,
        active_agent_count=args.active_agents or 0,
    )

    if args.tool_concurrency:
        metrics.tool_concurrency = dict(args.tool_concurrency)

    # Load failure details if provided
    failure_details = None
    if args.failure_details:
        try:
            with open(args.failure_details, 'r') as f:
                failure_details = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading failure details: {e}", file=sys.stderr)
            return 1

    # Make decision
    decision = governor.check_rework(
        task_id=args.task_id,
        task_type=args.task_type,
        failure_signature=args.failure_signature,
        failure_details=failure_details,
        proposed_action=args.proposed_action,
        workload_metrics=metrics,
    )

    # Output decision
    output = {
        "task_id": args.task_id,
        "task_type": args.task_type,
        "failure_signature": args.failure_signature,
        "proposed_action": args.proposed_action,
        "decision": decision.to_dict(),
        "workload_metrics": {
            "queue_length": metrics.queue_length,
            "avg_tool_call_latency_ms": metrics.avg_tool_call_latency_ms,
            "active_agent_count": metrics.active_agent_count,
        },
    }

    if args.format == "json":
        print(json.dumps(output, indent=2))
    else:
        print(f"\nRework Decision")
        print("=" * 60)
        print(f"Task: {args.task_id} (type: {args.task_type})")
        print(f"Failure: {args.failure_signature}")
        print(f"Action: {args.proposed_action}")
        print(f"\nWorkload:")
        print(f"  Queue length: {metrics.queue_length}")
        print(f"  Avg latency: {metrics.avg_tool_call_latency_ms:.0f}ms")
        print(f"\nDecision: {decision.action.value.upper()}")
        print(f"Reason: {decision.reason}")
        if decision.suggested_alternative:
            print(f"Alternative: {decision.suggested_alternative}")
        if decision.adjusted_throttle_rate is not None:
            print(f"Throttle rate: {decision.adjusted_throttle_rate * 100:.0f}%")

    return 0 if decision.action.value != "short_circuit" else 1


def cmd_stats(args: argparse.Namespace) -> int:
    """View current statistics."""
    policy = BasicReworkPolicy(
        max_rework_attempts=args.max_attempts or 3,
        workload_threshold_queue_length=args.queue_threshold or 100,
    )
    governor = ReworkGovernor(policy)
    stats = governor.get_stats()

    if args.format == "json":
        print(json.dumps(stats, indent=2, default=str))
    else:
        print("\nRework Governor Statistics")
        print("=" * 60)
        for key, value in stats.items():
            if key == "task_type_failures":
                print(f"\n{key}:")
                for task_type, failures in value.items():
                    print(f"  {task_type}:")
                    for sig, count in failures.items():
                        print(f"    {sig}: {count}")
            elif isinstance(value, float):
                print(f"{key:25}: {value:.2f}")
            else:
                print(f"{key:25}: {value}")

    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Simulate a rework decision scenario."""
    if args.scenario:
        try:
            with open(args.scenario, 'r') as f:
                scenario = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading scenario file: {e}", file=sys.stderr)
            return 1
    else:
        # Build scenario from command-line args
        scenario = {
            "policy": {
                "max_rework_attempts": args.max_attempts or 3,
                "cooldown_period_seconds": args.cooldown or 300,
                "workload_threshold_queue_length": args.queue_threshold or 100,
                "workload_threshold_latency_ms": args.latency_threshold or 5000.0,
            },
            "task": {
                "task_id": args.task_id or "demo-task-1",
                "task_type": args.task_type or "tool_execution",
                "failure_signature": args.failure_signature or "contract_lint_fail",
                "proposed_action": args.proposed_action or "retry",
                "existing_attempts": args.existing_attempts or 0,
            },
            "workload": {
                "queue_length": args.queue_length or 0,
                "avg_tool_call_latency_ms": args.latency_ms or 0.0,
                "active_agent_count": args.active_agents or 0,
                "tool_concurrency": dict(args.tool_concurrency) if args.tool_concurrency else {},
            },
        }

    # Create policy and governor with scenario config
    policy_config = scenario.get("policy", {})
    policy = BasicReworkPolicy(**policy_config)
    governor = ReworkGovernor(policy)

    # Simulate prior attempts if specified
    task_data = scenario.get("task", {})
    task_id = task_data.get("task_id", "task-1")
    task_type = task_data.get("task_type", "tool_execution")

    # Set existing attempts on the budget directly
    budget = policy.get_budget(task_id, task_type)
    budget.rework_attempts_used = task_data.get("existing_attempts", 0)
    if budget.rework_attempts_used > 0:
        budget.first_rework_attempt_at = time.time() - 3600  # 1 hour ago
        budget.last_rework_attempt_at = time.time() - 600    # 10 minutes ago

    # Build workload metrics
    workload_data = scenario.get("workload", {})
    metrics = FarmWorkloadMetrics(
        queue_length=workload_data.get("queue_length", 0),
        avg_tool_call_latency_ms=workload_data.get("avg_tool_call_latency_ms", 0.0),
        active_agent_count=workload_data.get("active_agent_count", 0),
        tool_concurrency=workload_data.get("tool_concurrency", {}),
    )

    # Make decision
    decision = governor.check_rework(
        task_id=task_id,
        task_type=task_type,
        failure_signature=task_data.get("failure_signature", "generic_failure"),
        failure_details=task_data.get("failure_details"),
        proposed_action=task_data.get("proposed_action", "retry"),
        workload_metrics=metrics,
    )

    # Record a successful rework if it was allowed (simulated outcome)
    if decision.action.value == "allow_rework":
        governor.record_rework_result(
            task_id, task_type, decision, {"success": True}
        )

    # Output
    output = {
        "scenario": scenario,
        "decision": decision.to_dict(),
        "final_workload_metrics": {
            "queue_length": metrics.queue_length,
            "avg_tool_call_latency_ms": metrics.avg_tool_call_latency_ms,
        },
    }

    if args.format == "json":
        print(json.dumps(output, indent=2, default=str))
    else:
        print(f"\nSimulation Results")
        print("=" * 60)
        print(f"Task: {task_id} (type: {task_type})")
        print(f"Initial attempts: {task_data.get('existing_attempts', 0)}")
        print(f"Failure: {task_data.get('failure_signature')}")
        print(f"Workload queue: {metrics.queue_length}, latency: {metrics.avg_tool_call_latency_ms:.0f}ms")
        print(f"\nDecision: {decision.action.value.upper()}")
        print(f"Reason: {decision.reason}")
        if decision.adjusted_throttle_rate is not None:
            print(f"Throttle rate: {decision.adjusted_throttle_rate * 100:.0f}%")
        # Show final budget state
        final_budget = governor.get_remaining_budget(task_id, task_type)
        print(f"\nBudget after simulation: {final_budget['attempts_used']}/{final_budget['max_attempts']} attempts")

    return 0 if decision.action.value in ("allow_rework", "throttle") else 1


def cmd_reset(args: argparse.Namespace) -> int:
    """Reset all budget tracking."""
    policy = BasicReworkPolicy()
    governor = ReworkGovernor(policy)
    governor.reset_session()
    print("All rework budgets have been reset.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show governor and workload status."""
    policy = BasicReworkPolicy()
    governor = ReworkGovernor(policy)
    stats = governor.get_stats()

    if args.format == "json":
        output = {
            "governor_stats": stats,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print("\nRework Governor Status")
        print("=" * 60)
        print(f"Active budgets: {stats.get('active_budgets', 0)}")
        print(f"Total checks: {stats.get('total_rework_checks', 0)}")
        print(f"Allowed: {stats.get('allowed', 0)}")
        print(f"Escalated: {stats.get('escalated', 0)}")
        print(f"Rejected: {stats.get('rejected', 0)}")
        print(f"Throttled: {stats.get('throttled', 0)}")
        print(f"Tasks at limit: {stats.get('tasks_at_limit', 0)}")
        print(f"Avg decision time: {stats.get('avg_decision_ms', 0):.2f}ms")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Workload-Aware Rework Budget Governor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check a rework decision
  rework-gov check --task-id build-42 --task-type tool_execution \\
      --failure-signature contract_lint_fail --proposed-action retry \\
      --queue-length 50 --latency 2000

  # Simulate a high-workload scenario
  rework-gov simulate --task-type agent_plan --failure-signature tool_latency_spike \\
      --queue-length 150 --latency 8000 --existing-attempts 2

  # View statistics in JSON
  rework-gov stats --format json

  # Reset all budgets
  rework-gov reset
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # check command
    check_parser = subparsers.add_parser("check", help="Check if a rework can proceed")
    check_parser.add_argument("--task-id", required=True, help="Task identifier")
    check_parser.add_argument("--task-type", required=True, help="Task type (e.g., tool_execution, agent_plan)")
    check_parser.add_argument("--failure-signature", required=True,
                              help="Failure signature (contract_lint_fail, sandbox_replay_diff, tool_latency_spike, etc.)")
    check_parser.add_argument("--proposed-action", required=True,
                              help="Proposed rework action (retry, downgrade, alternate_tool, etc.)")
    check_parser.add_argument("--failure-details", help="JSON file with additional failure context")
    check_parser.add_argument("--queue-length", type=int, required=True, help="Current queue length")
    check_parser.add_argument("--latency-ms", type=float, required=True, help="Average tool call latency in ms")
    check_parser.add_argument("--active-agents", type=int, help="Number of active agents")
    check_parser.add_argument("--tool-concurrency", action="append", metavar="TOOL=COUNT",
                              help="Tool concurrency (e.g., gpt-4=5)")
    check_parser.add_argument("--max-attempts", type=int, help="Max rework attempts (per task)")
    check_parser.add_argument("--cooldown", type=int, help="Cooldown period in seconds")
    check_parser.add_argument("--queue-threshold", type=int, help="Queue length throttling threshold")
    check_parser.add_argument("--latency-threshold", type=float, help="Latency throttling threshold (ms)")
    check_parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    check_parser.set_defaults(func=cmd_check)

    # stats command
    stats_parser = subparsers.add_parser("stats", help="View current usage statistics")
    stats_parser.add_argument("--max-attempts", type=int, help="Max rework attempts (for reference)")
    stats_parser.add_argument("--queue-threshold", type=int, help="Queue threshold (for reference)")
    stats_parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    stats_parser.set_defaults(func=cmd_stats)

    # simulate command
    simulate_parser = subparsers.add_parser("simulate", help="Simulate a rework decision scenario")
    simulate_parser.add_argument("--scenario", help="JSON file with scenario configuration")
    simulate_parser.add_argument("--task-id", help="Task ID for simulation")
    simulate_parser.add_argument("--task-type", help="Task type")
    simulate_parser.add_argument("--failure-signature", help="Failure signature")
    simulate_parser.add_argument("--proposed-action", help="Proposed action")
    simulate_parser.add_argument("--existing-attempts", type=int, help="Simulate prior rework attempts")
    simulate_parser.add_argument("--queue-length", type=int, help="Queue length")
    simulate_parser.add_argument("--latency-ms", type=float, help="Avg tool latency (ms)")
    simulate_parser.add_argument("--active-agents", type=int, help="Active agents")
    simulate_parser.add_argument("--tool-concurrency", action="append", metavar="TOOL=COUNT",
                                 help="Tool concurrency")
    simulate_parser.add_argument("--max-attempts", type=int, help="Max rework attempts")
    simulate_parser.add_argument("--cooldown", type=int, help="Cooldown period")
    simulate_parser.add_argument("--queue-threshold", type=int, help="Queue threshold")
    simulate_parser.add_argument("--latency-threshold", type=float, help="Latency threshold")
    simulate_parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    simulate_parser.set_defaults(func=cmd_simulate)

    # reset command
    reset_parser = subparsers.add_parser("reset", help="Reset all budget tracking")
    reset_parser.set_defaults(func=cmd_reset)

    # status command
    status_parser = subparsers.add_parser("status", help="Show governor and workload status")
    status_parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
