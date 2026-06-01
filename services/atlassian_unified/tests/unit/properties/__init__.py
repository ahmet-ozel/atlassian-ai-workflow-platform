"""Property-based tests for the Atlassian DC tool parity feature.

This package hosts `hypothesis` property tests for the cross-cutting guards
(`utils/dc_guards.py`, `utils/secret_redaction.py`) and for individual tool
invariants (secret hygiene, owner-scoped delete, empty-query short-circuit,
comment visibility, copy-page-tree, CQL validation, etc.).

Shared Hypothesis profiles, fixtures, and tool-arg strategies live in
`conftest.py` alongside this file.
"""
