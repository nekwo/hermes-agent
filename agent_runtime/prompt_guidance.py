"""Stable downstream tool guidance, independent of upstream prompt layout."""

TOOL_DESCRIBE_GUIDANCE = (
    "Tool descriptions are brief. Before the first use of an unfamiliar tool, "
    "call tool_describe(<name>) to load its full documentation and parameter "
    "reference."
)

SHELL_TOOL_PREFERENCE_GUIDANCE = (
    "Prefer the native tools over shell equivalents: read_file (not "
    "cat/head/tail), search_files (not grep/rg/find/ls), patch (not sed/awk), "
    "write_file (not echo/heredoc). Reserve terminal for builds, installs, git, "
    "processes, and scripts."
)

CLARIFY_CHOICES_GUIDANCE = (
    "When using clarify with options, put each option only in the `choices` "
    "array — never enumerate them inside the question text (choices render as "
    "pickable rows)."
)

BROWSER_PRECONDITION_GUIDANCE = (
    "All browser_* tools require a prior browser_navigate; browser_click and "
    "browser_type also require a prior browser_snapshot."
)

_WINDOWS_NATIVE_TOOLING_HINT = (
    "Windows-native tooling: your terminal is bash, but when a task genuinely "
    "needs PowerShell or cmd (a cmdlet, a `.ps1` script, a Windows-only CLI), "
    "invoke it as a program from bash — `powershell.exe -NoProfile -Command "
    "'<script>'` (or `pwsh` for PowerShell 7), or `cmd.exe /c '<command>'`. "
    "These are on PATH; quote the inner command so bash passes it through "
    "verbatim. Prefer plain POSIX for everything else."
)
