# Stateful search

Remote MCP Commander keeps the existing one-shot `search_files` tool and also exposes a bounded cursor-style search flow:

1. `start_search(...)` scans a bounded tree once and returns the first page plus a `session_id`.
2. `get_more_search_results(agent_id, session_id, limit=...)` advances the Agent-owned cursor without rescanning the filesystem.
3. `stop_search(agent_id, session_id)` immediately releases the cached result set.

## Bounds

- at most 10,000 files are scanned by the underlying search implementation;
- a stateful search retains at most 2,000 matches;
- page size is at most 200 matches;
- at most 20 search sessions are retained per Agent connection; the oldest session is evicted when that bound is exceeded;
- content search ignores files larger than 1 MiB and binary/NUL-containing files, matching one-shot search behavior;
- symlink directories and symlink files are not traversed;
- hidden files/directories are skipped by default (`include_hidden=false`), matching Desktop Commander-style code search;
- traversal order is deterministic by sorted directory and file name.

`exhausted=true` means there are no more cached matches in the current search session. `truncated=true` means the bounded scan or match ceiling was reached, so additional matches may exist outside the retained result set.

Search sessions are connection-local and disappear when the Agent reconnects. Older Agents that do not advertise `filesystem.search_session` are rejected by the Gateway rather than silently falling back to one-shot search.

## Session navigation

`list_searches(agent_id)` returns every retained search with its query, root, mode, total cached matches, current cursor, and truncation state.

`get_more_search_results` supports two access modes:

- omit `offset` to consume from the session cursor and advance it;
- provide a non-negative `offset` to re-read an exact range without moving the cursor;
- provide a negative `offset` (for example `-20`) to read from the tail of the cached result set without moving the cursor.

The returned page includes `offset`, `next_offset`, and `total_matches`, so callers can navigate deterministically. Explicit offset reads never modify the default sequential cursor.
