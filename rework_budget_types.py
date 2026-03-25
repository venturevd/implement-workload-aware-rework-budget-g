"""Workload-Aware Rework Budget Governor - Policy Interface.

This module defines the abstract ReworkBudgetPolicy interface for making
rework routing decisions based on budget constraints, failure signatures,
and farm workload.

Usage:
    from workload_aware_rework_budget import ReworkBudgetPolicy, ReworkDecision
    from workload_aware_rework_budget import DecisionAction, FailureSignature

    class MyPolicy(ReworowBudgetPolicy):
        def decide(self, task_id, task_type, failure_sig, metrics, proposed_action):
            # implement decision logic
            pass
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, List


class DecisionAction(Enum):
    """Actions the rework governor can take."""
    ALLOW_REWORK = "allow_rework"           # Let the rework proceed
    ESCALATE = "escalate"                   # Escalate to human/runbook
    REJECT_WITH_ALT = "reject_with_alt"     # Reject but suggest alternative
    THROTTLE = "throttle"                   # Allow but with reduced rate


class FailureSignature(Enum):
    """Types of failure signatures that affect rework decisions."""
    CONTRACT_LINT_FAIL = "contract_lint_fail"     # Tool contract validation failed
    SANDBOX_REPLAY_DIFF = "sandbox_replay_diff"   # Sandbox replay produced differences
    TOOL_LATENCY_SPIKE = "tool_latency_spike"     # Tool execution latency exceeded threshold
    INTEGRATION_GAP = "integration_gap"           # Integration contract mismatch
    GENERIC_FAILURE = "generic_failure"           # Unclassified failure
    REPEATED_FAILURE = "repeated_failure"         # Same failure signature recurring


@dataclass
class TaskReworkBudget:
    """Budget tracking for a specific task or task class."""
    task_id: str
    task_type: str

    # Rework limits
    max_rework_attempts: int = 3                    # Max rework attempts per task
    max_rework_time_period_hours: float = 24.0      # Time window for rework attempts
    rework_cooldown_period_seconds: int = 300       # Cooldown between rework attempts

    # Current usage
    rework_attempts_used: int = 0
    first_rework_attempt_at: Optional[float] = None
    last_rework_attempt_at: Optional[float] = None

    # Historical failure tracking per task type
    failure_count_by_signature: Dict[str, int] = None

    def __post_init__(self):
        if self.failure_count_by_signature is None:
            self.failure_count_by_signature = {}


@dataclass
class FarmWorkloadMetrics:
    """Snapshot of current farm workload."""
    queue_length: int = 0                              # Total tasks in queue
    avg_tool_call_latency_ms: float = 0.0              # Average tool latency
    active_agent_count: int = 0                        # Number of active agents
    tool_concurrency: Dict[str, int] = None            # Current concurrent executions per tool

    def __post_init__(self):
        if self.tool_concurrency is None:
            self.tool_concurrency = {}


@dataclass
class ReworkDecision:
    """Decision result from a rework budget policy."""
    action: DecisionAction
    reason: str
    suggested_alternative: Optional[str] = None
    adjusted_throttle_rate: Optional[float] = None  # For THROTTLE action (0.0-1.0)
    estimated_rework_cost: Optional[float] = None
    budget_remaining: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert decision to dictionary for serialization."""
        return {
            "action": self.action.value,
            "reason": self.reason,
            "suggested_alternative": self.suggested_alternative,
            "adjusted_throttle_rate": self.adjusted_throttle_rate,
            "estimated_rework_cost": self.estimated_rework_cost,
            "budget_remaining": self.budget_remaining,
        }


class ReworkBudgetPolicy(ABC):
    """Abstract base class for workload-aware rework budget policies.

    A ReworkBudgetPolicy decides whether a rework attempt should proceed
    based on:
    - Remaining rework budget for the task
    - Observed failure signature patterns
    - Current farm workload

    Implement this interface to create custom rework strategies:
    - Fixed per-task rework limits
    - Adaptive limits based on failure patterns
    - Workload-based throttling
    - Automatic escalation for stuck tasks
    """

    @abstractmethod
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

        Args:
            task_id: Unique identifier for the task
            task_type: Type/category of the task (e.g., "tool_execution", "agent_plan")
            failure_signature: Type of failure that triggered rework
            failure_details: Optional additional context about the failure
            proposed_action: What rework action is proposed (e.g., "retry", "downgrade", "alternate_tool")
            rework_budget: Current budget tracking for this task
            workload_metrics: Current farm workload snapshot

        Returns:
            ReworkDecision: The policy decision with action and reasoning
        """
        pass

    @abstractmethod
    def record_rework_attempt(
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
        pass

    @abstractmethod
    def get_budget(self, task_id: str, task_type: str) -> TaskReworkBudget:
        """Get or create budget tracking for a task.

        Args:
            task_id: Task identifier
            task_type: Task type

        Returns:
            TaskReworkBudget: Budget tracking object (may be newly created)
        """
        pass

    @abstractmethod
    def update_budget(
        self,
        task_id: str,
        task_type: str,
        increment_attempts: bool = False,
        reset_attempts: bool = False,
    ) -> None:
        """Update budget counters for a task.

        Args:
            task_id: Task identifier
            task_type: Task type
            increment_attempts: Increment the rework attempt counter
            reset_attempts: Reset attempt counter to 0
        """
        pass

    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """Get policy statistics for monitoring.

        Returns:
            Dictionary with metrics including:
            - total_rework_checks: count of check_rework calls
            - allowed: count of ALLOW_REWORK decisions
            - escalated: count of ESCALATE decisions
            - rejected: count of REJECT_WITH_ALT decisions
            - throttled: count of THROTTLE decisions
            - tasks_at_limit: count of tasks at rework limit
            - avg_decision_ms: average decision latency
        """
        pass

    def reset_all_budgets(self) -> None:
        """Reset all budget tracking (for testing or new session)."""
        pass


class ReworkBudgetError(Exception):
    """Base exception for rework budget errors."""
    pass


class BudgetExhaustedError(ReworkBudgetError):
    """Raised when rework budget for a task is exhausted."""

    def __init__(self, task_id: str, attempts_used: int, max_attempts: int):
        self.task_id = task_id
        self.attempts_used = attempts_used
        self.max_attempts = max_attempts
        super().__init__(
            f"Rework budget exhausted for task '{task_id}': "
            f"{attempts_used}/{max_attempts} attempts used"
        )


class WorkloadThresholdError(ReworkBudgetError):
    """Raised when farm workload exceeds safe thresholds."""

    def __init__(self, metric: str, current: float, threshold: float):
        self.metric = metric
        self.current = current
        self.threshold = threshold
        super().__init__(
            f"Workload threshold exceeded: {metric} = {current} > {threshold}"
        )
