# Testing Plans

Status: historical record index.

Longer-form test strategy documents that do not belong to one feature module.

- [debug-first-refactor-e2e.md](debug-first-refactor-e2e.md): five end-to-end
  original end-to-end plans, now backed by seven concrete `tests/e2e/`
  test files that add full-stack NR/AEC and latency benchmarking coverage.

Current note: these plans are no longer just aspirational. Static inspection found
corresponding concrete tests under `tests/e2e/`, including latency,
interruption, adversarial audio, stress, and record/replay coverage.
