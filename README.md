# Workload-Aware Rework Budget Governor

A coordination utility that prevents rework loops from consuming the farm by enforcing a workload-aware rework budget per task/class of tasks.

## Problem Addressed

The farm currently has rework tasks stuck for 12+ hours, and tester pass rates are low (10/23). Existing rework routing exists, but lacks a budget/circuit-breaker that adapts to:
- Current farm load
- Per-task historical failure modes
- Observed failure signatures

## Architecture

```
┌─────────────────────────┐
│   Farm/Task Queue       │
│   (rework task arrives)│
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│  Rework Governor        │  Checks:
│  (check_rework)         │  • Remaining rework budget
│                         │  • Failure signature patterns
└──────────┬──────────────┘  • Farm workload metrics
           ▼
    ┌──────┴──────┬─────────────┐
    │             │             │
    ▼             ▼             ▼
┌─────────┐ ┌──────────┐ ┌────────────┐
│ ALLOW   │ │ESCALATE  │ │ THROTTLE   │
│ rework  │ │to human/ │ │(rate limit)│
│ proceeds│ │runbook   │ │            │
└─────────┘ └──────────┘ └────────────┘
```

## Components

| Component | Responsibility |
|-----------|----------------|
| `rework_budget_types.py` | Abstract interface and data classes |
| `basic_rework_policy.py` | Concrete policy implementation with budget tracking and decision logic |
| `rework_governor.py` | Decision engine wrapper |
| `main.py` | CLI interface |

## Decision Actions

| Action | Description |
|--------|-------------|
| `ALLOW_REWORK` | Rework proceeds normally |
| `ESCALATE` | Block and escalate to human/runbook |
| `REJECT_WITH_ALT` | Reject but suggest alternative approach |
| `THROTTLE` | Allow but with reduced rate (cooldown or adaptive) |

## Decision Factors

1. **Rework Budget** (per task)
   - Max attempts (default: 3)
   - Cooldown period (default: 5 minutes)
   - Time window for attempts (24 hours)

2. **Failure Signature** (severity scoring)
   - `contract_lint_fail` (severity 3) - high priority
   - `sandbox_replay_diff` (severity 3) - high priority
   - `tool_latency_spike` (severity 2) - medium priority
   - `integration_gap` (severity 2) - medium priority
   - `repeated_failure` (severity 4) - critical
   - `generic_failure` (severity 1) - low priority

3. **Farm Workload** (throttling triggers)
   - Queue length threshold (default: 100)
   - Avg tool latency threshold (default: 5000ms)
   - Tool-specific concurrency (expensive tools: GPT-4, Claude Opus, web-search)

## Installation

No external dependencies - uses Python 3 standard library only.

```bash
# Make executable
chmod +x main.py

# Or install globally
ln -s $(pwd)/main.py /usr/local/bin/rework-gov
```

## Quick Start

### Check a rework decision

```bash
python3 main.py check \
    --task-id task-123 \
    --task-type tool_execution \
    --failure-signature contract_lint_fail \
    --proposed-action retry \
    --queue-length 50 \
    --latency-ms 2000 \
    --format text
```

### Simulate a scenario

```bash
python3 main.py simulate \
    --task-type agent_plan \
    --failure-signature tool_latency_spike \
    --queue-length 150 \
    --latency-ms 8000 \
    --existing-attempts 2 \
    --format text
```

### View statistics

```bash
python3 main.py stats --format json
```

### Reset all budgets

```bash
python3 main.py reset
```

## JSON Output Format

```json
{
  "task_id": "task-123",
  "task_type": "tool_execution",
  "failure_signature": "contract_lint_fail",
  "proposed_action": "retry",
  "decision": {
    "action": "allow_rework",
    "reason": "Rework approved: attempt 1/3 for task type 'tool_execution'",
    "estimated_rework_cost": 0.015
  },
  "workload_metrics": {
    "queue_length": 50,
    "avg_tool_call_latency_ms": 2000.0
  }
}
```

## Python Library Usage

```python
from basic_rework_policy import BasicReworkPolicy
from rework_governor import ReworkGovernor
from rework_budget_types import FarmWorkloadMetrics

# Create policy and governor
policy = BasicReworkPolicy(
    max_rework_attempts=3,
    cooldown_period_seconds=300,
    workload_threshold_queue_length=100,
    workload_threshold_latency_ms=5000.0,
)
governor = ReworkGovernor(policy)

# Check if rework can proceed
metrics = FarmWorkloadMetrics(
    queue_length=50,
    avg_tool_call_latency_ms=2000.0,
    active_agent_count=5,
    tool_concurrency={"gpt-4": 3}
)

decision = governor.check_rework(
    task_id="task-123",
    task_type="tool_execution",
    failure_signature="contract_lint_fail",
    failure_details={"lint_errors": ["missing required field"]},
    proposed_action="retry",
    workload_metrics=metrics
)

if decision.action.value == "allow_rework":
    # Execute the rework
    result = execute_rework()
    # Record outcome
    governor.record_rework_result("task-123", "tool_execution", decision, result)
elif decision.action.value == "escalate":
    # Route to human or runbook
    escalate_to_runbook(task_id="task-123")
elif decision.action.value == "throttle":
    # Apply rate limiting
    wait_time = decision.adjusted_throttle_rate * 60  # Example
    time.sleep(wait_time)
```

## Configuration

Policy can be tuned via constructor parameters:

```python
policy = BasicReworkPolicy(
    max_rework_attempts=5,                    # More attempts for complex tasks
    cooldown_period_seconds=600,              # 10 min cooldown
    workload_threshold_queue_length=200,      # Higher threshold
    workload_threshold_latency_ms=10000.0,    # 10 second latency tolerance
    escalate_after_failures_of_same_signature=3,  # More tolerance
    auto_throttle_factor=0.3,                 # Conservative throttling
)
```

## Integration with Farm

Wrap your rework routing with the governor:

```python
def handle_rework_task(task_id, task_type, failure_data):
    # Get current farm metrics from monitoring
    workload = get_current_workload_metrics()

    # Check with governor
    decision = governor.check_rework(
        task_id=task_id,
        task_type=task_type,
        failure_signature=failure_data["signature"],
        proposed_action=failure_data["proposed_action"],
        workload_metrics=workload,
        failure_details=failure_data.get("details")
    )

    if decision.action.value == "allow_rework":
        # Proceed with rework
        result = execute_rework_action(failure_data["proposed_action"])
        governor.record_rework_result(task_id, task_type, decision, result)
        return result
    elif decision.action.value == "throttle":
        # Rate limit this rework
        throttle_factor = decision.adjusted_throttle_rate or 0.5
        time.sleep(throttle_factor * 300)  # Delay before retry
        return handle_rework_task(task_id, task_type, failure_data)  # Retry
    elif decision.action.value == "escalate":
        # Route to human or runbook
        return route_to_runbook(task_id, failure_data)
    else:
        # Reject with alternative
        return {"error": decision.reason, "alternative": decision.suggested_alternative}
```

## Verification

Run these commands to verify the implementation:

```bash
# Test imports
python3 -c 'from rework_budget_types import ReworkBudgetPolicy; print("OK")'
python3 -c 'from basic_rework_policy import BasicReworkPolicy; print("OK")'
python3 -c 'from rework_governor import ReworkGovernor; print("OK")'

# CLI help
python3 main.py --help

# Basic functional test
python3 -c "
from basic_rework_policy import BasicReworkPolicy
from rework_governor import ReworkGovernor
from rework_budget_types import FarmWorkloadMetrics

policy = BasicReworkPolicy(max_rework_attempts=2)
governor = ReworkGovernor(policy)
metrics = FarmWorkloadMetrics(queue_length=10, avg_tool_call_latency_ms=500)

decision = governor.check_rework(
    task_id='test-1',
    task_type='tool_execution',
    failure_signature='generic_failure',
    proposed_action='retry',
    workload_metrics=metrics
)
print(f'Decision: {decision.action.value}')
assert decision.action.value == 'allow_rework'
print('All basic tests passed!')
"

# Run simulation scenarios
python3 main.py simulate --task-id test-1 --task-type tool_execution \
    --failure-signature generic_failure --existing-attempts 0 \
    --queue-length 10 --latency-ms 500 --format text

# Test escalation on repeated failures
python3 main.py simulate --task-id test-2 --task-type tool_execution \
    --failure-signature contract_lint_fail --existing-attempts 2 \
    --queue-length 10 --latency-ms 500 --format text
```

## File Structure

```
implement-workload-aware-rework-budget-g/
├── main.py                        # CLI entry point
├── rework_budget_types.py           # Abstract interface (DecisionAction, etc.)
├── basic_rework_policy.py         # Concrete policy implementation
├── rework_governor.py             # Decision engine wrapper
├── README.md                      # This file
└── samples/                       # Example scenarios (optional)
    ├── low_workload.json
    ├── high_workload.json
    └── escalation_scenario.json
```

## Notes

- Uses Python 3 standard library only (`abc`, `dataclasses`, `enum`, `typing`, `time`, `collections`, `datetime`, `json`)
- Thread-safe structures prepared (locks in governor for future parallelization)
- Designed for integration into the farm's rework routing layer
- All data classes support JSON serialization for logging and monitoring