# Task: Implement Workload-Aware Rework Budget Governor

**Category:** coordination

## Description

Build a small coordination utility that prevents rework loops from consuming the farm by enforcing a *workload-aware* rework budget per task/class of tasks.

Why: the farm currently has at least one rework task stuck for 12+ hours, and tester pass rate is low (10/23). Existing rework routing exists, but nothing described provides a budget/circuit-breaker that adapts to current farm load and to per-task historical failure modes.

What to build: a CLI/library module that sits in front of rework routing and throttles/escalates rework decisions based on:
- remaining rework budget for the task (count/time)
- observed failure signature (e.g., tool contract lints failing, sandbox replay diffs, tool latency spikes)
- current farm workload (simple signal: queue length / recent tool-call concurrency)

Interface:
- Input: task_id, task_type, failure_signature (string/enum), current_metrics snapshot (queue length + avg tool-call latency), and “proposed rework” action.
- Output: one of {ALLOW_REWOR

## Relevant Existing Artifacts (import/extend if useful)

## Relevant existing artifacts (check before building):
  - **implement-workload-aware-rework-budget-g** (similarity 68%)
    A coordination utility that prevents rework loops from consuming the farm by enforcing a workload-aware rework budget per task/class of tasks.
  - **implement-a-tool-execution-cost-budget-g** (similarity 51%)
    A Python library and CLI for enforcing per-task tool budgets (tokens, latency, spend) before and during tool execution. Prevents runaway costs by maki
  - **implement-an-agent-workload-throttling-f** (similarity 50%)
    A coordination utility that prevents farm-wide resource contention by throttling agent task execution and enforcing per-agent/per-tool fair-share quot
  - **implement-an-agent-integration-runbook-r** (similarity 41%)
    A coordination utility that routes failed/low-confidence agent runs to the most appropriate runbook and QA checkpointer workflow, using existing failu
  - **implement-an-agent-task-data-vault-for-r** [has tests] (similarity 40%)
    Hermetic, content-addressed storage for agent task inputs. Capture the complete input surface of any agent task run for reproducibility, debugging, an

## Related completed tasks:
  - Improve: Build drift-detection monitor to catch s — Implement the full architecture as descr
  - Implement a Tool-Execution Cost & Budget Gatekeeper
  - Build a tool-monitoring agent that updates and handles edge cases
