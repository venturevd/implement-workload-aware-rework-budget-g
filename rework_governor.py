"""Rework Governor - Decision Engine for Workload-Aware Rwork Budget.

The ReworkGovernor sits in front of rework routing and makes decisions
based on policy, budget constraints, and farm workload.

Usage:
    from rework_governor import ReworkGovernor
    from basic_rework_policy import BasicReworkPolicy
    from rework_budget_types import FarmWorkloadMetrics

    policy = BasicReworkPolicy(max_rework_attempts=3)
    governor = ReworkGovernor(policy)

    metrics = FarmWorkloadMetrics(queue_length=50, avg_tool_call_latency_ms=2000)
    decision = governor.check_rework(
        task_id="task-123",
        task_type="tool_execution",
        failure_signature="contract_lint_fail",
        proposed_action="retry",
        workload_metrics=metrics
    )
"""

from typing import Any, Dict, Optional

from rework_budget_types import (
    DecisionAction,
    FarmWorkloadMetrics,
    ReworkBudgetPolicy,
    ReworkDecision,
)


class ReworkGovernor:
    """The decision engine that sits in front of rework routing.

    ReworkGovernor responsibilities:
    - Pre-execution: Check rework budget constraints
    - Decision: Return allow/escalate/reject/throttle
    - Post-execution: Record outcomes and update tracking
    - Query: Provide remaining budget and statistics

    Integrates a ReworkBudgetPolicy with rework execution to make
    real-time workload-aware decisions.
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
        """Evaluate whether a rework can proceed.

        Args:
            task_id: Task identifier
            task_type: Task type/category
            failure_signature: Type of failure that triggered rework
            proposed_action: What rework action is proposed
            workload_metrics: Current farm workload snapshot
            failure_details: Optional additional context

        Returns:
            ReworkDecision with action and reasoning
        """
        budget = self.policy.get_budget(task_id, task_type)

        return self.policy.check_rework(
            task_id=task_id,
            task_type=task_type,
            failure_signature=failure_signature,
            failure_details=failure_details,
            proposed_action=proposed_action,
            rework_budget=budget,
            workload_metrics=workload_metrics,
        )

    def record_rework_result(
        self,
        task_id: str,
        task_type: str,
        decision: ReworkDecision,
        execution_result: Optional[Dict[str, Any]],
    ) -> None:
        """Record the outcome of a rework attempt.

        Args:
            task_id: Task identifier
            task_type: Task type
            decision: The decision that was made
            execution_result: Optional result data including success/failure
        """
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
