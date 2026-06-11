# Project Gemini Instructions (musicstream)

This file contains foundational mandates for all AI agents operating in this repository.

## Core Mandates

### 1. Handoff Protocol (CRITICAL)
- **Status Tracking**: Every session MUST conclude with a handoff document or a clear update to a `HANDOFF.md` or `MEMORY.md` file.
- **Continuity**: Ensure that any changes, decisions, and pending tasks are documented so another agent can immediately resume work without context loss.
- **Location**: Prefer `.claude/handoffs/` for detailed session snapshots.

### 2. Internet Awareness & Resilience
- **Connectivity Checks**: The application is designed to handle internet loss gracefully.
- **Wait on Startup**: Background tasks and CLI commands must wait for internet connectivity before proceeding.
- **Graceful Pause**: Background tasks are decorated with `@pause_on_no_internet` to block execution during outages without flooding logs with errors.
- **Maintenance**: When adding new network-dependent features, ensure they utilize `src.utils.wait_for_internet` or the `@pause_on_no_internet` decorator.

### 3. Development Workflow
- **TDD**: Always write or update tests before implementation.
- **Spec-First**: Refer to `SPEC.md` and `tasks.md` for project scope and current priorities.
- **Types**: Use strict typing in Python (type hints) and TypeScript.

## Documentation Reference
- `SPEC.md`: Technical specification and architectural invariants.
- `tasks.md`: Current execution task list and status.
- `AGENTS.md`: Workspace-wide agent configurations and universal rules.
