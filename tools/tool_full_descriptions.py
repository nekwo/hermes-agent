"""Full (untrimmed) tool descriptions — fork-owned mirror for T6b details-on-demand.

Context Cost Workstream T6b (2026-07-18). The wire tool schemas ship BRIEF
descriptions (see each tool file); this module preserves the FULL original
description text so ``tool_describe(name)`` can serve it on demand. The brief
rides every API call; the full docs are one ``tool_describe`` call away.

REVERT CONTRACT: this file is intentionally independent of the description-trim
commit. A ``git revert`` of the trims restores the full text to the schemas while
this mirror keeps serving the same originals — the full docs are never lost.

MIRROR DISCIPLINE: this is a snapshot of the descriptions as they shipped before
the T6b trims. If a tool's genuine documentation changes, update BOTH the brief
schema description and this mirror. Parameter docs are NOT duplicated here —
``tool_describe`` reads live ``parameters`` straight off the registry schema
(the trims never touch parameter schemas).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Union


def _skill_manage_full() -> str:
    """skill_manage full docs, with the profile-aware skills home resolved live."""
    from hermes_constants import display_hermes_home

    return (
        'Manage skills (create, update, delete). Skills are your procedural memory — reusable approaches for recurring task types. New skills go to \x00HERMES_HOME\x00/skills/; existing skills can be modified wherever they live.\n\nActions: create (full SKILL.md + optional category), patch (old_string/new_string — preferred for fixes), edit (full SKILL.md rewrite — major overhauls only), delete, write_file, remove_file.\n\nOn delete, pass `absorbed_into=<umbrella>` when you\'re merging this skill\'s content into another one, or `absorbed_into=""` when you\'re pruning it with no forwarding target. This lets the curator tell consolidation from pruning without guessing, so downstream consumers (cron jobs that reference the old skill name, etc.) get updated correctly. The target you name in `absorbed_into` must already exist — create/patch the umbrella first, then delete.\n\nCreate when: complex task succeeded (5+ calls), errors overcome, user-corrected approach worked, non-trivial workflow discovered, or user asks you to remember a procedure.\nUpdate when: instructions stale/wrong, OS-specific failures, missing steps or pitfalls found during use. If you used a skill and hit issues not covered by it, patch it immediately.\n\nAfter difficult/iterative tasks, offer to save as a skill. Skip for simple one-offs. Confirm with user before creating/deleting.\n\nGood skills: trigger conditions, numbered steps with exact commands, pitfalls section, verification steps. Use skill_view() to see format examples.\n\nDescription: long descriptions are truncated to the first 57 chars plus \'...\' in the system prompt skill index; longer text is visible via skills_list/skill_view. Keep the trigger self-contained in that first 57-char window: \'Use when <trigger>. <one-line behavior>.\'\n\nPinned skills are protected from deletion only — skill_manage(action=\'delete\') will refuse with a message pointing the user to `hermes curator unpin <name>`. Patches and edits go through on pinned skills so you can still improve them as pitfalls come up; pin only guards against irrecoverable loss.'
    ).replace("\x00HERMES_HOME\x00", display_hermes_home())


FULL_TOOL_DESCRIPTIONS: Dict[str, Union[str, Callable[[], str]]] = {
    'session_search': (
        'Search past sessions stored in the local session DB, or scroll inside one. FTS5-backed retrieval over the SQLite message store. No LLM calls — every shape returns actual messages from the DB.\n\nSOURCE-FIRST LIMIT\n\n  This tool searches Hermes conversation history only. It is not evidence about the current contents of external sources. If the user provided a direct source such as a URL, phone number/contact, app/thread, file path, account, website, or live system, inspect that original source before or instead of session_search when accessible. Use session_search as secondary context for what was previously said, not as primary proof of what the source currently contains. If the original source is inaccessible, say so and why before falling back to session history. Do not conclude \'not found\' or \'no prior correspondence\' from session_search alone when a direct source was provided.\n\nFOUR CALLING SHAPES\n\n  1) DISCOVERY — pass `query`:\n     session_search(query="auth refactor", limit=3)\n     Runs FTS5, dedupes hits by session lineage, returns the top N sessions. Each result carries:\n       - session_id, title, when, source\n       - snippet: FTS5-highlighted match excerpt\n       - bookend_start: first 3 user+assistant messages of the session (the goal / kickoff)\n       - messages: ±5 messages around the FTS5 match, with the anchor message flagged (the hit in context)\n       - bookend_end: last 3 user+assistant messages of the session (the resolution / decisions)\n       - match_message_id, messages_before, messages_after\n     Bookends + window together let you reconstruct goal → match → resolution without paying for the whole transcript.\n\n  2) SCROLL — pass `session_id` + `around_message_id`:\n     session_search(session_id="...", around_message_id=12345, window=10)\n     Returns a window of ±`window` messages centered on the anchor. No FTS5, no bookends — just the slice. Use after a discovery call when you need more context than the ±5 default window.\n       - To scroll FORWARD: pass messages[-1].id back as around_message_id.\n       - To scroll BACKWARD: pass messages[0].id back as around_message_id.\n       - The boundary message appears in both windows — orientation marker.\n       - When messages_before or messages_after is < window, you\'re at the start or end of the session.\n\n  3) READ — pass `session_id` only (no around_message_id):\n     session_search(session_id="...", profile="work")\n     Dumps the whole session by id (first 20 + last 10 messages when large). This is how you resolve an `@session:<profile>/<id>` link the user dropped into the chat: split the value on `/` into profile + id and call session_search(session_id=id, profile=profile).\n\n  4) BROWSE — no args:\n     session_search()\n     Returns recent sessions chronologically: titles, previews, timestamps. Use when the user asks "what was I working on" without naming a topic.\n\nLINKING THE USER TO A SESSION\n\n  When you refer the user to a session, write its `link` value inline in your reply — every result carries one, e.g. `@session:default/20260722_204335_d62c16`. Copy it verbatim; do not reformat it as a markdown link or wrap it in backticks. Hermes renders it as a link showing the session\'s title, so the link IS the title: use it as a noun mid-sentence ("that\'s @session:default/... — want me to pick it up?"), never alone on its own line, and never alongside the title, id, or date spelled out — that shows the user the same session twice.\n\nFTS5 SYNTAX\n\n  AND is the default — multi-word queries require all terms. Use OR explicitly for broader recall (`alpha OR beta OR gamma`), quoted phrases for exact match (`"docker networking"`), boolean (`python NOT java`), or prefix wildcards (`deploy*`).\n\nWHEN TO USE\n\n  Reach for this on questions about Hermes conversation history itself, such as "what did we do about X", "where did we leave Y", or "find the session where Z". If the user provided a direct source identifier, inspect that source first when accessible; session_search can then supply historical context. The session DB carries what was said when; external tools show current source/world state.'
    ),
    'execute_code': (
        'Run a Python script that can call Hermes tools programmatically. Use this when you need 3+ tool calls with processing logic between them, need to filter/reduce large tool outputs before they enter your context, need conditional branching (if X then Y else Z), or need to loop (fetch N pages, process N files, retry on failure).\n\nUse normal tool calls instead when: single tool call with no processing, you need to see the full result and apply complex reasoning, or the task requires interactive user input.\n\nAvailable via `from hermes_tools import ...`:\n\n  web_search(query: str, limit: int = 5) -> dict\n    Returns {"data": {"web": [{"url", "title", "description"}, ...]}}\n  web_extract(urls: list[str], char_limit: int = None) -> dict\n    Returns {"results": [{"url", "title", "content", "error"}, ...]} where content is markdown.\n    No LLM summarization. Pages over char_limit (default 15000) are head+tail truncated; full text stored on disk (path in the content footer).\n  read_file(path: str, offset: int = 1, limit: int = 500) -> dict\n    Lines are 1-indexed. Returns {"content": "...", "total_lines": N}\n  write_file(path: str, content: str) -> dict\n    Always overwrites the entire file.\n  search_files(pattern: str, target="content", path=".", file_glob=None, limit=50) -> dict\n    target: "content" (search inside files) or "files" (find files by name). Returns {"matches": [...]}\n  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n    Replaces old_string with new_string in the file.\n  terminal(command: str, timeout=None, workdir=None) -> dict\n    Foreground only (no background/pty). Returns {"output": "...", "exit_code": N}\n\nLimits: 5-minute timeout, 50KB stdout cap, max 50 tool calls per script. terminal() is foreground-only (no background or pty).\n\nScripts run in the session\'s working directory with the active venv\'s python, so project deps (pandas, etc.) and relative paths work like in terminal().\n\nPrint your final result to stdout. Use Python stdlib (json, re, math, csv, datetime, collections, etc.) for processing between tool calls.\n\nAlso available (no import needed — built into hermes_tools):\n  json_parse(text: str) — json.loads with strict=False; use for terminal() output with control chars\n  shell_quote(s: str) — shlex.quote(); use when interpolating dynamic strings into shell commands\n  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff for transient failures'
    ),
    'terminal': (
        'Execute shell commands on a Linux environment. Filesystem, current working directory, and exported environment variables persist between calls.\n\nDo NOT use cat/head/tail to read files — use read_file instead.\nDo NOT use grep/rg/find to search — use search_files instead.\nDo NOT use ls to list directories — use search_files(target=\'files\') instead.\nDo NOT use sed/awk to edit files — use patch instead.\nDo NOT use echo/cat heredoc to create files — use write_file instead.\nReserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.\nBecause exported environment state persists, activate a virtualenv or export setup variables once per session; do not re-source the same environment before every command unless a command proves the shell state was reset.\n\nForeground (default): Commands return INSTANTLY when done, even if the timeout is high. Set timeout=300 for long builds/scripts — you\'ll still get the result in seconds if it\'s fast. Prefer foreground for short commands.\nBackground: Set background=true to get a session_id. Almost always pair with notify_on_complete=true — bg without notify runs SILENTLY and you have no way to learn it finished short of calling process(action=\'poll\') yourself. Two legitimate uses:\n  (1) Long-lived processes that never exit (servers, watchers, daemons) — silent is correct, there\'s no exit to notify on.\n  (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — MUST set notify_on_complete=true. Without it you\'ll either forget to poll or sit blocked waiting for the user to surface the result.\nFor servers/watchers, do NOT use shell-level background wrappers (nohup/disown/setsid/trailing \'&\') in foreground mode. Use background=true so Hermes can track lifecycle and output.\nAfter starting a server, verify readiness with a health check or log signal, then run tests in a separate terminal() call. Avoid blind sleep loops.\nUse process(action="poll") for progress checks, process(action="wait") to block until done.\nWorking directory: Use \'workdir\' for per-command cwd.\nPTY mode: Set pty=true for interactive CLI tools (Codex, Claude Code, Python REPL).\n\nDo NOT use vim/nano/interactive tools without pty=true — they hang without a pseudo-terminal. Pipe git output to cat if it might page.\n'
    ),
    "skill_manage": _skill_manage_full,
    'agent_chat_send': (
        "Send a chat message to ANOTHER Harness persona (agent-to-agent chat). Use this when the operator asks you to brief, prompt, deploy, hand off to, or check in with another agent conversationally. The message lands in that persona's own Mission Control chat session and their reply is returned to you. This is conversational only and does not create tracked work. Prefer the persona id (e.g. neko_supervisor, dev, backend_dev, qa), which reaches that persona's canonical primary instance. When a persona runs MORE THAN ONE live instance, pass the specific @personainst_* handle from your Runtime Situation HUD to reach THAT instance exactly. Display names are not accepted.\n\nThread control — threads are TASK-SCOPED (V3, 2026-07-27):\n- OMIT session_id → under the default `new_per_dispatch` policy this MINTS a fresh thread scoped to this task.\n- session_id=<id> → continue THAT exact thread.\n- new_session=false → continue the target's CURRENT default thread.\n- new_session=true → force a fresh thread where the policy would not have opened one. Passing new_session=true together with session_id is contradictory and refused.\nEvery reply carries a `session_established` block {fresh, reason, predecessor_session_id}.\n\nWaiting vs dispatching (2026-08-03):\n- Default (`wait` omitted or true) - you BLOCK on their reply and it comes back in this tool result, on a conversational budget (240s default).\n- `wait=false` - you DISPATCH and keep working. The call returns immediately with a `dispatch_id`; their turn runs in the background on its own longer budget (default 30 min, `agent_runtime.mission_chat.dispatch_max_seconds`); their answer is delivered to you later as a NEW message in this conversation, once you are idle. Use it for anything that takes real time - test suites, builds, long reviews - instead of holding your turn open. The reply is NOT in the wait=false result, so do not re-send because you did not see one.\n- `notify_operator=true` (only meaningful with wait=false) - the delivered turn will instruct you to tell the operator what came back.\n- `agent_chat_dispatches` lists your in-flight and recent background dispatches."
    ),
    'clarify': (
        "Ask the user a question when you need clarification, feedback, or a decision before proceeding. Supports three modes:\n\n1. **Multiple choice (single-select)** — provide up to 4 choices. The user picks one or types their own answer via a 5th 'Other' option.\n2. **Multiple choice (multi-select)** — provide choices and set `multi_select=true` for checkbox selection of several; `user_response` comes back as a list.\n3. **Open-ended** — omit choices entirely. The user types a free-form response.\n\nCRITICAL: when you are offering options, put each option ONLY in the `choices` array — NEVER enumerate the options inside the `question` text. The UI renders `choices` as selectable rows; options written into the question string render as dead prose the user can't pick. Right: question='Which deployment target?', choices=['staging', 'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\nUse this tool when:\n- The task is ambiguous and you need the user to choose an approach\n- You want post-task feedback ('How did that work out?')\n- You want to offer to save a skill or update memory\n- A decision has meaningful trade-offs the user should weigh in on\n\nDo NOT use this tool for simple yes/no confirmation of dangerous commands (the terminal tool handles that). Prefer making a reasonable default choice yourself when the decision is low-stakes."
    ),
    'agent_chat_open': (
        "Review the recent message tail of a chat thread with ONE teammate before continuing it — 'what did we last say to each other?', or 'what did that dispatched task actually say?'. Returns the newest messages (role, text, timestamp) of that teammate's CURRENT default thread (threads are task-scoped, so that is the most recently established one), or of a specific session_id you pass (which must belong to that teammate's chat lane — this is not a transcript browser, foreign sessions are refused); name the session_id when you mean an earlier task's thread. Read-only, never creates a session: if you have never chatted with the target, it says so. Prefer the persona id (e.g. dev, qa, neko_supervisor) to review the canonical primary instance's thread; pass a personainst_* handle to review a SPECIFIC instance's thread when a persona runs several. Pair with agent_chat_threads (to find your threads) and agent_chat_send (to reply)."
    ),
    'agent_chat_threads': (
        "List your agent-to-agent chat threads with the teammates on your level (the personas agent_chat_send can reach — the same @personainst_* handles shown in your Runtime Situation HUD). For each teammate: persona id, display name, canonical personainst_* handle, and their CURRENT default thread's session id + title + last activity + message count when one exists. Threads are task-scoped, so that default is the most recently established thread with that teammate, not a stable per-pair thread. A teammate you have never chatted with is listed honestly with no thread yet (no session is created just to answer this). Read-only. Use this to see who you can talk to and which conversations already exist before deciding whether to continue one (agent_chat_send carrying that session_id — an omitted session opens a fresh task thread instead) or review one first (agent_chat_open)."
    ),
    'browser_vision': (
        'Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Hermes falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.'
    ),
    'browser_navigate': (
        'Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.'
    ),
    'todo': (
        "Manage your task list for the current session. Use for complex tasks with 3+ steps or when the user provides multiple tasks. Call with no parameters to read the current list.\n\nWriting:\n- Provide 'todos' array to create/update items\n- merge=false (default): replace the entire list with a fresh plan\n- merge=true: update existing items by id, add any new ones\n\nEach item: {id: string, content: string, status: pending|in_progress|completed|cancelled}\nList order is priority. Only ONE item in_progress at a time.\nMark items completed immediately when done. If something fails, cancel it and add a revised item.\n\nAlways returns the full current list."
    ),
    'web_extract': (
        "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times out, use the browser tool instead."
    ),
    'read_file': (
        "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are truncated on a line boundary and return a next_offset; continue with offset to read the rest. Jupyter notebooks (.ipynb), Word documents (.docx), and Excel workbooks (.xlsx) are auto-extracted to readable text. NOTE: Cannot read images or other binary files — use vision_analyze for images."
    ),
    'search_files': (
        "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time."
    ),
    'browser_snapshot': (
        "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 15000 chars are truncated or LLM-summarized; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content."
    ),
    'board_card_add': (
        "Add a planning CARD to the Mission Board (a kanban board scoped to the workspace). Use this to track follow-up work worth remembering. A card is planning state only and never starts tracked work. The card lands in the board's Queued column and is attributed to you. Optional, advisory: only add a card when it is genuinely useful."
    ),
    'browser_console': (
        "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically."
    ),
    'patch': (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. Returns a unified diff. Auto-runs syntax checks after editing.\n\nREPLACE MODE (mode='replace', default): find a unique string and replace it. REQUIRED PARAMETERS: mode, path, old_string, new_string.\nPATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. REQUIRED PARAMETERS: mode, patch."
    ),
    'close_terminal': (
        "Close the read-only terminal tab for one of your background processes in the Hermes desktop GUI (the tabs mirroring terminal(background=true) runs). This does NOT kill the process — it only drops the tab/view; the output keeps buffering and the user can reopen it from the status stack. Use it to tidy up when a background process's live terminal is no longer worth showing. To actually stop the process, use process(action='kill') instead."
    ),
    'vision_analyze': (
        'Load an image into the conversation so you can see it. Accepts a URL, local file path, or data URL. When your active model has native vision, the image is attached to your context directly and you read the pixels yourself on the next turn — call this any time the user references an image (filepath in their message, URL in tool output, screenshot from the browser, etc.). For non-vision models, falls back to an auxiliary vision model that returns a text description.'
    ),
    'read_terminal': (
        "Read what's currently shown in the in-app terminal pane of the Hermes desktop GUI (the embedded shell beside this chat). Call with no arguments to get the visible screen plus the total line count (`total_lines`). To page through scrollback, pass `start_line` (0 = oldest line) and `count`; valid lines are [0, total_lines). Returns JSON: {total_lines, start, end, viewport_rows, cursor_row, text}."
    ),
    'process': (
        "Manage background processes started with terminal(background=true). Actions: 'list' (show all), 'poll' (check status + new output), 'log' (full output with pagination), 'wait' (block until done or timeout), 'kill' (terminate), 'write' (send raw stdin data without newline), 'submit' (send data + Enter, for answering prompts), 'close' (close stdin/send EOF)."
    ),
    'write_file': (
        "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out)."
    ),
    'skill_view': (
        "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter."
    ),
    'browser_click': (
        "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first."
    ),
    'browser_type': (
        'Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.'
    ),
    'web_search': (
        'Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and "exact phrase" may work when the backend supports them.'
    ),
    'browser_scroll': (
        'Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.'
    ),
    'browser_get_images': (
        'Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.'
    ),
    'browser_press': (
        'Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.'
    ),
    'board_cards': (
        'List the active cards on the Mission Board for the current workspace (or an explicit board_id): title, column, priority, and any linked goal. Read-only. Use it to check what is already tracked before adding a card.'
    ),
    'browser_back': (
        'Navigate back to the previous page in browser history. Requires browser_navigate to be called first.'
    ),
    'skill_search': (
        'Search installed skills and the Hermes Skills Hub by query without loading full SKILL.md bodies. Returns compact identifiers/descriptions only; use skill_view for installed matches or hermes skills install for external matches.'
    ),
    'skills_list': (
        'List available skills (name + description). Use skill_search(query) to find matching skills or skill_view(name) to load full content.'
    ),
}


def full_tool_description(name: str) -> Optional[str]:
    """Return the full (untrimmed) description for a tool, or None if not mirrored.

    Values may be plain strings or zero-arg callables (for profile-aware text).
    """
    value = FULL_TOOL_DESCRIPTIONS.get(name)
    if value is None:
        return None
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


__all__ = ["FULL_TOOL_DESCRIPTIONS", "full_tool_description"]
