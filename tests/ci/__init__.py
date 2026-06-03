"""CI gate tests for the workspace.

The ``tests/ci/`` directory hosts cross-cutting build-time gates that
run on every commit. They are example-based (not Hypothesis property
tests) and intentionally lean on filesystem walks + plain string
matching so the failure mode reads as a concrete diff, not a shrunk
counterexample.

Current gates:

* :mod:`tests.ci.test_taskprompt_mimari_sync` - prompt backlog sync gate.
 Asserts every backlog ID mentioned by a prompt under ``platform/prompts/``
 (or any other ``prompts/`` directory shipped with the platform tree) also
 appears in the workspace backlog document.
"""
