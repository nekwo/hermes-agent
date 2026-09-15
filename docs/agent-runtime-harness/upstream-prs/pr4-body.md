## The Windows failure mode

`_find_bash` ends its Windows candidate list with a bare `shutil.which("bash")`.
On any box with the WSL optional feature enabled, that resolves to
`C:\Windows\System32\bash.exe` — the WSL launcher, not Git Bash.

The comment above the Git-for-Windows probes already acknowledges this hazard,
but nothing actually rejects the stub. And the `_bash_starts` filter does not
screen it out either — the stub starts successfully.

Measured on a stock Windows 10 host that also has Git for Windows installed:

```
which(bash)        = C:\WINDOWS\system32\bash.EXE
_bash_starts(stub) = True
```

So it is a fully viable candidate. It wins whenever it is reached — Git for
Windows installed outside the three probed locations (scoop, chocolatey, a
custom directory), or every earlier candidate failing to start — and when it
is the only candidate, the `return candidates[0]` last-resort hands it back
regardless.

It then fails every real invocation, because it is a Linux shell that cannot
see Windows-form paths at all:

## Minimal repro

```
$ C:\Windows\System32\bash.exe -lc "pwd; ls 'C:/Program Files'"
/mnt/x/...
ls: cannot access 'C:/Program Files': No such file or directory
```

Exit code 0, so nothing upstream of it notices. Every terminal call the agent
routes through this shell silently targets the wrong filesystem.

## What the fix does

Adds `_is_windows_system_shim` and gates **only** the PATH fallback on it:

- rooted at `%SystemRoot%` rather than a hardcoded `C:\Windows`, so a
  relocated Windows install is handled,
- covers `System32` / `SysWOW64` / `Sysnative`,
- case- and separator-insensitive, so `c:/windows/system32/bash.exe` and
  `C:\WINDOWS\System32\bash.exe` both match.

An explicitly-configured `HERMES_GIT_BASH_PATH` is untouched — if an operator
deliberately points at something, that is their call. Only the PATH *guess* is
filtered.

The not-found error message now states why the System32 bash is ignored, so it
does not read as a broken probe to someone who can plainly see a `bash.exe` on
their PATH.

## What was tested, on what platform

Windows 10 (19045), Python 3.12.5, native (not WSL):

- `python -m py_compile tools/environments/local.py` — OK
- `python -m pytest tests/tools/test_local_env_windows_msys.py -q`
  - **before this change:** 19 passed, 1 failed
  - **after:** 23 passed, 1 failed
- Live check on the same host, which genuinely has the stub first on PATH:

```
which(bash)  = C:\WINDOWS\system32\bash.EXE
is_stub      = True
_find_bash() = C:\Program Files\Git\bin\bash.exe
```

The 4 new tests cover the three system subdirectories, case/separator
insensitivity, a relocated `%SystemRoot%`, and that real Git Bash paths (and
POSIX paths, and empty input) are **not** rejected.

The 1 pre-existing failure is unrelated and present on a clean checkout of
`main` (verified by stashing this change and re-running):
`TestGitBashCoreutilsOnPath::test_derives_dirs_from_portablegit_layout`
asserts POSIX-separator paths against `os.path.join` output. Not touched here.

Not tested on macOS/Linux: the guarded branch is inside the
`if not _IS_WINDOWS: return ...` early-out's Windows-only tail, so it cannot
execute there. The new unit tests drive the predicate directly and pass on any
host.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
