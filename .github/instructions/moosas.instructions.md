---
description: "Use for all code changes in this workspace for the LLM-agent-enabled building performance platform. Enforces Occam's razor, direct logic, mandatory pre-edit alignment, immediate legacy interface removal, and no transition period."
name: "Workspace Occam-First Hard Rules"
applyTo: "**"
---
# Workspace-Wide Occam-First Instruction

- Treat the following as hard rules unless the user explicitly overrides them for a specific task.
- Scope: applies to the entire current workspace and all modifications.
- Apply Occam's razor: if a layer, entity, abstraction, or strategy is not necessary, do not add it.
- Prefer the most direct processing logic that satisfies requirements and constraints.
- Do not add fallback branches, defensive backup strategies, or compatibility shims "just in case".
- When replacing legacy interface logic, delete the old interface path in the same change.
- Do not set temporary transition periods for old interfaces.
- Refactor toward simpler control flow and fewer moving parts whenever this can be done safely.

## Mandatory Pre-Edit Alignment

- Before any code change, provide your technical judgment first:
  - What should change and why.
  - Expected impact and trade-offs.
  - Any explicit risk that needs user confirmation.
- This pre-edit alignment is mandatory for all changes, including small edits.
- Ask the user to confirm details before editing.
- Start edits only after user alignment.

## Decision Rule for Uncertainty

- If details are unclear, ask a targeted clarification question.
- Do not resolve uncertainty by adding extra fallback logic.

## Python Validation Environment

- Run Python validation in the `moosas` Conda environment.
- On this workspace, invoke it explicitly with `D:\miniconda3\Scripts\conda.exe run -n moosas python ...`; do not use an unqualified system Python.

## Tests for Behavior-Preserving Refactoring

- When a refactor does not add or remove functionality, do not add permanent regression tests solely to assert removed content, obsolete import/export paths, or structural relocation.
- Verify these changes with temporary checks while editing and run the relevant existing tests.
- Do not persist these temporary checks in formal test files or test documentation.
- Add permanent regression tests for functional changes or bug fixes only when they cover actual behavior, or when the user explicitly requests them.
