---
name: staragent-simplify
description: Find and implement evidence-backed simplifications in StarAgent without accidental feature loss. Use when simplifying or refactoring StarAgent, removing redundant or historical code, collapsing duplicated Hub/Node or frontend logic, trimming stale tests/comments/docs, replacing unnecessary custom machinery, or reviewing a change for avoidable complexity and maintenance cost.
---

# Simplify StarAgent

Prefer a few proven reductions over a broad rewrite. Optimize for less code, fewer states, and clearer ownership while preserving observable behavior unless the user explicitly authorizes a product change.

## Establish Context

- Read `.agents/staragent-dev/SKILL.md`, `ARCHITECTURE.md`, and the documentation that owns the area under review.
- Inspect `git status`, the active diff, and recent history before editing. Preserve unrelated and uncommitted user work.
- Treat tmux as the runtime source of truth. Preserve Node scoping, local/remote parity, authentication boundaries, allowlisted execution, and Agent Session continuity.
- Start with the current diff and the largest relevant production files. Expand only when call-site evidence requires it.

## Find Strong Candidates

Look for maintenance surface that costs more than it provides:

- Helpers, routes, fields, states, compatibility aliases, or configuration with no production consumer.
- Tests or documentation as the only consumer of behavior that is no longer a maintained contract.
- Multiple representations or caches for one fact, especially across Hub, Node, browser, and tmux state.
- Repeated normalization, proxy, rendering, or error-handling branches that can share one owner.
- Defensive copies, validators, locks, or fallbacks that protect no real trust or concurrency boundary.
- One-use wrappers that obscure a direct standard-library or framework operation.
- Hand-rolled parsing, retry, diff, or protocol machinery already covered by the Python standard library, a browser platform API, or an existing dependency.
- Comments and docs that restate implementation, describe removed behavior, or live above the layer that owns the contract.

Do not count formatting churn, renamed symbols, code moved into another wrapper, or a new dependency with equal glue as simplification.

## Prove Each Candidate

1. Search the exact symbol, route, wire key, CSS class, data attribute, or configuration name with `rg`.
2. Classify every hit as production, test, documentation, generated output, or runtime state.
3. Inspect dynamic consumers: Jinja-generated paths, delegated browser events, Node capability payloads, tmux command construction, and string-based registries may not look like normal calls.
4. Check Git history when code looks historical or defensive; determine which failure or compatibility promise introduced it.
5. State the simpler owner and the behavior that remains. Estimate net deletion including replacement glue, tests, and docs.
6. Reject the candidate if it requires an unrequested feature decision, weakens a security boundary, or merely relocates complexity.

Treat tests as evidence, not unquestionable truth. Keep tests for public behavior and regressions; remove tests that exist only for deleted private machinery.

## Choose The Action

- Implement immediately when the change is local, behavior-preserving, and supported by complete caller evidence.
- Present a proposal before editing when the simplification changes an API, persistence format, compatibility promise, or user workflow.
- Leave intentional seams intact when they separate Hub from Remote Node behavior, trusted code from wire input, Agent sessions from system sessions, or browser presentation from tmux truth.
- Avoid speculative TODO comments. Record a concise TODO only for a proven local cleanup that cannot safely fit the current change.

## Implement Coherently

- Remove the obsolete implementation, exports, tests, styles, translations, and documentation together.
- Prefer an existing shared owner over adding another abstraction. Extract a helper only when it eliminates repeated policy or state.
- Keep browser event delegation scoped to the component that owns the event; root-level `data-*` attributes can otherwise become accidental matches.
- Keep API responses normalized at trust boundaries. Do not expose raw credentials, arbitrary commands, filesystem secrets, or unbounded subprocess output to the Hub or browser.
- Keep live-service changes surgical. Restart only the supervised StarAgent service that needs new Python code; never terminate user Agent sessions.

## Validate Equivalence

Select checks from the changed surface, then run the complete gates before handoff:

```bash
.conda/bin/ruff check staragent tests
.conda/bin/pytest -q
node --check staragent/dashboard/static/<changed-file>.js
git diff --check
```

For Dashboard changes, exercise the behavior with real browser input on desktop and a narrow viewport. Verify persisted browser state, active/ARIA state, and horizontal overflow. For Hub/Node changes, test both local behavior and the proxy/capability boundary. When the live Hub is running, verify it after tests without touching Agent tmux sessions.

Report what was removed or collapsed, the evidence that made it safe, meaningful candidates intentionally rejected, and all validation performed.
