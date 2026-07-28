# Hermes Mobile Core Dependency Certification

**Decision:** GO for the Stage 1 Python distribution. The committed package
closure contains no native extension, provider SDK, dynamic installer, desktop
runtime, or forbidden process/tool module. The built core wheel is
`hermes_mobile_core-0.2.0-py3-none-any.whl` (`Root-Is-Purelib: true`) and is
approximately 48.9 KB (compressed ZIP size varies slightly with build timestamps).

Certification baseline: Hermes checkout
`d994dbf5be9e9b159062914f72f5c55040674c13`; the newest commit affecting the
vendored allowlist is recorded in `VENDOR_STAMP`. Python 3.12 and the versions
already resolved in the root Hermes lock on 2026-07-27. Package licenses and
wheel tags were checked from the corresponding wheel metadata. Compressed
wheel sizes are the locked PyPI artifact sizes; they are not installed sizes.

## Vendored source closure

| Entry | Imports reachable in source (lazy imports included) | Class | License | Decision |
|---|---|---:|---|---|
| `agent/transports/__init__.py` | vendored `types`, generated registry import of `chat_completions` only | Pure | MIT (Hermes) | Include; Anthropic, Bedrock, and Codex discovery removed by generator |
| `agent/transports/base.py` | `abc`, `typing`, vendored `types` | Pure | MIT (Hermes) | Include |
| `agent/transports/types.py` | `json`, `dataclasses`, `typing` | Pure | MIT (Hermes) | Include |
| `agent/transports/chat_completions.py` | `copy`, `typing`, the allowlisted helpers, generated shims, vendored transport/provider base | Pure | MIT (Hermes) | Include |
| `agent/lmstudio_reasoning.py` | `typing` | Pure | MIT (Hermes) | Include |
| `agent/moonshot_schema.py` | `copy`, `typing` | Pure | MIT (Hermes) | Include |
| `providers/base.py` | `logging`, `dataclasses`, `typing`, stdlib `urllib`/`json`; its optional version-label lookup catches absent `hermes_cli` | Pure | MIT (Hermes) | Include; mobile facade never exposes model-catalog fetching |
| `agent/prompt_builder.py` shim | no imports; `DEVELOPER_ROLE_MODELS` only | Pure/generated | MIT (Hermes) | Include; live-value parity test required |
| `agent/gemini_native_adapter.py` shim | no imports; native detection always false | Pure/generated | MIT (Hermes) | Include; Gemini-native remains deferred |
| `tools/registry.py` shim | `hermes_mobile_core.exceptions` | Pure/generated | MIT (Hermes) | Include; raises `MobileUnsupported` on access |
| `hermes_mobile_core` handwritten modules | stdlib plus `httpx`; all local modules are scanned recursively | Pure | MIT | Include |

The static gate parses every `*.py` file under `hermes_mobile_core`, walks the
entire AST (including function bodies), and rejects subprocess/PTY/process
discovery, Harness/Mission Control/worktree/daemon/Stage C, FastAPI/Uvicorn,
MCP/browser/filesystem/code-execution tools, credential/env loaders, lazy
dependency installers, and dynamic execution/install calls. The test also
injects a forbidden function-level lazy import to prove that it is detected.

## HTTPS dependency closure

| Distribution | Pin | Compressed wheel | Wheel tag | Class | License |
|---|---:|---:|---|---:|---|
| `httpx` | 0.28.1 | 73,517 B | `py3-none-any` | Pure | BSD-3-Clause |
| `httpcore` | 1.0.9 | 78,784 B | `py3-none-any` | Pure | BSD-3-Clause |
| `h11` | 0.16.0 | 37,515 B | `py3-none-any` | Pure | MIT |
| `anyio` | 4.12.1 | 113,592 B | `py3-none-any` | Pure | MIT |
| `certifi` | 2026.5.20 | 134,134 B | `py3-none-any` | Pure | MPL-2.0 |
| `idna` | 3.15 | 72,340 B | `py3-none-any` | Pure | BSD-3-Clause |
| `typing-extensions` (Python <3.13) | 4.15.0 | 44,614 B | `py3-none-any` | Pure | PSF-2.0 |
| **Resolved dependency total on Python 3.11/3.12** |  | **554,496 B (0.529 MiB)** | all `py3-none-any` | **Pure** | compatible |

`sniffio` is not in the resolved AnyIO 4.12.1 closure, so Stage 0 intentionally
does not add the stale extra package named in the earlier plan draft. There are
no `openai`, `anthropic`, `pydantic-core`, or `jiter` distributions.

## Embedded interpreter availability

- Android: [Chaquopy 17.0's version matrix](https://chaquo.com/chaquopy/doc/current/versions.html)
  supports Python 3.10 through 3.14. Python 3.12 is selected for the feasibility
  build and is inside this package's `>=3.11,<3.14` range.
- iOS: [Python-Apple-support](https://github.com/beeware/Python-Apple-support)
  provides maintained Python 3.11, 3.12, and 3.13 branches and device/simulator
  XCFrameworks. Python 3.12 is selected. CPython documents iOS as an embedded,
  app-bundled runtime in [Using Python on iOS](https://docs.python.org/3/using/ios.html).

## Signed engineering budgets

These are stop gates for Stages 2 and 3. The Python-only items are measured
here; native-host items must be measured on release builds and physical arm64
devices before either feasibility stage can pass.

| Budget | Limit | Stage 0/1 result |
|---|---:|---|
| Pure-Python dependency wheels | <= 0.75 MiB compressed | PASS: 0.529 MiB |
| `hermes-mobile-core` wheel | <= 0.25 MiB compressed | PASS: 0.047 MiB |
| Total Python package closure excluding interpreter/stdlib | <= 1.00 MiB compressed | PASS: 0.576 MiB |
| Android release AAB download delta per delivered ABI | <= 18 MiB | Stage 2 measurement gate |
| iOS archived IPA download delta | <= 25 MiB | Stage 3 measurement gate |
| Cold embedded-interpreter start | <= 750 ms p95 | Stage 2/3 physical-device gate |
| First-token bridge overhead (provider/network time excluded) | <= 150 ms p95 | Stage 2/3 physical-device gate |
| Steady-state resident-memory delta during a 4k-token stream | <= 50 MiB | Stage 2/3 physical-device gate |
| One-hour long-stream energy overhead above network-only baseline | <= 5 percentage points | Stage 2/3 physical-device gate |

No-go conditions remain: any native/provider-SDK wheel entering the resolved
closure, any import-gate violation, a non-`py3-none-any` core wheel, or any
native feasibility measurement exceeding the budgets above.
