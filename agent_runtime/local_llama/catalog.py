"""Bounded GGUF metadata discovery; never map or read model tensors."""
from __future__ import annotations

from copy import deepcopy
import os
import re
from pathlib import Path
import struct
import time
import uuid

from .config import LOAD_DEFAULTS, GENERATION_DEFAULTS, LocalLlamaError


def metadata(path: Path) -> dict:
    with path.open("rb") as stream:
        budget = 32 * 1024 * 1024
        def read(n):
            if n < 0 or stream.tell() + n > budget:
                raise LocalLlamaError("unsupported_model", "GGUF metadata exceeds the bounded reader")
            value = stream.read(n)
            if len(value) != n:
                raise LocalLlamaError("incomplete_model", "GGUF metadata is truncated")
            return value
        def number(fmt):
            return struct.unpack("<" + fmt, read(struct.calcsize(fmt)))[0]
        def string():
            return read(number("Q")).decode("utf-8")
        def value(kind, depth=0):
            if depth > 2:
                raise LocalLlamaError("unsupported_model", "Nested GGUF metadata exceeds reader limit")
            formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
                       6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
            if kind in formats:
                return number(formats[kind])
            if kind == 8:
                return string()
            if kind == 9:
                element, count = number("I"), number("Q")
                if count > 1000000:
                    raise LocalLlamaError("unsupported_model", "GGUF array is too large")
                for _ in range(count):
                    value(element, depth + 1)
                return None
            raise LocalLlamaError("unsupported_model", "Unknown GGUF metadata type")
        if read(4) != b"GGUF" or number("I") not in (2, 3):
            raise LocalLlamaError("unsupported_model", "Not a supported GGUF file")
        tensors, count = number("Q"), number("Q")
        if count > 100000 or tensors == 0:
            raise LocalLlamaError("unsupported_model", "Invalid GGUF header")
        result = {}
        for _ in range(count):
            key = string()
            item = value(number("I"))
            if key in ("general.name", "general.architecture", "split.no", "split.count") or key.endswith(".context_length"):
                result[key] = item
        architecture = result.get("general.architecture")
        if not isinstance(architecture, str) or not architecture or architecture in ("clip", "vision"):
            raise LocalLlamaError("unsupported_model", "GGUF has no supported text architecture")
        if "general.name" in result and not isinstance(result["general.name"], str):
            raise LocalLlamaError("unsupported_model", "Invalid GGUF model name")
        for key in ("split.count", "split.no"):
            if key in result and (type(result[key]) is not int or result[key] < 0 or result[key] > 1000):
                raise LocalLlamaError("incomplete_model", "Invalid GGUF shard metadata")
        return result


def validate_model(path: Path):
    try:
        info = metadata(path)
    except OSError as exc:
        raise LocalLlamaError("missing_file", "The saved model is missing or unreadable") from exc
    except UnicodeError as exc:
        raise LocalLlamaError("unsupported_model", "GGUF metadata contains invalid text") from exc
    count = info.get("split.count", 1)
    if count > 1:
        match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", path.name, re.IGNORECASE)
        if not match or int(match[3]) != count or info.get("split.no") != 0 or int(match[2]) != 1:
            raise LocalLlamaError("incomplete_model", "Select the first file of a complete GGUF shard set")
        for index in range(count):
            shard = path.with_name(f"{match[1]}-{index + 1:05d}-of-{count:05d}.gguf")
            try:
                part = metadata(shard)
            except OSError as exc:
                raise LocalLlamaError("incomplete_model", "A GGUF shard is missing or unreadable") from exc
            if any(part.get(k) != info.get(k) for k in ("general.name", "general.architecture", "split.count")) or part.get("split.no") != index:
                raise LocalLlamaError("incomplete_model", "GGUF shards do not belong to the same model")
    return info


def scan(config: dict, *, max_candidates=10000, timeout=60):
    result = deepcopy(config)
    existing = {os.path.normcase(str(Path(p["gguf_path"]).resolve())): p for p in result["presets"]}
    candidates, errors, truncated = [], [], False
    deadline = time.monotonic() + timeout
    for root in config["model_roots"]:
        for parent, directories, files in os.walk(root, followlinks=False,
                onerror=lambda exc: errors.append({"path": exc.filename, "reason": "unreadable_directory"})):
            directories[:] = [d for d in directories if not Path(parent, d).is_symlink()
                               and not Path(parent, d).is_junction()]
            if time.monotonic() > deadline:
                truncated = True
                break
            for name in sorted(files):
                path = Path(parent, name)
                if path.suffix.lower() != ".gguf" or name.lower().startswith("mmproj") or path.is_symlink():
                    continue
                if len(candidates) >= max_candidates or time.monotonic() > deadline:
                    truncated = True
                    break
                candidates.append(path.resolve())
            if truncated:
                break
        if truncated:
            break
    split_groups = {}
    singles = []
    for path in candidates:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            info = metadata(path)
            if info.get("general.architecture") in ("clip", "vision"):
                continue
            if info.get("split.count", 1) > 1:
                # Shards must be adjacent and agree on name, architecture, count.
                match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", path.name, re.IGNORECASE)
                if not match:
                    raise LocalLlamaError("incomplete_model", "Unrecognized GGUF shard filename")
                key = (str(path.parent), match[1], info.get("general.name"), info.get("general.architecture"), info["split.count"])
                split_groups.setdefault(key, []).append((path, info))
            else:
                singles.append((path, info))
        except (LocalLlamaError, OSError, UnicodeError) as exc:
            errors.append({"path": str(path), "reason": getattr(exc, "reason", "unreadable_model")})
    for key, group in split_groups.items():
        if len(group) != key[-1] or {info.get("split.no") for _, info in group} != set(range(key[-1])):
            errors.append({"path": key[0], "reason": "incomplete_model"})
        else:
            singles.append(next(row for row in group if row[1]["split.no"] == 0))
    for path, info in singles:
        canonical = os.path.normcase(str(path))
        if canonical not in existing:
            preset = {"model_id": str(uuid.uuid4()), "display_name": (info.get("general.name") or path.parent.name)[:120],
                      "gguf_path": str(path), "revision": 0, "load": deepcopy(LOAD_DEFAULTS),
                      "generation": deepcopy(GENERATION_DEFAULTS)}
            contexts = [v for k, v in info.items() if k.endswith(".context_length") and type(v) is int]
            if contexts:
                preset["load"]["context_size"] = min(32768, min(contexts))
                preset["generation"]["max_output_tokens"] = min(4096, preset["load"]["context_size"] // 2)
            result["presets"].append(preset)
            existing[canonical] = preset
    return result, {"candidates": len(candidates), "errors": errors, "truncated": truncated}
