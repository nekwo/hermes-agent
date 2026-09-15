## The Windows failure mode

`FileOperations._escape_shell_arg` sends **every** argument through
`_bash_safe_path`, which rewrites `C:\Users\x` → `/c/Users/x`. Two mechanisms
in the tree contradict each other there:

- `_apply_windows_msys_bash_env_defaults` sets `MSYS_NO_PATHCONV=1` and
  `MSYS2_ARG_CONV_EXCL=*` on every bash spawn, so MSYS never converts an
  `/c/...` argument back into a native path, and
- the binaries these arguments actually reach (`rg`, `python.exe`) are native
  Windows programs with no MSYS runtime — they cannot resolve `/c/...` at all.

So the rewrite has no counterpart and content search fails outright with
`IO error ... (os error 3)`.

There is a second, quieter bug in the same line. `_bash_safe_path` replaces
**every** backslash in the string, but `_escape_shell_arg` is applied to
non-path arguments too. A `search_files` regex is silently corrupted:

```
pattern in:   func_\w+
pattern out:  func_/w+
```

The search then returns nothing, with no error — the user's regex was changed
under them.

## Minimal repro

```python
from tools.file_operations import FileOperations
# on Windows
FileOperations._escape_shell_arg(None, r"func_\w+")   # -> 'func_/w+'   (corrupted)
FileOperations._escape_shell_arg(None, r"C:\repo")    # -> '/c/repo'    (unresolvable by rg)
```

## What the fix does

Splits the two consumer classes, which are genuinely different:

- `_bash_safe_path` keeps feeding paths that **bash itself** resolves —
  `source`, redirections, `cd` — where the MSYS `/c/...` spelling is correct.
  Unchanged.
- New `_shell_arg_safe_path` handles **program arguments**: emit the
  drive-qualified forward-slash form `C:/Users/x`. The MSYS runtime resolves
  it, native Windows binaries resolve it, and with argument conversion
  disabled MSYS cannot re-mangle it into the `Directory \drivers\etc does not
  exist` failure class.

Anything that is not a drive-qualified path — POSIX paths, relative paths, and
non-path arguments such as a regex or a `python -c` snippet — is returned
**verbatim**. That is what stops the pattern corruption.

## What was tested, on what platform

Windows 10 (19045), Python 3.12.5, native (not WSL):

- `python -m py_compile tools/environments/local.py tools/file_operations.py` — OK
- `python -m pytest tests/tools/test_local_env_windows_msys.py -q`
  - **before this change:** 19 passed, 1 failed
  - **after:** 25 passed, 1 failed

The 6 new tests cover the argument form, the MSYS-leftover fold, POSIX and
relative passthrough, and the regex-corruption regression.

The 1 pre-existing failure is unrelated and present on a clean checkout of
`main` (verified by stashing this change and re-running):
`TestGitBashCoreutilsOnPath::test_derives_dirs_from_portablegit_layout`
asserts POSIX-separator paths (`/pg/usr/bin`) against values built with
`os.path.join`, which emits `\` on Windows. Not touched here.

Not tested on macOS/Linux: both functions are no-ops off Windows
(`if not _IS_WINDOWS: return path`), and the tests fake the platform by
patching `_IS_WINDOWS`, so they exercise the same branches on any host.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
