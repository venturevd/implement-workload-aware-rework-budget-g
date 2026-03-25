"""Basic Rework Budget Policy Implementation.

A concrete ReworkBudgetPolicy that enforces:
- Per-task rework attempt limits
- Cooldown periods between attempts
- Workload-based throttling
- Failure signature-based escalation

Usage:
    from basic_rework_policy import BasicReworkPolicy
    from rework_budget_types import FarmWorkloadMetrics

    policy = BasicReworkPolicy(
        max_rework_attempts=3,
        workload_threshold_queue_length=100,
        workload_threshold_latency_ms=5000
    )
"""

import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Optional

from rework_budget_types import (
    DecisionAction,
    FailureSignature,
    FarmWorkloadMetrics,
    ReworkBudgetPolicy,
    ReworkDecision,
    TaskReworkBudget,
)


# Failure signature severity scores (higher = more severe)
FAILURE_SEVERITY = {
    FailureSignature.CONTRACT_LINT_FAIL.value: 3,
    FailureSignature.SANDBOX_REPLAY_DIFF.value: 3,
    FailureSignature.TOOL_LATENCY_SPIKE.value: 2,
    FailureSignature.INTEGRATION_GAP.value: 2,
    FailureSignature.REPEATED_FAILURE.value: 4,
    FailureSignature.GENERIC_FAILURE.value: 1,
}


class BasicReworkPolicy(ReworkBudgetPolicy):
    """Concrete rework budget policy with configurable limits and strategies.

    Decision Logic:
    1. Check rework budget (max attempts, cooldown)
    2. Evaluate failure signature severity
    3. Assess farm workload (queue length, latency)
    4. Decide: ALLOW, ESCALATE, REJECT, or THROTTLE

    Attributes:
        max_rework_attempts: Maximum rework attempts per task
        cooldown_period_seconds: Minimum time between rework attempts
        workload_threshold_queue_length: Queue length that triggers throttling
        workload_threshold_latency_ms: Latency threshold for throttling (ms)
        escalate_after_failures_of_same_signature: Count before escalation
        auto_throttle_factor: Throttle rate when workload is high (0.0-1.0)
        failure_escalation_multiplier: Severity threshold for escalation
    """

    def __init__(
        self,
        max_rework_attempts: int = 3,
        cooldown_period_seconds: int = 300,
        workload_threshold_queue_length: int = 100,
        workload_threshold_latency_ms: float = 5000.0,
        escalate_after_failures_of_same_signature: int = 2,
        auto_throttle_factor: float = 0.5,
        failure_escalation_multiplier: float = 2.0,
    ):
        """Initialize the basic rework policy.

        Args:
            max_rework_attempts: Maximum rework attempts per task
            cooldown_period_seconds: Minimum seconds between rework attempts
            workload_threshold_queue_length: Queue length triggering throttling
            workload_threshold_latency_ms: Latency threshold in milliseconds
            escalate_after_failures_of_same_signature: Same signature count before escalation
            auto_throttle_factor: Throttle factor when workload is high (0.0-1.0)
            failure_escalation_multiplier: Severity threshold for auto-escalation
        """
        # Configuration
        self.max_rework_attempts = max_rework_attempts
        self.cooldown_period_seconds = cooldown_period_seconds
        self.workload_threshold_queue_length = workload_threshold_queue_length
        self.workload_threshold_latency_ms = workload_threshold_latency_ms
        self.escalate_after_failures_of_same_signature = escalate_after_failures_of_same_signature
        self.auto_throttle_factor = max(0.0, min(1.0, auto_throttle_factor))
        self.failure_escalation_multiplier = failure_escalation_multiplier

        # Budget tracking: task_id -> TaskReworkBudget
        self._budgets: Dict[str, TaskReworkBudget] = {}

        # Statistics
        self._stats = {
            "total_rework_checks": 0,
            "allowed": 0,
            "escalated": 0,
            "rejected": 0,
            "throttled": 0,
            "tasks_at_limit": 0,
            "avg_decision_ms": 0.0,
            "total_decision_time_ms": 0.0,
        }

        # Per-task failure history for by task_type
        self._task_type_failures: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._task_signature_history: Dict[str, List[str]] = defaultdict(list)

        self._lock = None  # For future thread-safety

    def _get_task_budget(self, task_id: str, task_type: str) -> TaskReworkBudget:
        """Get or create budget for a task."""
        if task_id not in self._budgets:
            self._budgets[task_id] = TaskReworkBudget(
                task_id=task_id,
                task_type=task_type,
                max_rework_attempts=self.max_rework_attempts,
                max_rework_time_period_hours=24.0,
                rework_cooldown_period_seconds=self.cooldown_period_seconds,
            )
        return self._budgets[task_id]

    def check_rework(
        self,
        task_id: str,
        task_type: str,
        failure_signature: str,
        failure_details: Optional[Dict[str, Any]],
        proposed_action: str,
        rework_budget: TaskReworkBudget,
        workload_metrics: FarmWorkloadMetrics,
    ) -> ReworkDecision:
        """Decide whether the rework can proceed.

        Decision Priority:
        1. Budget exhaustion -> ESCALATE
        2. Cooldown violation -> THROTTLE
        3. High-severity repeated failures -> ESCALATE
        4. High workload -> THROTTLE (if within budget)
        5. Otherwise -> ALLOW_REWORK

        Args:
            task_id: Unique identifier for the task
            task_type: Type/category of the task
            failure_signature: Type of failure that triggered rework
            failure_details: Optional additional context about the failure
            proposed_action: What rework action is proposed
            rework_budget: Current budget tracking for this task
            workload_metrics: Current farm workload snapshot

        Returns:
            ReworkDecision with action and reasoning
        """
        start_time = time.time()
        self._stats["total_rework_checks"] += 1

        # Track failure history
        self._task_signature_history[task_id].append(failure_signature)
        self._task_type_failures[task_type][failure_signature] += 1

        # Check 1: Max attempts exceeded
        if rework_budget.rework_attempts_used >= self.max_rework_attempts:
            self._stats["tasks_at_limit"] += 1
            elapsed_hours = 0
            if rework_budget.first_rework_attempt_at:
                elapsed_hours = (time.time() - rework_budget.first_rework_attempt_at) / 3600
            decision = ReworkDecision(
                action=DecisionAction.ESCALATE,
                reason=(
                    f"Rework limit reached: {rework_budget.rework_attempts_used}/"
                    f"{self.max_rework_attempts} attempts used "
                    f"(first attempt {elapsed_hours:.1f}h ago). "
                    f"Requires human intervention or runbook escalation."
                ),
                budget_remaining={
                    "attempts_used": rework_budget.rework_attempts_used,
                    "max_attempts": self.max_rework_attempts,
                },
            )
            self._record_decision_time(start_time)
            return decision

        # Check 2: Cooldown period (throttle if too soon)
        if rework_budget.last_rework_attempt_at:
            elapsed_since_last = time.time() - rework_budget.last_rework_attempt_at
            if elapsed_since_last < self.cooldown_period_seconds:
                remaining_cooldown = self.cooldown_period_seconds - elapsed_since_last
                decision = ReworkDecision(
                    action=DecisionAction.THROTTLE,
                    reason=(
                        f"Cooldown period active: {remaining_cooldown:.0f}s remaining "
                        f"since last rework attempt"
                    ),
                    adjusted_throttle_rate=0.0,  # Block until cooldown expires
                )
                self._record_decision_time(start_time)
                return decision

        # Check 3: High-severity repeated failures (per task type)
        failure_count = self._task_type_failures[task_type].get(failure_signature, 0)
        severity = FAILURE_SEVERITY.get(failure_signature, 1)
        if failure_count >= self.escalate_after_failures_of_same_signature and severity >= 2:
            decision = ReworkDecision(
                action=DecisionAction.ESCALATE,
                reason=(
                    f"Repeated high-severity failure: '{failure_signature}' "
                    f"seen {failure_count} times for task type '{task_type}'. "
                    f"Escalating to prevent infinite rework loops."
                ),
                suggested_alternative=self._suggest_alternative_for_signature(failure_signature),
            )
            self._record_decision_time(start_time)
            return decision

        # Check 4: Workload-based throttling
        workload_decision = self._check_workload(workload_metrics, proposed_action)
        if workload_decision:
            self._record_decision_time(start_time)
            return workload_decision

        # All checks passed - allow rework
        decision = ReworkDecision(
            action=DecisionAction.ALLOW_REWORK,
            reason=(
                f"Rework approved: attempt {rework_budget.rework_attempts_used + 1}/"
                f"{self.max_rework_attempts} for task type '{task_type}'"
            ),
            estimated_rework_cost=self._estimate_rework_cost(proposed_action, failure_signature),
        )
        self._record_decision_time(start_time)
        return decision

    def _check_workload(
        self,
        workload_metrics: FarmWorkloadMetrics,
        proposed_action: str,
    ) -> Optional[ReworkDecision]:
        """Evaluate workload and return throttling decision if needed."""
        # Check queue length
        if workload_metrics.queue_length >= self.workload_threshold_queue_length:
            return ReworkDecision(
                action=DecisionAction.THROTTLE,
                reason=(
                    f"High farm workload: queue length {workload_metrics.queue_length} "
                    f">= threshold {self.workload_threshold_queue_length}"
                ),
                adjusted_throttle_rate=self.auto_throttle_factor,
            )

        # Check average tool call latency
        if workload_metrics.avg_tool_call_latency_ms >= self.workload_threshold_latency_ms:
            return ReworkDecision(
                action=DecisionAction.THROTTLE,
                reason=(
                    f"High tool latency: {workload_metrics.avg_tool_call_latency_ms:.0f}ms "
                    f">= threshold {self.workload_threshold_latency_ms:.0f}ms"
                ),
                adjusted_throttle_rate=self.auto_throttle_factor * 0.5,  # Even more conservative
            )

        # Check specific tool concurrency (for expensive tools)
        expensive_tools = ["gpt-4", "claude-opus", "web-search-precise"]
        for tool, concurrency in workload_metrics.tool_concurrency.items():
            if tool in expensive_tools and concurrency > 10:
                return ReworkDecision(
                    action=DecisionAction.THROTTLE,
                    reason=(
                        f"High concurrency on expensive tool '{tool}': "
                        f"{concurrency} concurrent executions"
                    ),
                    adjusted_throttle_rate=self.auto_throttle_factor * 0.3,
                )

        return None

    def _suggest_alternative_for_signature(self, failure_signature: str) -> Optional[str]:
        """Suggest an alternative approach based on failure signature."""
        suggestions = {
            FailureSignature.CONTRACT_LINT_FAIL.value:
                "Fix tool contract lint errors first, then retry",
            FailureSignature.SANDBOX_REPLAY_DIFF.value:
                "Use integration testing with anchored snapshots",
            FailureSignature.TOOL_LATENCY_SPIKE.value:
                "Switch to a faster model or cached results",
            FailureSignature.INTEGRATION_GAP.value:
                "Review integration contract and use fallback data contract",
            FailureSignature.REPEATED_FAILURE.value:
                "Mark task as failed and route to error runbook",
            FailureSignature.GENERIC_FAILURE.value:
                "Check logs and provide detailed error context",
        }
        return suggestions.get(failure_signature)

    def _estimate_rework_cost(self, proposed_action: str, failure_signature: str) -> float:
        """Estimate the cost of this rework attempt."""
        base_costs = {
            "retry": 0.01,
            "downgrade": 0.005,
            "alternate_tool": 0.02,
            "rerun_with_context": 0.05,
        }
        base = base_costs.get(proposed_action, 0.01)

        # Severity multiplier
        severity = FAILURE_SEVERITY.get(failure_signature, 1)
        return base * (1 + (severity - 1) * 0.5)

    def _record_decision_time(self, start_time: float) -> None:
        """Record decision latency for stats."""
        decision_time_ms = (time.time() - start_time) * 1000
        self._stats["total_decision_time_ms"] += decision_time_ms
        count = self._stats["total_rework_checks"]
        if count > 0:
            self._stats["avg_decision_ms"] = self._stats["total_decision_time_ms"] / count

    def record_rework_attempt(
        self,
        task_id: str,
        task_type: str,
        decision: ReworkDecision,
        execution_result: Optional[Dict[str, Any]],
    ) -> None:
        """Record the outcome of a rework attempt."""
        budget = self._get_task_budget(task_id, task_type)

        # Only increment if decision was to allow
        if decision.action == DecisionAction.ALLOW_REWORK:
            if budget.last_rework_attempt_at is None:
                budget.first_rework_attempt_at = time.time()
            budget.last_rework_attempt_at = time.time()
            budget.rework_attempts_used += 1

        # If execution failed with same signature, track it
        if execution_result and not execution_result.get("success", False):
            new_signature = execution_result.get("failure_signature")
            if new_signature and new_signature in self._task_signature_history[task_id]:
                self._task_type_failures[task_type][new_signature] += 1

        # Clean up completed tasks (success after rework)
        if execution_result and execution_result.get("success", False):
            if task_id in self._budgets:
                del self._budgets[task_id]

    def get_budget(self, task_id: str, task_type: str) -> TaskReworkBudget:
        """Get or create budget for a task."""
        return self._get_task_budget(task_id, task_type)

    def update_budget(
        self,
        task_id: str,
        task_type: str,
        increment_attempts: bool = False,
        reset_attempts: bool = False,
    ) -> None:
        """Update budget counters for a task."""
        budget = self._get_task_budget(task_id, task_type)

        if reset_attempts:
            budget.rework_attempts_used = 0
            budget.first_rework_attempt_at = None
            budget.last_rework_attempt_at = None
        elif increment_attempts:
            if budget.last_rework_attempt_at is None:
                budget.first_rework_attempt_at = time.time()
            budget.last_rework_attempt_at = time.time()
            budget.rework_attempts_used += 1

    def get_stats(self) -> Dict[str, Any]:
        """Get policy statistics for monitoring."""
        stats = self._stats.copy()
        stats["active_budgets"] = len(self._budgets)
        stats["task_type_failures"] = dict(self._task_type_failures)
        return stats

    def reset_all_budgets(self) -> None:
        """Reset all budget tracking."""
        self._budgets.clear()
        self._task_type_failures.clear()
        self._task_signature_history.clear()
        self._stats = {
            "total_rework_checks": 0,
            "allowed": 0,
            "escalated": 0,
            "rejected": 0,
            "throttled": 0,
            "tasks_at_limit": 0,
            "avg_decision_ms": 0.0,
            "total_decision_time_ms": 0.0,
        }


class ReworkGovernor:
    """The decision engine that sits in front of rework routing.

    ReworkGovernor responsibilities:
    - Pre-execution: Check rework budget constraints
    - Decision: Return allow/escalate/reject/throttle
    - Post-execution: Record outcomes and update tracking
    - Query: Provide remaining budget and statistics

    Example:
        from basic_rework_policy import BasicReworkPolicy
        from rework_budget_types import FarmWorkloadMetrics

        policy = BasicReworkPolicy(max_rework_attempts=3)
        governor = ReworkGovernor(policy)

        # Check if rework can proceed
        metrics = FarmWorkloadMetrics(
            queue_length=50,
            avg_tool_call_latency_ms=2000.0
        )
        decision = governor.check_rework(
            task_id="task-123",
            task_type="tool_execution",
            failure_signature="contract_lint_fail",
            proposed_action="retry",
            workload_metrics=metrics
        )

        if decision.action.value == "allow_rework":
            # Execute the rework
            result = execute_rework()
            # Record outcome
            governor.record_rework_result(task_id, task_type, decision, result)
    """

    def __init__(self, policy: ReworkBudgetPolicy):
        """Initialize the governor with a rework policy.

        Args:
            policy: A ReworkBudgetPolicy implementation
        """
        self.policy = policy

    def check_rework(
        self,
        task_id: str,
        task_type: str,
        failure_signature: str,
        proposed_action: str,
        workload_metrics: FarmWorkloadMetrics,
        failure_details: Optional[Dict[str, Any]] = None,
    ) -> ReworkDecision:
        """Evaluate whether a rework can proceed."""
        budget = self.policy.get_budget(task_id, task_type)

        decision = self.policy.check_rework(
            task_id=task_id,
            task_type=task_type,
            failure_signature=failure_signature,
            failure_details=failure_details,
            proposed_action=proposed_action,
            rework_budget=budget,
            workload_metrics=workload_metrics,
        )

        # Update stats based on decision
        stats = self.policy.get_stats()
        if decision.action == DecisionAction.ALLOW_REWORK:
            stats["allowed"] = stats.get("allowed", 0) + 1
        elif decision.action == DecisionAction.ESCALATE:
            stats["escalated"] = stats.get("escalated", 0) + 1
        elif decision.action == DecisionAction.REJECT_WITH_ALT:
            stats["rejected"] = stats.get("rejected", 0) + 1

        return decision

    def record_rework_result(
        self,
        task_id: str,
        task_type: str,
        decision: ReworkDecision,
        execution_result: Optional[Dict[str, Any]],
    ) -> None:
        """Record the outcome of a rework attempt."""
        self.policy.record_rework_attempt(task_id, task_type, decision, execution_result)

    def get_remaining_budget(self, task_id: str, task_type: str) -> Dict[str, Any]:
        """Get current remaining budget for a task."""
        budget = self.policy.get_budget(task_id, task_type)
        return {
            "task_id": budget.task_id,
            "task_type": budget.task_type,
            "attempts_used": budget.rework_attempts_used,
            "max_attempts": budget.max_rework_attempts,
            "attempts_remaining": max(0, budget.max_rework_attempts - budget.rework_attempts_used),
            "last_attempt_at": budget.last_rework_attempt_at,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics."""
        return self.policy.get_stats()

    def reset_session(self) -> None:
        """Reset the governor and policy for a fresh session."""
        self.policy.reset_all_budgets()


def load_workload_metrics_from_dict(data: Dict[str, Any]) -> FarmWorkloadMetrics:
    """Load workload metrics from a dictionary.

    Args:
        data: Dictionary with workload metrics

    Returns:
        FarmWorkloadMetrics instance
    """
    return FarmWorkloadMetrics(
        queue_length=data.get("queue_length", 0),
        avg_tool_call_latency_ms=data.get("avg_tool_call_latency_ms", 0.0),
        active_agent_count=data.get("active_agent_count", 0),
        tool_concurrency=data.get("tool_concurrency", {}),
    )
