# Changelog

All notable changes to Native Agent Runtime Router are documented here.

## Unreleased

- Corrected installation instructions because the package is not yet published on PyPI.
- Added per-command Alibaba Cloud and Tsinghua TUNA mirror examples for dependency installation.
- Added `[OK]`, `[X]`, and `[i]` markers plus Node.js version validation to `nar doctor`.
- Added two regression tests for healthy and missing dependency diagnostics.

## [1.0.0] - 2026-09-07

- Added a deterministic, model-free task kernel with bounded waiting, workspace locks, cancellation, recovery, budgets, verification, and one pre-authorized repair.
- Added five stable MCP tools: `agents`, `run`, `wait`, `inspect`, and `cancel`.
- Added the `nar` CLI for local operation and diagnostics.
- Added native ZCode and generic ACP adapters with session continuation and model selection.
- Added usage accounting, deterministic agent scoring, scope auditing, and sanitized diagnostics.
- Added English and Simplified Chinese documentation.
- Added 64 offline automated tests and documented live verification against ZCode 0.16.5 and opencode 1.18.15.

[1.0.0]: https://github.com/BerineYang/native-agent-router/releases/tag/v1.0.0
