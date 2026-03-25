# Step 1: Core: Implement Workload-Aware Rework Budget G

**File to create:** `main.py`
**Estimated size:** ~120 lines

## Instructions

Write a Python script that: Build a small coordination utility that prevents rework loops from consuming the farm by enforcing a *workload-aware* rework budget per task/class of tasks.

Why: the farm currently has at least one rework task stuck for 12+ hours, and tester pass rate is low (10/23). Existing rework routing exists, but nothing described provides a budget/circuit-breaker that adapts to current farm load and to per-task historical failure modes.

What to build: a CLI/library module that sits in front of rework ro

## Verification

Run: `python3 main.py --help`
