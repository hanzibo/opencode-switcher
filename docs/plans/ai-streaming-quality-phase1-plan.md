# AI Streaming Quality — Phase 1 Plan

## Scope

Fix four high-priority correctness issues identified in the read-only audit of
`fix/ai-background-stream-persistence`:

1. `/skill` manual execution calls the nonexistent `_on_ai_send_clicked`.
2. Background `_on_tool_calls_started` callbacks can mutate the visible stream.
3. `_on_tool_result` compares against the global request counter instead of the
   active stream request id.
4. Panel send/retry paths discard `thinking_enabled` and `reasoning_effort`.

This phase intentionally excludes the Phase 2/3 refactors: extracting shared
ownership helpers, converting model configuration to a dataclass, deduplicating
test fixtures, and broad `AIChatPanel` decomposition.

## Implementation Steps

### 1. Skill manual send

- Replace `_on_ai_send_clicked()` with the existing `_on_send_clicked()` in
  `_handle_skill_command`.
- Add coverage for successful, bare, and unknown `/skill` commands.

### 2. Tool-call-start ownership

- Resolve the stream owner by scanning `_ai_running_convs` for the callback's
  `req_id`.
- Only the visible conversation's active streaming state may cancel flush
  timers or call `finishReasoning()`.
- Add foreground, background, and unknown/superseded request tests.

### 3. Tool-result ownership

- Replace the global `_ai_request_id` comparison in `_on_tool_result` with
  per-stream `req_id` and visible-conversation validation.
- Preserve rejection for background, unknown, and non-streaming states.
- Add rebound foreground acceptance and rejection tests.

### 4. Thinking configuration propagation

- Preserve `thinking_enabled` and `reasoning_effort` when unpacking
  `_read_model_config()` in `_send_user_message` and `_retry_response`.
- Pass both values to `_run_llm_api_request` in the same order used by
  `ask_llm_api`.
- Add tests for send, retry, defaults, and positional argument order.

## Validation

- Run each new focused test module while implementing its fix.
- Run the affected AI streaming tests, then:
  `venv/bin/python3 -m unittest discover tests`.
- Run diagnostics on changed Python files where the configured LSP is
  available; otherwise record the environment limitation and use compilation
  plus unittest results.
- Run the post-implementation review workflow before reporting completion.

## Commit Strategy

Use one plan commit followed by four atomic fix commits:

1. `docs(ai-panel): phase 1 ai streaming quality plan`
2. `fix(ai-panel): invoke _on_send_clicked from skill manual trigger`
3. `fix(ai-panel): isolate tool-calls-started to foreground stream`
4. `fix(ai-panel): validate tool results per stream req_id`
5. `fix(ai-panel): propagate thinking_enabled and reasoning_effort on send and retry`

Each fix commit contains only its production change and direct regression
test. Implement the shared production file serially so atomic staging remains
unambiguous. Do not merge or push.

## Rollback

Each fix is independently revertible with `git revert <commit>`. No database
schema, migration, dependency, or persisted-data format changes are planned.

## Acceptance Criteria

- `/skill` manual execution sends its generated payload.
- Background tool phases do not cancel or complete the visible conversation's
  reasoning UI.
- Rebound foreground streams accept their own tool results; other streams do
  not update the visible DOM.
- Panel send and retry preserve thinking configuration.
- All focused and full tests pass, with only documented pre-existing skips or
  environment warnings.
