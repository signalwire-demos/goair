# Enriched Call Log Format — Migration Notes

Reference doc: `https://tatooine.cantina.cloud/devuser/docs/ENRICHED_CALL_LOG.md`

## What changed in the platform

### Breaking field renames (system-log entries)

| Old | New |
|-----|-----|
| `action == "change_step"` | `action == "step_change"` |
| `entry.name` (step name) | `entry.metadata.to_step` |
| `entry.index` (step index) | `entry.metadata.to_index` |
| `action in ["call_function", "gather_submit"]` | `action == "function_call"` |
| `entry.function` (func name) | `entry.metadata.function` |

### What moved into metadata (system-log `step_change`)

```json
{
  "role": "system-log",
  "action": "step_change",
  "metadata": {
    "context": "default",
    "step": "collect_booking",
    "step_index": 3,
    "from_step": "collect_trip_type",
    "from_index": 2,
    "to_step": "collect_booking",
    "to_index": 3,
    "trigger": "gather_complete"
  }
}
```

Trigger values: `"ai_function"`, `"webhook_action"`, `"gather_complete"`, `"auto_advance"`

### What moved into metadata (system-log `function_call`)

```json
{
  "role": "system-log",
  "action": "function_call",
  "metadata": {
    "function": "resolve_location",
    "native": false,
    "duration_ms": 234,
    "error": null
  }
}
```

**Note:** Args are NOT in call_log anymore. Still only in `swaig_log[].command_arg`.

### Tool entries now have `function_name` directly

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "Airport resolved.\nSFO, San Francisco",
  "function_name": "resolve_location",
  "latency": 200
}
```

No more 5-second timestamp matching against `swaig_log` to get function identity.

### New gather flow events (all new — nothing equivalent before)

| action | key fields |
|--------|-----------|
| `gather_start` | `output_key`, `total_questions` |
| `gather_question` | `key`, `question_index`, `question_type`, `requires_confirm` |
| `gather_answer` | `key`, `question_index`, `attempt`, `confirmed` |
| `gather_reject` | `key`, `question_index`, `attempt`, `reason` |
| `gather_complete` | `output_key`, `answered`, `completion_action` |

### New `call_timeline` top-level array

Flat event stream with metadata flattened to top-level. Present alongside `call_log`, `swaig_log`, `times[]`. Entries without metadata are skipped. Use instead of walking `call_log` manually if present.

```json
{
  "call_timeline": [
    {"ts": 1705300001123000, "type": "session_start", "model": "gpt-4o"},
    {"ts": 1705300005012000, "type": "step_change", "from_step": "greeting", "to_step": "collect_info", "trigger": "ai_function"},
    {"ts": 1705300010000000, "type": "gather_start", "output_key": "trip_type_answers", "total_questions": 1},
    {"ts": 1705300015890000, "type": "function_call", "function": "resolve_location", "native": false, "duration_ms": 234},
    {"ts": 1705300016000000, "type": "tool_result", "function_name": "resolve_location", "latency": 234}
  ]
}
```

---

## What needs to change in voyager.py

Only `_parse_call_flow()` (lines 1875–1914) and `_generate_mermaid_flow()` (lines 1917–1981) need updating.

### `_parse_call_flow` changes

1. **Prefer `call_timeline` if present** — use it instead of walking `call_log` manually
2. **Fall back to `call_log`** with updated field names, keeping backwards compat for old logs already in `calls/`
3. **Step change**: `action == "step_change"`, name from `metadata.to_step`, index from `metadata.to_index`
4. **Function call**: `action == "function_call"`, name from `metadata.function`
5. **Gather events**: handle `gather_question` and `gather_answer` as new flow item types
6. **Args for diagram labels**: build a swaig_log queue per `command_name` (in call order), pop the first occurrence when that function appears in flow — args stay rich in the diagram

### `_generate_mermaid_flow` changes

- Add `gather_question` node type (new style class, e.g. dotted border)
- Add `gather_answer` node type (shows confirmed answer)
- Update `select_trip_type` label logic — args are now `{}` (no args), trip_type comes from `trip_type_answers` in global_data not args

---

## Backwards compatibility notes

- Old call logs in `calls/` use the old field names — need dual-path support
- `swaig_log[]` is unchanged — still has `command_name`, `command_arg`, `command_response`
- `times[]` is unchanged
- User/assistant entry shapes are unchanged
- The only hard break: `change_step` → `step_change`, `entry.name` → `entry.metadata.to_step`

---

## TODO

- [ ] Get a sample call log with new format to test against
- [ ] Rewrite `_parse_call_flow` with `call_timeline` fast path + call_log fallback
- [ ] Update `_generate_mermaid_flow` with gather node types
- [ ] Update `select_trip_type` label logic (no args anymore)
- [ ] Smoke test flow generation against both old and new format logs
