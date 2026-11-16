#!/usr/bin/env python3
"""Southbound 10-Second Full-Blooded Edge Hand Bridge for Causal Store v2.

Mounts any local or deployed capability — HTTP web apps (e.g. "全球羊毛情报中心",
DSH HTTP services), arbitrary local CLI binaries/scripts, in-process Python
callables, or local MCP stdio servers — onto Causal Store v2 as a live
``async_poll`` hand over pure outbound HTTP(S), with zero port forwarding,
zero public IP, and zero ``python3 -I`` AST handcuffs.

Pure Python 3.12 standard library only (zero third-party imports).
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
import http.client
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

BRIDGE_VERSION = "1.0.0"
DEFAULT_STORE_URL = "https://store-v2.206.189.67.53.sslip.io"
DEFAULT_SEAT_KEY_PATH = Path.home() / ".causal-store-v2" / "edge_seat_key"
DEFAULT_GATE = "03 设备"
MAX_FULFILL_BODY_BYTES = 220 * 1024  # Safely under store_v2 server.py 256 KiB limit
MAX_UPSTREAM_READ_BYTES = 2 * 1024 * 1024

# Outbound secret & credential redaction patterns (compiled once).
_TOK_PREFIX = "sk2" + "_"
_RE_SK2_TOKEN = re.compile(_TOK_PREFIX + r"[A-Za-z0-9_-]{20,}")
_RE_SK_TOKEN = re.compile("sk" + r"-[A-Za-z0-9_-]{20,}")
_RE_GHP_TOKEN = re.compile(r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_RE_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=:-]{8,}")
_RE_PRIVATE_KEY = re.compile(
    "-----BEGIN [A-Z0-9 ]*PRIV" + r"ATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIV" + "ATE KEY-----"
)
_RE_ARGV_RAW_TOKEN = re.compile("^(?:" + _TOK_PREFIX + r"[A-Za-z0-9_-]{16,}|sk" + r"-[A-Za-z0-9_-]{20,}|gh" + r"p_[A-Za-z0-9]{20,})$")
_RE_SLUG_CLEAN = re.compile(r"[^A-Za-z0-9._-]+")
_INVALID_TOKEN_SENTINELS = frozenset(
    {
        "",
        "0",
        "false",
        "none",
        "null",
        "undefined",
        "redacted",
        "[redacted]",
        "[redacted:key]",
        "your_api_key",
        "replace_me",
        "changeme",
    }
)


def _normalize_store_token(raw: str | None) -> str:
    """Normalize a store bearer token and reject unexpanded template/sentinel placeholders."""
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
        cleaned = cleaned[1:-1].strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    if not cleaned:
        return ""
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")) or any(ch.isspace() for ch in cleaned):
        return ""
    lower = cleaned.lower()
    if lower in _INVALID_TOKEN_SENTINELS or lower.startswith("[redacted"):
        return ""
    if (cleaned.startswith("${") and cleaned.endswith("}")) or (cleaned.startswith("<") and cleaned.endswith(">")):
        return ""
    if cleaned.startswith("$") and len(cleaned) > 1 and cleaned[1:].replace("_", "").isalnum():
        return ""
    if "${" in cleaned:
        return ""
    return cleaned


def _read_store_key_file(path: Path) -> str:
    """Read a store bearer token from a plain-text or JSON 0600 key file."""
    resolved = path.expanduser()
    if not resolved.is_file():
        return ""
    raw = resolved.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(obj, dict):
            legacy_field = _TOK_PREFIX + "key"
            for field_name in ("token", "api_key", "seat_key", "seller_token", "buyer_key", "buyer_token", legacy_field):
                val = obj.get(field_name)
                if isinstance(val, str):
                    norm = _normalize_store_token(val)
                    if norm:
                        return norm
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return _normalize_store_token(stripped)
    return ""


def _open_url_with_proxy_fallback(
    req: urllib.request.Request,
    *,
    timeout: float,
    ctx: ssl.SSLContext | None = None,
) -> Any:
    """Open URL with automatic ProxyHandler({}) fallback on dead local proxy env vars."""
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as err:
        reason_txt = str(err).lower()
        has_proxy = any(
            os.environ.get(k, "").strip()
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        ) or bool(urllib.request.getproxies())
        is_proxy_err = (
            has_proxy
            or isinstance(getattr(err, "reason", None), ConnectionRefusedError)
            or "connection refused" in reason_txt
            or "tunnel connection failed" in reason_txt
            or "proxy" in reason_txt
        )
        if not is_proxy_err:
            raise
        fresh_req = urllib.request.Request(
            req.full_url,
            data=req.data,
            headers=dict(req.header_items()),
            method=req.get_method(),
        )
        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        opener = urllib.request.build_opener(*handlers)
        return opener.open(fresh_req, timeout=timeout)


class BridgeError(RuntimeError):
    """Operational or configuration error in bazaar_edge_hand."""


class _PollInterrupted(BaseException):
    """Internal wakeup raised inside signal handler to break blocking poll immediately."""


def _log(msg: str, *, stream: Any = None) -> None:
    out = stream if stream is not None else sys.stderr
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        out.write(f"[bazaar-edge-hand {ts}] {msg}\n")
        out.flush()
    except OSError:
        pass


def sanitize_slug(raw: str, *, fallback: str = "edge_item") -> str:
    cleaned = _RE_SLUG_CLEAN.sub("_", (raw or "").strip()).strip("._-")
    return (cleaned or fallback)[:120]


def redact_outbound_secrets(
    value: Any,
    *,
    extra_secrets: Sequence[str] = (),
    _depth: int = 0,
) -> Any:
    """Recursively scrub accidental API keys, tokens, and Bearer headers before fulfill."""
    if _depth > 32:
        return "[TRUNCATED_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return redact_outbound_secrets(
            value.decode("utf-8", errors="replace"),
            extra_secrets=extra_secrets,
            _depth=_depth + 1,
        )
    if isinstance(value, str):
        scrubbed = value
        for secret in extra_secrets:
            if secret and len(secret) >= 8 and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[REDACTED:SEAT_KEY]")
        scrubbed = _RE_PRIVATE_KEY.sub("[REDACTED:PRIVATE_KEY]", scrubbed)
        scrubbed = _RE_BEARER_TOKEN.sub("Bearer [REDACTED]", scrubbed)
        scrubbed = _RE_SK2_TOKEN.sub("[REDACTED:SK2_KEY]", scrubbed)
        scrubbed = _RE_SK_TOKEN.sub("[REDACTED:SK_KEY]", scrubbed)
        scrubbed = _RE_GHP_TOKEN.sub("[REDACTED:GITHUB_TOKEN]", scrubbed)
        return scrubbed
    if isinstance(value, dict):
        return {
            str(redact_outbound_secrets(str(k), extra_secrets=extra_secrets, _depth=_depth + 1)): redact_outbound_secrets(
                v, extra_secrets=extra_secrets, _depth=_depth + 1
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_outbound_secrets(item, extra_secrets=extra_secrets, _depth=_depth + 1) for item in value]
    return redact_outbound_secrets(str(value), extra_secrets=extra_secrets, _depth=_depth + 1)


def scrub_cli_argv_tokens(argv: list[str], *, log_stream: Any = None) -> tuple[str, list[str]]:
    """Detect, warn on, and scrub raw tokens passed on CLI argv so ps does not leak them."""
    extracted_token = ""
    cleaned = list(argv)
    idx = 0
    while idx < len(cleaned):
        arg = cleaned[idx]
        if arg in ("--api-key", "--token") and idx + 1 < len(cleaned):
            extracted_token = cleaned[idx + 1].strip()
            cleaned[idx + 1] = "REDACTED"
            _log(
                "SECURITY WARNING: Passing raw API token on CLI argv exposes it in process listings (ps). "
                "Prefer BAZAAR_API_KEY env var or --key-file <path>. Scrubbed argv in-place.",
                stream=log_stream,
            )
            idx += 2
            continue
        if arg.startswith(("--api-key=", "--token=")):
            opt, _, val = arg.partition("=")
            extracted_token = val.strip()
            cleaned[idx] = f"{opt}=REDACTED"
            _log(
                "SECURITY WARNING: Passing raw API token on CLI argv exposes it in process listings (ps). "
                "Prefer BAZAAR_API_KEY env var or --key-file <path>. Scrubbed argv in-place.",
                stream=log_stream,
            )
            idx += 1
            continue
        if _RE_ARGV_RAW_TOKEN.match(arg):
            if not extracted_token:
                extracted_token = arg.strip()
            cleaned[idx] = "REDACTED"
            _log(
                "SECURITY WARNING: Raw token pattern detected in CLI argv; scrubbed in-place.",
                stream=log_stream,
            )
        idx += 1
    return extracted_token, cleaned


def save_key_0600(path: Path, token: str) -> Path:
    """Atomically persist a store seat key with strict 0600 file permissions."""
    target = path.expanduser()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".edge_key_", dir=str(parent))
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token.strip() + "\n")
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _spec_to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False, separators=(",", ":"))


@dataclass
class CardHandSpec:
    """Registration metadata for the Edge Hand and its bound Capability Card."""

    hand_id: str = ""
    card_id: str = ""
    title: str = ""
    price_micros: int = 0
    gate: str = DEFAULT_GATE
    inputs_spec: str = ""
    outputs_spec: str = ""
    mechanism: str = ""
    preconditions: str = ""
    side_effects: str = ""
    presence_note: str = "Edge hand online (async_poll)"
    away_note: str = "Edge hand offline (graceful shutdown)"
    is_public: bool = True
    skip_card: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.price_micros, bool) or not isinstance(self.price_micros, int) or self.price_micros < 0:
            raise BridgeError(f"price_micros must be a non-negative integer, got {self.price_micros!r}")

    @classmethod
    def from_manifest_and_args(
        cls,
        manifest: dict[str, Any] | None = None,
        args: argparse.Namespace | None = None,
        *,
        default_slug: str = "edge_capability",
    ) -> "CardHandSpec":
        m = dict(manifest or {})
        card_section = m.get("card") if isinstance(m.get("card"), dict) else {}
        hand_section = m.get("hand") if isinstance(m.get("hand"), dict) else {}

        def pick(attr: str, *keys: str, default: Any = "") -> Any:
            if args is not None and getattr(args, attr, None) is not None:
                return getattr(args, attr)
            for k in keys:
                if k in m and m[k] is not None:
                    return m[k]
                if k in card_section and card_section[k] is not None:
                    return card_section[k]
                if k in hand_section and hand_section[k] is not None:
                    return hand_section[k]
            return default

        raw_card_id = str(pick("card_id", "card_id", default="")).strip()
        raw_hand_id = str(pick("hand_id", "hand_id", default="")).strip()
        skip_card = bool(getattr(args, "skip_card", False)) if args is not None else bool(m.get("skip_card", False))

        if not raw_card_id and not skip_card:
            raw_card_id = sanitize_slug(default_slug, fallback="edge_card")
        if not raw_hand_id:
            base = raw_card_id or default_slug
            raw_hand_id = sanitize_slug(
                base if base.startswith("hand_") else f"hand_{base}",
                fallback="hand_edge_default",
            )

        title = str(pick("title", "title", "door", default="")).strip()
        if not title and not skip_card:
            title = f"Edge Capability ({raw_card_id})"

        price_raw = pick("price_micros", "price_micros", default=0)
        if isinstance(price_raw, bool):
            raise BridgeError(f"price_micros must be a non-negative integer, got {price_raw!r}")
        try:
            price_micros = int(price_raw)
        except (TypeError, ValueError) as exc:
            raise BridgeError(f"price_micros must be a non-negative integer, got {price_raw!r}") from exc
        if price_micros < 0:
            raise BridgeError("price_micros must be >= 0")

        gate = str(pick("gate", "gate", default=DEFAULT_GATE)).strip() or DEFAULT_GATE
        inputs_spec = _spec_to_str(pick("inputs_spec", "inputs_spec", "inputs", default="JSON object"))
        outputs_spec = _spec_to_str(
            pick(
                "outputs_spec",
                "outputs_spec",
                "outputs",
                default=(
                    "Structured JSON response from live edge execution; "
                    "on timeout outcome='unknown', replay with identical idem_key to reconcile."
                ),
            )
        )
        mechanism = _spec_to_str(
            pick("mechanism", "mechanism", default="Outbound async_poll edge bridge via bazaar_edge_hand.py")
        )
        preconditions = _spec_to_str(pick("preconditions", "preconditions", default="Edge bridge process is online (in)"))
        side_effects = _spec_to_str(pick("side_effects", "side_effects", default="Executes configured edge target"))
        presence_note = str(
            pick("presence_note", "presence_note", default="Edge hand online (async_poll)")
        ).strip() or "Edge hand online (async_poll)"
        away_note = str(
            pick("away_note", "away_note", default="Edge hand offline (graceful shutdown)")
        ).strip() or "Edge hand offline (graceful shutdown)"

        if args is not None and getattr(args, "private", False):
            is_public = False
        else:
            is_public = bool(m.get("is_public", card_section.get("is_public", True)))

        return cls(
            hand_id=raw_hand_id,
            card_id=raw_card_id,
            title=title,
            price_micros=price_micros,
            gate=gate,
            inputs_spec=inputs_spec,
            outputs_spec=outputs_spec,
            mechanism=mechanism,
            preconditions=preconditions,
            side_effects=side_effects,
            presence_note=presence_note,
            away_note=away_note,
            is_public=is_public,
            skip_card=skip_card,
        )


@dataclass
class ExecutionResult:
    """Normalized adapter execution outcome prior to outbound secret redaction."""

    outcome: str  # "ok" | "fail" | "unknown"
    output: Any
    did: str = ""
    pitfall_note: str = ""
    spend_micros: int = 0
    paid_micros: int | None = None


def _parse_opt_nonneg_int(raw: Any) -> int | None:
    """Parse an optional non-negative integer (ignoring bools and invalid strings)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return max(0, int(s))
        except ValueError:
            return None
    return None


def _extract_opt_paid_micros(mapping: dict[str, Any], *, header_fallback: str = "") -> int | None:
    if "paid_micros" in mapping:
        parsed = _parse_opt_nonneg_int(mapping.get("paid_micros"))
        if parsed is not None:
            return parsed
    if header_fallback:
        return _parse_opt_nonneg_int(header_fallback)
    return None


def _extract_spend_micros(mapping: dict[str, Any], *, header_fallback: str = "") -> int:
    if "spend_micros" in mapping:
        parsed = _parse_opt_nonneg_int(mapping.get("spend_micros"))
        if parsed is not None:
            return parsed
    if header_fallback:
        parsed = _parse_opt_nonneg_int(header_fallback)
        if parsed is not None:
            return parsed
    return 0


# ============================================================================
# 4 Plug-and-Play Full-Blooded Execution Adapters (Zero AST Handcuffs)
# ============================================================================


class EdgeAdapter:
    """Base interface for local/edge execution adapters."""

    name: str = "base"

    def execute(self, inputs: dict[str, Any], event_meta: dict[str, Any]) -> ExecutionResult:
        raise NotImplementedError

    def close(self) -> None:
        return


class HttpUpstreamAdapter(EdgeAdapter):
    """Adapter A (--http-upstream <URL>): forwards inputs JSON via POST or GET."""

    name = "http_upstream"

    def __init__(
        self,
        upstream_url: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        timeout_sec: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        url = (upstream_url or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BridgeError(f"Invalid --http-upstream URL: {upstream_url!r}")
        m = (method or "POST").strip().upper()
        if m not in ("POST", "GET"):
            raise BridgeError(f"--http-method must be POST or GET, got {method!r}")
        self.upstream_url = url
        self.method = m
        self.headers = dict(headers or {})
        self.timeout_sec = max(0.1, float(timeout_sec))
        self.ssl_context = ssl_context or ssl.create_default_context()

    def _build_get_url(self, inputs: dict[str, Any]) -> str:
        if not inputs:
            return self.upstream_url
        parsed = urllib.parse.urlsplit(self.upstream_url)
        existing_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        for k, v in inputs.items():
            if v is None:
                continue
            if isinstance(v, bool):
                existing_pairs.append((str(k), "true" if v else "false"))
            elif isinstance(v, (int, float, str)):
                existing_pairs.append((str(k), str(v)))
            else:
                existing_pairs.append((str(k), json.dumps(v, ensure_ascii=False, separators=(",", ":"))))
        new_query = urllib.parse.urlencode(existing_pairs)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

    def execute(self, inputs: dict[str, Any], event_meta: dict[str, Any]) -> ExecutionResult:
        hdrs = {
            "Accept": "application/json",
            "User-Agent": f"bazaar-edge-hand/{BRIDGE_VERSION}",
            "X-Causal-Run-Id": str(event_meta.get("run_id") or ""),
            "X-Causal-Card-Id": str(event_meta.get("card_id") or ""),
            "X-Causal-Hand-Id": str(event_meta.get("hand_id") or ""),
        }
        hdrs.update(self.headers)
        if self.method == "GET":
            target_url = self._build_get_url(inputs)
            body_bytes = None
        else:
            target_url = self.upstream_url
            body_bytes = json.dumps(inputs, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")

        req = urllib.request.Request(target_url, data=body_bytes, headers=hdrs, method=self.method)
        ctx = self.ssl_context if target_url.startswith("https://") else None
        try:
            with _open_url_with_proxy_fallback(req, timeout=self.timeout_sec, ctx=ctx) as resp:
                status = int(resp.status)
                ctype = resp.headers.get("Content-Type", "")
                hdr_paid = resp.headers.get("X-Causal-Paid-Micros") or resp.headers.get("X-Paid-Micros") or ""
                hdr_spend = resp.headers.get("X-Actual-Cost-Micros") or ""
                raw = resp.read(MAX_UPSTREAM_READ_BYTES + 1)
        except urllib.error.HTTPError as err:
            try:
                err_raw = err.read(65536).decode("utf-8", errors="replace") if err.fp else ""
            finally:
                err.close()
            try:
                err_parsed = json.loads(err_raw) if err_raw.strip() else {"error": str(err)}
            except ValueError:
                err_parsed = {"error": err_raw[:2000] or str(err)}
            out_err = err_parsed if isinstance(err_parsed, dict) else {"error": err_parsed}
            out_err.setdefault("ok", False)
            out_err.setdefault("http_status", err.code)
            return ExecutionResult(
                outcome="fail",
                output=out_err,
                did=f"upstream HTTP {self.method} returned {err.code}",
                pitfall_note=f"http_upstream {err.code}",
            )
        except Exception as exc:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                did=f"upstream HTTP {self.method} error: {type(exc).__name__}",
                pitfall_note=f"{type(exc).__name__}: {exc}"[:240],
            )

        if len(raw) > MAX_UPSTREAM_READ_BYTES:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": "upstream response exceeded 2 MiB safety cap"},
                did="upstream response exceeded size cap",
                pitfall_note="upstream response > 2 MiB",
            )

        text = raw.decode("utf-8", errors="replace")
        stripped = text.strip()
        if stripped:
            try:
                parsed_json = json.loads(stripped)
            except ValueError:
                parsed_json = {"body": text, "content_type": ctype, "http_status": status}
        else:
            parsed_json = {"http_status": status}

        if isinstance(parsed_json, dict):
            spend_m = _extract_spend_micros(parsed_json, header_fallback=hdr_spend)
            if parsed_json.get("outcome") == "fail" or parsed_json.get("ok") is False:
                return ExecutionResult(
                    outcome="fail",
                    output=parsed_json,
                    did=str(parsed_json.get("did") or f"upstream HTTP {status} reported ok=False"),
                    pitfall_note=str(parsed_json.get("pitfall_note") or parsed_json.get("error") or "")[:240],
                    spend_micros=spend_m,
                )
            return ExecutionResult(
                outcome="ok",
                output=parsed_json,
                did=str(parsed_json.get("did") or f"HTTP {self.method} {status} ok"),
                spend_micros=spend_m,
                paid_micros=_extract_opt_paid_micros(parsed_json, header_fallback=hdr_paid),
            )
        hdr_spend_int = _parse_opt_nonneg_int(hdr_spend) or 0
        hdr_paid_int = _parse_opt_nonneg_int(hdr_paid)
        if isinstance(parsed_json, list):
            return ExecutionResult(
                outcome="ok",
                output={"items": parsed_json, "count": len(parsed_json)},
                did=f"HTTP {self.method} {status} ok ({len(parsed_json)} items)",
                spend_micros=hdr_spend_int,
                paid_micros=hdr_paid_int,
            )
        return ExecutionResult(
            outcome="ok",
            output={"result": parsed_json},
            did=f"HTTP {self.method} {status} ok",
            spend_micros=hdr_spend_int,
            paid_micros=hdr_paid_int,
        )


def _child_env() -> dict[str, str]:
    """Build subprocess environment stripping bridge seat, session, and founder credentials."""
    env = dict(os.environ)
    for var_name in (
        "BAZAAR_API_KEY",
        "CAUSAL_STORE_API_KEY",
        "BAZAAR_SELLER_KEY",
        "BAZAAR_EDGE_SEAT_KEY",
        "BAZAAR_FOUNDER_TOKEN",
        "BAZAAR_ACCOUNT_SESSION_TOKEN",
        "BAZAAR_REGISTRAR_ADMIN_KEY",
        "STORE_BOOTSTRAP_KEY",
    ):
        env.pop(var_name, None)
    return env


def _close_proc_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        else:  # pragma: no cover
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
    _close_proc_pipes(proc)


class CommandAdapter(EdgeAdapter):
    """Adapter B (--cmd "<command>"): pipes inputs JSON on stdin, reads JSON or plain text from stdout."""

    name = "cmd"

    def __init__(self, command: str, *, timeout_sec: float = 30.0, cwd: str | Path | None = None) -> None:
        cmd = (command or "").strip()
        if not cmd:
            raise BridgeError("--cmd cannot be empty")
        self.command = cmd
        self.timeout_sec = max(0.05, float(timeout_sec))
        self.cwd = str(cwd) if cwd is not None else None
        self._proc_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen[bytes]] = set()

    def close(self) -> None:
        with self._proc_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()
        for p in procs:
            _terminate_process_group(p)

    def execute(self, inputs: dict[str, Any], event_meta: dict[str, Any]) -> ExecutionResult:
        stdin_bytes = json.dumps(inputs, ensure_ascii=False).encode("utf-8")
        env = _child_env()
        env["CAUSAL_RUN_ID"] = str(event_meta.get("run_id") or "")
        env["CAUSAL_CARD_ID"] = str(event_meta.get("card_id") or "")
        env["CAUSAL_HAND_ID"] = str(event_meta.get("hand_id") or "")

        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", self.command],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"spawn failed: {type(exc).__name__}: {exc}"},
                did="command spawn failed",
                pitfall_note=str(exc)[:240],
            )

        with self._proc_lock:
            self._active_procs.add(proc)
        try:
            out_bytes, err_bytes = proc.communicate(input=stdin_bytes, timeout=self.timeout_sec)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"command timed out after {self.timeout_sec}s"},
                did=f"command timeout ({self.timeout_sec}s)",
                pitfall_note=f"command exceeded exec_timeout={self.timeout_sec}s",
            )
        except Exception as exc:
            _terminate_process_group(proc)
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                did=f"command error: {type(exc).__name__}",
                pitfall_note=str(exc)[:240],
            )
        finally:
            with self._proc_lock:
                self._active_procs.discard(proc)
            _close_proc_pipes(proc)

        rc = int(proc.returncode or 0)
        stdout_text = out_bytes.decode("utf-8", errors="replace")
        stderr_text = err_bytes.decode("utf-8", errors="replace").strip()

        if rc != 0:
            err_payload: dict[str, Any] = {"ok": False, "returncode": rc}
            if stderr_text:
                err_payload["stderr"] = stderr_text[:4000]
            if stdout_text.strip():
                try:
                    parsed_err = json.loads(stdout_text.strip())
                    if isinstance(parsed_err, dict):
                        err_payload.update(parsed_err)
                        err_payload["ok"] = False
                        err_payload["returncode"] = rc
                    else:
                        err_payload["stdout"] = stdout_text.rstrip("\r\n")[:4000]
                except ValueError:
                    err_payload["stdout"] = stdout_text.rstrip("\r\n")[:4000]
            err_payload.setdefault("error", stderr_text[:400] or f"command exited with code {rc}")
            return ExecutionResult(
                outcome="fail",
                output=err_payload,
                did=f"command exited with code {rc}",
                pitfall_note=(stderr_text or f"exit code {rc}")[:240],
            )

        stripped = stdout_text.strip()
        if not stripped:
            return ExecutionResult(outcome="ok", output={"stdout": ""}, did="command exited 0")

        try:
            parsed = json.loads(stripped)
        except ValueError:
            return ExecutionResult(
                outcome="ok",
                output={"stdout": stdout_text.rstrip("\r\n")},
                did="command exited 0 (plain text stdout)",
            )

        if isinstance(parsed, dict):
            spend_m = _extract_spend_micros(parsed)
            if parsed.get("outcome") == "fail" or parsed.get("ok") is False:
                return ExecutionResult(
                    outcome="fail",
                    output=parsed,
                    did=str(parsed.get("did") or "command returned ok=False"),
                    pitfall_note=str(parsed.get("pitfall_note") or parsed.get("error") or "")[:240],
                    spend_micros=spend_m,
                )
            return ExecutionResult(
                outcome="ok",
                output=parsed,
                did=str(parsed.get("did") or "command exited 0"),
                spend_micros=spend_m,
                paid_micros=_extract_opt_paid_micros(parsed),
            )
        if isinstance(parsed, str):
            return ExecutionResult(outcome="ok", output={"stdout": parsed}, did="command exited 0")
        return ExecutionResult(outcome="ok", output={"result": parsed}, did="command exited 0")


def load_python_callable(target_spec: str) -> Callable[[dict[str, Any]], Any]:
    """Load `<file_or_module>:<callable>` in-process."""
    spec_str = (target_spec or "").strip()
    mod_ref, sep, attr_chain = spec_str.rpartition(":")
    if not sep or not mod_ref.strip() or not attr_chain.strip():
        raise BridgeError(
            f"Invalid --python-target {target_spec!r}; expected '<file_or_module>:<callable>'"
        )
    mod_ref = mod_ref.strip()
    attr_chain = attr_chain.strip()

    candidate_path = Path(mod_ref).expanduser()
    looks_like_file = (
        mod_ref.endswith(".py")
        or os.sep in mod_ref
        or (os.altsep and os.altsep in mod_ref)
        or candidate_path.is_file()
    )
    if looks_like_file:
        resolved = candidate_path.resolve()
        if not resolved.is_file():
            raise BridgeError(f"Python target file not found: {resolved}")
        mod_name = f"_bazaar_edge_target_{ abs(hash(str(resolved))) & 0xFFFFFFFF:08x}"
        spec = importlib.util.spec_from_file_location(mod_name, str(resolved))
        if spec is None or spec.loader is None:
            raise BridgeError(f"Cannot create module spec from {resolved}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        parent_dir = str(resolved.parent)
        added_path = False
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            added_path = True
        try:
            spec.loader.exec_module(module)
        finally:
            if added_path:
                try:
                    sys.path.remove(parent_dir)
                except ValueError:
                    pass
    else:
        try:
            module = importlib.import_module(mod_ref)
        except Exception as exc:
            raise BridgeError(f"Failed to import module {mod_ref!r}: {exc}") from exc

    obj: Any = module
    for part in attr_chain.split("."):
        if not hasattr(obj, part):
            raise BridgeError(f"Target attribute {attr_chain!r} not found in {mod_ref!r}")
        obj = getattr(obj, part)
    if not callable(obj):
        raise BridgeError(f"Target {target_spec!r} is not callable")
    return obj


class PythonTargetAdapter(EdgeAdapter):
    """Adapter C (--python-target "<file_or_module>:<callable>"): invokes fn(inputs) -> dict in-process."""

    name = "python_target"

    def __init__(
        self,
        target: str | Callable[[dict[str, Any]], Any],
        *,
        timeout_sec: float = 30.0,
    ) -> None:
        if callable(target):
            self.fn = target
            self.target_label = getattr(target, "__qualname__", getattr(target, "__name__", "callable"))
        else:
            self.fn = load_python_callable(target)
            self.target_label = target
        self.timeout_sec = max(0.05, float(timeout_sec))
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="edge-py-target")

    def _call_fn(self, inputs: dict[str, Any]) -> Any:
        res = self.fn(dict(inputs))
        if inspect.isawaitable(res):
            return asyncio.run(res)
        return res

    def execute(self, inputs: dict[str, Any], _event_meta: dict[str, Any]) -> ExecutionResult:
        fut = self._executor.submit(self._call_fn, inputs)
        try:
            res = fut.result(timeout=self.timeout_sec)
        except FuturesTimeoutError:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"python target timed out after {self.timeout_sec}s"},
                did=f"python target timeout ({self.timeout_sec}s)",
                pitfall_note=f"python target exceeded exec_timeout={self.timeout_sec}s",
            )
        except Exception as exc:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                did=f"python target raised {type(exc).__name__}",
                pitfall_note=f"{type(exc).__name__}: {exc}"[:240],
            )

        if isinstance(res, ExecutionResult):
            return res
        if isinstance(res, dict):
            spend_m = _extract_spend_micros(res)
            if res.get("outcome") == "fail" or res.get("ok") is False:
                return ExecutionResult(
                    outcome="fail",
                    output=res,
                    did=str(res.get("did") or f"{self.target_label} returned ok=False"),
                    pitfall_note=str(res.get("pitfall_note") or res.get("error") or "")[:240],
                    spend_micros=spend_m,
                )
            return ExecutionResult(
                outcome="ok",
                output=res,
                did=str(res.get("did") or f"invoked {self.target_label}"),
                spend_micros=spend_m,
                paid_micros=_extract_opt_paid_micros(res),
            )
        return ExecutionResult(
            outcome="ok",
            output={"result": res},
            did=f"invoked {self.target_label}",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class McpStdioAdapter(EdgeAdapter):
    """Adapter D (--mcp-stdio "<command>" --mcp-tool "<tool_name>"): bridges a local MCP stdio tool."""

    name = "mcp_stdio"

    def __init__(
        self,
        command: str,
        tool_name: str,
        *,
        timeout_sec: float = 30.0,
        cwd: str | Path | None = None,
    ) -> None:
        cmd = (command or "").strip()
        tool = (tool_name or "").strip()
        if not cmd:
            raise BridgeError("--mcp-stdio command cannot be empty")
        if not tool:
            raise BridgeError("--mcp-tool <tool_name> is required with --mcp-stdio")
        self.command = cmd
        self.tool_name = tool
        self.timeout_sec = max(0.1, float(timeout_sec))
        self.cwd = str(cwd) if cwd is not None else None

    @staticmethod
    def _send_jsonrpc(stdin_pipe: Any, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        stdin_pipe.write(line)
        stdin_pipe.flush()

    @staticmethod
    def _read_jsonrpc_for_id(stdout_pipe: Any, target_id: int) -> dict[str, Any]:
        """Read newline-delimited (or Content-Length framed) MCP JSON-RPC until matching id."""
        while True:
            raw_line = stdout_pipe.readline()
            if not raw_line:
                raise EOFError("MCP server closed stdout before returning JSON-RPC response")
            if raw_line.lower().startswith(b"content-length:"):
                length = int(raw_line.split(b":", 1)[1].strip())
                while True:
                    hdr = stdout_pipe.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                payload_bytes = stdout_pipe.read(length)
                if not payload_bytes:
                    raise EOFError("MCP server EOF while reading Content-Length body")
                text = payload_bytes.decode("utf-8", errors="replace").strip()
            else:
                text = raw_line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == target_id:
                return msg

    def _run_mcp_session(self, inputs: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        argv = shlex.split(self.command)
        if not argv:
            raise BridgeError("Invalid --mcp-stdio command")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=_child_env(),
            start_new_session=True,
        )

        result_holder: dict[str, Any] = {}
        error_holder: list[BaseException] = []
        stderr_chunks: list[bytes] = []
        stderr_size = 0

        def _drain_stderr() -> None:
            nonlocal stderr_size
            if proc.stderr is None:
                return
            try:
                while chunk := proc.stderr.read(4096):
                    if stderr_size < 16384:
                        stderr_chunks.append(chunk[: 16384 - stderr_size])
                        stderr_size += len(chunk)
            except OSError:
                pass

        err_thread = threading.Thread(target=_drain_stderr, name="mcp-stdio-stderr", daemon=True)
        err_thread.start()

        def _worker() -> None:
            try:
                assert proc.stdin is not None and proc.stdout is not None
                self._send_jsonrpc(
                    proc.stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "bazaar-edge-hand", "version": BRIDGE_VERSION},
                        },
                    },
                )
                init_resp = self._read_jsonrpc_for_id(proc.stdout, 1)
                if "error" in init_resp:
                    result_holder["init_error"] = init_resp["error"]
                    return

                self._send_jsonrpc(
                    proc.stdin,
                    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                )
                self._send_jsonrpc(
                    proc.stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": self.tool_name, "arguments": dict(inputs)},
                    },
                )
                call_resp = self._read_jsonrpc_for_id(proc.stdout, 2)
                result_holder["call_resp"] = call_resp
            except BaseException as exc:
                error_holder.append(exc)

        try:
            t = threading.Thread(target=_worker, name="mcp-stdio-io", daemon=True)
            t.start()
            t.join(timeout=self.timeout_sec)
            if t.is_alive():
                _terminate_process_group(proc)
                raise TimeoutError(f"MCP stdio call {self.tool_name!r} timed out after {self.timeout_sec}s")

            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)

            err_thread.join(timeout=0.5)
        finally:
            _close_proc_pipes(proc)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()

        if error_holder:
            raise error_holder[0]
        if "init_error" in result_holder:
            return {"error": result_holder["init_error"], "_stage": "initialize"}, stderr_text
        return result_holder.get("call_resp"), stderr_text

    def execute(self, inputs: dict[str, Any], _event_meta: dict[str, Any]) -> ExecutionResult:
        try:
            resp, stderr_text = self._run_mcp_session(inputs)
        except Exception as exc:
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": f"{type(exc).__name__}: {exc}", "mcp_tool": self.tool_name},
                did=f"MCP stdio error ({self.tool_name}): {type(exc).__name__}",
                pitfall_note=f"{type(exc).__name__}: {exc}"[:240],
            )

        if not isinstance(resp, dict):
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "error": "empty MCP response", "stderr": stderr_text[:1000]},
                did=f"MCP tool {self.tool_name} returned empty response",
                pitfall_note=(stderr_text or "empty MCP response")[:240],
            )

        if "error" in resp:
            err_obj = resp["error"]
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            return ExecutionResult(
                outcome="fail",
                output={"ok": False, "mcp_tool": self.tool_name, "mcp_error": err_obj},
                did=f"MCP JSON-RPC error in {self.tool_name}",
                pitfall_note=str(err_msg or err_obj)[:240],
            )

        res = resp.get("result")
        if not isinstance(res, dict):
            return ExecutionResult(
                outcome="ok",
                output={"result": res, "mcp_tool": self.tool_name},
                did=f"MCP tools/call {self.tool_name} ok",
            )

        is_err = bool(res.get("isError", False))
        parsed_payload: dict[str, Any] | None = None

        if isinstance(res.get("structuredContent"), dict):
            parsed_payload = dict(res["structuredContent"])
        elif isinstance(res.get("content"), list):
            texts = [
                str(block.get("text") or "")
                for block in res["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if len(texts) == 1 and texts[0].strip():
                try:
                    candidate = json.loads(texts[0].strip())
                    if isinstance(candidate, dict):
                        parsed_payload = candidate
                    else:
                        parsed_payload = {"result": candidate}
                except ValueError:
                    parsed_payload = {"text": texts[0]}
            elif texts:
                parsed_payload = {"text": "\n".join(texts), "content": res["content"]}
            else:
                parsed_payload = {"content": res["content"]}
        else:
            parsed_payload = dict(res)

        if is_err or parsed_payload.get("outcome") == "fail" or parsed_payload.get("ok") is False:
            out_fail = dict(parsed_payload)
            out_fail.setdefault("ok", False)
            out_fail.setdefault("mcp_tool", self.tool_name)
            note = str(out_fail.get("error") or out_fail.get("text") or f"MCP tool {self.tool_name} returned isError")
            return ExecutionResult(
                outcome="fail",
                output=out_fail,
                did=f"MCP tools/call {self.tool_name} reported error",
                pitfall_note=note[:240],
                spend_micros=_extract_spend_micros(parsed_payload),
            )

        return ExecutionResult(
            outcome="ok",
            output=parsed_payload,
            did=f"MCP tools/call {self.tool_name} ok",
            spend_micros=_extract_spend_micros(parsed_payload),
            paid_micros=_extract_opt_paid_micros(parsed_payload),
        )


# ============================================================================
# Store v2 HTTP Client & Interruptible Long-Poller
# ============================================================================


class StoreV2Client:
    """Pure-stdlib HTTP client for Causal Store v2 with interruptible /events/wait."""

    def __init__(
        self,
        store_url: str,
        token: str = "",
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        base = (store_url or DEFAULT_STORE_URL).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BridgeError(f"Invalid store_url: {store_url!r}")
        self.store_url = base
        self.token = token.strip()
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._poll_lock = threading.Lock()
        self._active_poll_conn: http.client.HTTPConnection | None = None
        self._active_poll_conns: set[http.client.HTTPConnection] = set()

    def request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
        auth: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{self.store_url}{path_or_url}"
        hdrs = {
            "Accept": "application/json",
            "User-Agent": f"bazaar-edge-hand/{BRIDGE_VERSION}",
        }
        if auth and self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        body_bytes = None
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method=method)
        ctx = self.ssl_context if url.startswith("https://") else None
        try:
            with _open_url_with_proxy_fallback(req, timeout=timeout, ctx=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return int(resp.status), (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as err:
            try:
                raw = err.read().decode("utf-8", errors="replace") if err.fp else ""
            finally:
                err.close()
            try:
                data = json.loads(raw) if raw.strip() else {"ok": False, "error": str(err)}
            except ValueError:
                data = {"ok": False, "error": raw[:1000] or str(err)}
            return int(err.code), data
        except Exception as exc:
            return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def interrupt_poll(self) -> None:
        """Immediately wake up all in-flight GET /events/wait sockets."""
        with self._poll_lock:
            conns = list(self._active_poll_conns)
            if self._active_poll_conn is not None and self._active_poll_conn not in self._active_poll_conns:
                conns.append(self._active_poll_conn)
            self._active_poll_conns.clear()
            self._active_poll_conn = None
        for conn in conns:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def wait_events(
        self,
        hand_id: str,
        *,
        poll_timeout: float = 20.0,
        allow_owner_wildcard: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        hid = (hand_id or "").strip()
        if not hid and not allow_owner_wildcard:
            raise BridgeError("hand_id must be non-empty for /events/wait unless allow_owner_wildcard=True")
        eff_wait = min(max(float(poll_timeout), 0.05), 30.0)
        qs = urllib.parse.urlencode({"hand_id": hid, "timeout": f"{eff_wait:.2f}"})
        parsed = urllib.parse.urlsplit(self.store_url)
        base_path = parsed.path.rstrip("/")
        req_path = f"{base_path}/events/wait?{qs}"
        sock_timeout = eff_wait + 8.0

        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname or "",
                parsed.port or 443,
                timeout=sock_timeout,
                context=self.ssl_context,
            )
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname or "",
                parsed.port or 80,
                timeout=sock_timeout,
            )

        hdrs = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"bazaar-edge-hand/{BRIDGE_VERSION}",
            "Connection": "close",
        }
        with self._poll_lock:
            self._active_poll_conns.add(conn)
            self._active_poll_conn = conn
        try:
            conn.request("GET", req_path, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
            return status, (json.loads(raw) if raw.strip() else {})
        except _PollInterrupted:
            raise
        except Exception as exc:
            return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._poll_lock:
                self._active_poll_conns.discard(conn)
                if self._active_poll_conn is conn:
                    self._active_poll_conn = next(iter(self._active_poll_conns), None)
            try:
                conn.close()
            except OSError:
                pass


# ============================================================================
# Authentication, Self-Onboarding & Bridge Lifecycle Runtime
# ============================================================================


def _token_supports_hand_ops(
    store_url: str,
    token: str,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> bool:
    """Return True if `token` authenticates via GET /whoami and allows `hand` operations."""
    client = StoreV2Client(store_url=store_url, token=token, ssl_context=ssl_context)
    st, data = client.request_json("GET", "/whoami", timeout=6.0)
    if st != 200 or not isinstance(data, dict) or not data.get("ok"):
        return False
    if str(data.get("kind") or "") == "founder":
        return True
    allow_ops = data.get("allow_ops")
    return isinstance(allow_ops, list) and "hand" in allow_ops


def _read_local_account_session_token(path: Path) -> str:
    """Read an account session or buyer token from `account_session.json` or a plain-text file."""
    resolved = path.expanduser()
    if not resolved.is_file():
        return ""
    try:
        raw = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(obj, dict):
            for field_name in (
                "session_token",
                "account_token",
                "buyer_token",
                "buyer_key",
                "token",
                "api_key",
            ):
                val = obj.get(field_name)
                if isinstance(val, str):
                    norm = _normalize_store_token(val)
                    if norm:
                        return norm
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return _normalize_store_token(stripped)
    return ""


def _is_safe_account_bind_url(url: str, environ: Mapping[str, str]) -> bool:
    """Allow forwarding local account credentials only over HTTPS or loopback (unless explicitly overridden)."""
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return True
    if scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return True
    return str(environ.get("BAZAAR_ALLOW_INSECURE_HTTP") or "").strip().lower() in ("1", "true", "yes")


def _discover_local_account_token(
    *,
    reg_url: str,
    account_token: str,
    bind_local_account: bool,
    keyring_dirs: Sequence[Path],
    fallback_buyer_tok: str,
    environ: Mapping[str, str],
) -> str:
    explicit_tok = _normalize_store_token(account_token)
    if explicit_tok:
        if not _is_safe_account_bind_url(reg_url, environ):
            raise BridgeError(f"Refusing to send account_token over insecure non-loopback HTTP URL: {reg_url}")
        return explicit_tok
    if not bind_local_account or not _is_safe_account_bind_url(reg_url, environ):
        return ""
    for env_key in ("BAZAAR_ACCOUNT_TOKEN", "BAZAAR_ACCOUNT_SESSION_TOKEN"):
        cand = _normalize_store_token(environ.get(env_key) or "")
        if cand:
            return cand
    if fallback_buyer_tok:
        return fallback_buyer_tok
    seen_dirs: set[Path] = set()
    for kd in keyring_dirs:
        resolved_dir = kd.expanduser()
        if resolved_dir in seen_dirs:
            continue
        seen_dirs.add(resolved_dir)
        buyer_file = resolved_dir / "buyer_key"
        if buyer_file.is_file():
            btok = _read_store_key_file(buyer_file)
            if btok:
                return btok
        sess_file = resolved_dir / "account_session.json"
        if sess_file.is_file():
            stok = _read_local_account_session_token(sess_file)
            if stok:
                return stok
    return ""


def resolve_or_onboard_api_key(
    *,
    store_url: str,
    key_file: str | Path | None = None,
    seat_key_file: str | Path | None = None,
    auto_onboard: bool = False,
    owner_label: str = "external_edge_hand",
    onboarding_url: str = "",
    argv_token: str = "",
    account_token: str = "",
    bind_local_account: bool = True,
    keyring_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    log_stream: Any = None,
) -> tuple[str, Path | None]:
    """Resolve store key from explicit CLI/file, seller seat key, env, or self-onboard via /bridge/register."""
    environ = env if env is not None else os.environ

    # 1. Explicit CLI --api-key / --token override takes highest precedence when valid.
    explicit_argv_tok = _normalize_store_token(argv_token)
    if explicit_argv_tok:
        return explicit_argv_tok, None

    # 2. Explicit --key-file override takes precedence over ambient env vars.
    if key_file:
        kp = Path(key_file).expanduser()
        if kp.is_file():
            tok = _read_store_key_file(kp)
            if tok:
                return tok, kp
            if not auto_onboard:
                raise BridgeError(f"--key-file is empty or invalid: {kp}")
        elif not auto_onboard:
            raise BridgeError(f"--key-file does not exist: {kp}")

    # 3. Explicit seller env vars take precedence over generic buyer BAZAAR_API_KEY.
    seller_env_tok = _normalize_store_token(
        environ.get("BAZAAR_SELLER_KEY") or environ.get("BAZAAR_EDGE_SEAT_KEY") or ""
    )
    if seller_env_tok:
        return seller_env_tok, None

    home_dir = Path(environ["HOME"]) if environ.get("HOME") else Path.home()
    default_keyring_dir = Path(
        keyring_dir
        or environ.get("BAZAAR_KEYRING_DIR")
        or (home_dir / ".causal-store-v2")
    ).expanduser()
    default_seat_path = Path(
        seat_key_file
        or environ.get("BAZAAR_EDGE_SEAT_KEY_FILE")
        or (default_keyring_dir / "edge_seat_key")
    ).expanduser()
    save_target = Path(key_file).expanduser() if key_file else default_seat_path
    existing_seat_tok = _read_store_key_file(save_target) if save_target.is_file() else ""

    # If --seat-key-file was explicitly supplied and exists, honor it directly.
    if seat_key_file and existing_seat_tok:
        return existing_seat_tok, save_target

    env_tok = _normalize_store_token(environ.get("BAZAAR_API_KEY") or environ.get("CAUSAL_STORE_API_KEY") or "")
    fallback_buyer_env_tok = ""
    if env_tok:
        if existing_seat_tok and existing_seat_tok != env_tok:
            # Common Personal-Agent case: env.sh exported buyer_key into BAZAAR_API_KEY while edge_seat_key holds hand ops.
            if _token_supports_hand_ops(store_url, env_tok, ssl_context=ssl_context):
                return env_tok, None
            _log(
                f"Ambient BAZAAR_API_KEY lacks seller `hand` permission or is stale; falling back to 0600 seat key {save_target}",
                stream=log_stream,
            )
            return existing_seat_tok, save_target
        if auto_onboard and not existing_seat_tok:
            if _token_supports_hand_ops(store_url, env_tok, ssl_context=ssl_context):
                return env_tok, None
            fallback_buyer_env_tok = env_tok
            _log(
                "Ambient BAZAAR_API_KEY lacks seller `hand` permission; proceeding with --auto-onboard seller seat registration",
                stream=log_stream,
            )
        else:
            return env_tok, None

    if existing_seat_tok:
        return existing_seat_tok, save_target

    if not auto_onboard:
        raise BridgeError(
            "No API key found. Set BAZAAR_API_KEY, pass --key-file <path>, "
            "or pass --auto-onboard to mint a Seller-only seat key automatically."
        )

    reg_url = (
        onboarding_url.strip()
        or environ.get("BAZAAR_ONBOARDING_URL", "").strip()
        or f"{store_url.rstrip('/')}/bridge/register"
    )
    candidate_keyring_dirs: list[Path] = []
    if keyring_dir or environ.get("BAZAAR_KEYRING_DIR"):
        candidate_keyring_dirs.append(default_keyring_dir)
    elif env is not None and not environ.get("HOME") and not environ.get("BAZAAR_KEYRING_DIR") and keyring_dir is None:
        candidate_keyring_dirs.append(save_target.parent)
    else:
        candidate_keyring_dirs.extend([save_target.parent, default_keyring_dir])
    resolved_acct_tok = _discover_local_account_token(
        reg_url=reg_url,
        account_token=account_token,
        bind_local_account=bind_local_account,
        keyring_dirs=candidate_keyring_dirs,
        fallback_buyer_tok=fallback_buyer_env_tok,
        environ=environ,
    )
    client = StoreV2Client(store_url=store_url, token=resolved_acct_tok, ssl_context=ssl_context)
    clean_label = (owner_label or "external_edge_hand").strip()[:80] or "external_edge_hand"
    st, data = client.request_json(
        "POST",
        reg_url,
        payload={"owner_label": clean_label, "label": clean_label},
        timeout=15.0,
        auth=bool(resolved_acct_tok),
    )
    if st not in (200, 201) or not isinstance(data, dict):
        raise BridgeError(f"Auto-onboard POST {reg_url} failed (HTTP {st}): {data}")

    nested = data.get("key") if isinstance(data.get("key"), dict) else {}
    legacy_key_field = _TOK_PREFIX + "key"
    minted_tok = _normalize_store_token(
        str(
            data.get("seat_key")
            or data.get("token")
            or data.get(legacy_key_field)
            or data.get("api_key")
            or nested.get("token")
            or ""
        )
    )
    if not minted_tok:
        raise BridgeError(f"Auto-onboard response from {reg_url} did not contain a token: {data}")

    saved_path = save_key_0600(save_target, minted_tok)
    bind_suffix = (
        f" account_bound=True (account_id={data.get('bound_account_id')})"
        if data.get("account_bound")
        else ""
    )
    _log(
        f"Auto-onboarded Seller seat key (owner_id={data.get('owner_id') or nested.get('owner_id')}) "
        f"and saved 0600 to {saved_path}{bind_suffix}",
        stream=log_stream,
    )
    return minted_tok, saved_path


class EdgeHandBridge:
    """Lifecycle manager & event loop bridging Causal Store v2 async_poll to one or more EdgeAdapters."""

    def __init__(
        self,
        *,
        store_url: str,
        token: str,
        spec: CardHandSpec | None = None,
        adapter: EdgeAdapter | None = None,
        hands: Sequence[tuple[CardHandSpec, EdgeAdapter]] | None = None,
        specs: Sequence[CardHandSpec] | None = None,
        adapters: Sequence[EdgeAdapter] | Mapping[str, EdgeAdapter] | None = None,
        poll_timeout: float = 20.0,
        install_signals: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        log_stream: Any = None,
    ) -> None:
        if not token or not token.strip():
            raise BridgeError("API token is required")
        self.client = StoreV2Client(store_url=store_url, token=token.strip(), ssl_context=ssl_context)

        resolved_hands: list[tuple[CardHandSpec, EdgeAdapter]] = []
        if hands is not None:
            for item in hands:
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    raise BridgeError("hands entries must be (CardHandSpec, EdgeAdapter) pairs")
                resolved_hands.append((item[0], item[1]))
        if specs is not None:
            spec_list = list(specs)
            if isinstance(adapters, Mapping):
                for s_item in spec_list:
                    ad = adapters.get(s_item.hand_id) or adapters.get(s_item.card_id) or adapter
                    if ad is None:
                        raise BridgeError(f"Missing EdgeAdapter for hand_id={s_item.hand_id!r}")
                    resolved_hands.append((s_item, ad))
            elif adapters is not None:
                ad_list = list(adapters)
                if len(ad_list) != len(spec_list):
                    raise BridgeError("specs and adapters sequences must have identical length")
                resolved_hands.extend(zip(spec_list, ad_list))
            else:
                if adapter is None:
                    raise BridgeError("adapter is required when passing specs without adapters")
                for s_item in spec_list:
                    resolved_hands.append((s_item, adapter))
        if spec is not None:
            if adapter is None:
                raise BridgeError("adapter is required when spec is provided")
            resolved_hands.insert(0, (spec, adapter))
        if not resolved_hands:
            raise BridgeError("At least one CardHandSpec and EdgeAdapter must be provided")

        self._hands: list[tuple[CardHandSpec, EdgeAdapter]] = []
        self._hand_by_id: dict[str, tuple[CardHandSpec, EdgeAdapter]] = {}
        for s_item, ad_item in resolved_hands:
            self._append_validated_hand(s_item, ad_item)

        self.spec = self._hands[0][0]
        self.adapter = self._hands[0][1]
        self.poll_timeout = min(max(float(poll_timeout), 0.05), 30.0)
        self.install_signals = install_signals
        self.log_stream = log_stream

        self.whoami_info: dict[str, Any] = {}
        self.registered_hand: dict[str, Any] = {}
        self.registered_card: dict[str, Any] = {}
        self.registered_hands: dict[str, dict[str, Any]] = {}
        self.registered_cards: dict[str, dict[str, Any]] = {}

        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._presence_active = False
        self._marked_away = False
        self._in_poll_wait = False
        self._active_poll_waiters = 0
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None
        self._signals_installed = False
        self._fulfilled_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._card_price_cache: dict[str, int] = {}

    @property
    def specs(self) -> list[CardHandSpec]:
        return [s for s, _ in self._hands]

    def _append_validated_hand(self, spec_item: CardHandSpec, adapter_item: EdgeAdapter) -> None:
        hid = (spec_item.hand_id or "").strip()
        if not hid:
            raise BridgeError("CardHandSpec.hand_id must be non-empty")
        spec_item.hand_id = hid
        if hid in self._hand_by_id:
            raise BridgeError(f"Duplicate hand_id on EdgeHandBridge: {hid!r}")
        self._hands.append((spec_item, adapter_item))
        self._hand_by_id[hid] = (spec_item, adapter_item)

    def add_hand(self, spec_item: CardHandSpec, adapter_item: EdgeAdapter | None = None) -> None:
        """Register an additional (CardHandSpec, EdgeAdapter) on this bridge prior to starting."""
        eff_adapter = adapter_item if adapter_item is not None else self.adapter
        self._append_validated_hand(spec_item, eff_adapter)

    def verify_whoami(self) -> dict[str, Any]:
        st, data = self.client.request_json("GET", "/whoami", timeout=10.0)
        if st != 200 or not data.get("ok"):
            raise BridgeError(f"GET /whoami failed (HTTP {st}): {data.get('error') or data}")
        self.whoami_info = data
        _log(
            f"Authenticated via GET /whoami: owner_id={data.get('owner_id')} "
            f"key_id={data.get('key_id')} kind={data.get('kind')} "
            f"allow_ops={data.get('allow_ops')}",
            stream=self.log_stream,
        )
        return data

    def register_hand_and_card(self) -> None:
        if not self.whoami_info:
            self.verify_whoami()

        who = self.whoami_info
        owner_id = str(who.get("owner_id") or "")
        k_kind = str(who.get("kind") or "internal")

        for idx, (spec_item, adapter_item) in enumerate(self._hands):
            old_hid = spec_item.hand_id
            # Preserve immutable hand_id if card already exists under our owner_id
            if spec_item.card_id and not spec_item.skip_card:
                cst, cdata = self.client.request_json(
                    "GET",
                    f"/card/{urllib.parse.quote(spec_item.card_id, safe='')}",
                    timeout=10.0,
                )
                if cst == 200 and isinstance(cdata.get("card"), dict):
                    existing = cdata["card"]
                    if existing.get("owner_id") == owner_id and existing.get("hand_id"):
                        rebound_hid = str(existing["hand_id"]).strip()
                        if rebound_hid and rebound_hid != old_hid:
                            if rebound_hid in self._hand_by_id and self._hand_by_id[rebound_hid][0] is not spec_item:
                                raise BridgeError(f"Duplicate hand_id after existing card rebind: {rebound_hid!r}")
                            self._hand_by_id.pop(old_hid, None)
                            spec_item.hand_id = rebound_hid
                            self._hand_by_id[rebound_hid] = (spec_item, adapter_item)

            if not (spec_item.hand_id or "").strip():
                raise BridgeError("CardHandSpec.hand_id must be non-empty")

            if k_kind == "founder":
                pc_cap = max(500_000, spec_item.price_micros)
                dl_cap = max(2_000_000, spec_item.price_micros * 100)
            else:
                k_pc = int(who.get("per_call_limit_micros") or 0)
                k_dl = int(who.get("daily_limit_micros") or 0)
                pc_cap = min(k_pc, max(500_000, spec_item.price_micros))
                dl_cap = min(k_dl, max(2_000_000, spec_item.price_micros * 100))
                if spec_item.price_micros > pc_cap:
                    _log(
                        f"WARNING: card price_micros ({spec_item.price_micros}) exceeds seat key "
                        f"per_call_limit_micros ({pc_cap}); zero-custody seat keys require price_micros=0 "
                        f"until upgraded.",
                        stream=self.log_stream,
                    )

            hand_payload = {
                "hand_id": spec_item.hand_id,
                "label": spec_item.title or f"Edge Hand ({spec_item.hand_id})",
                "kind": "async_poll",
                "endpoint": "",
                "upstream_url": "",
                "per_call_limit_micros": pc_cap,
                "daily_limit_micros": dl_cap,
                "presence_state": "in",
                "presence_note": spec_item.presence_note,
            }
            hst, hres = self.client.request_json("POST", "/hand", payload=hand_payload, timeout=10.0)
            if hst != 200 or not hres.get("ok"):
                raise BridgeError(f"POST /hand failed (HTTP {hst}): {hres.get('error') or hres}")
            self.registered_hands[spec_item.hand_id] = hres
            if idx == 0:
                self.registered_hand = hres

            if not spec_item.skip_card:
                card_payload = {
                    "card_id": spec_item.card_id,
                    "title": spec_item.title,
                    "price_micros": spec_item.price_micros,
                    "gate": spec_item.gate,
                    "inputs_spec": spec_item.inputs_spec,
                    "outputs_spec": spec_item.outputs_spec,
                    "mechanism": spec_item.mechanism,
                    "preconditions": spec_item.preconditions,
                    "side_effects": spec_item.side_effects,
                    "hand_id": spec_item.hand_id,
                    "is_public": spec_item.is_public,
                }
                cst, cres = self.client.request_json("POST", "/card", payload=card_payload, timeout=10.0)
                if cst != 200 or not cres.get("ok"):
                    raise BridgeError(f"POST /card failed (HTTP {cst}): {cres.get('error') or cres}")
                if spec_item.card_id:
                    self.registered_cards[spec_item.card_id] = cres
                    self._card_price_cache[spec_item.card_id] = max(0, int(spec_item.price_micros))
                if idx == 0:
                    self.registered_card = cres

            self.set_presence("in", note=spec_item.presence_note, hand_id=spec_item.hand_id)
            _log(
                f"Mounted hand_id={spec_item.hand_id} card_id={spec_item.card_id or '(none)'} "
                f"adapter={adapter_item.name} presence=in",
                stream=self.log_stream,
            )

    def set_presence(
        self,
        state: str,
        *,
        note: str = "",
        hand_id: str | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        targets: list[CardHandSpec]
        if hand_id is not None:
            clean_hid = hand_id.strip()
            if not clean_hid:
                raise BridgeError("hand_id must be non-empty for set_presence")
            pair = self._hand_by_id.get(clean_hid)
            targets = [pair[0]] if pair is not None else [CardHandSpec(hand_id=clean_hid)]
        else:
            targets = [s for s, _ in self._hands]

        last_res: dict[str, Any] = {}
        any_ok = False
        for s_item in targets:
            eff_note = note or (s_item.presence_note if state == "in" else s_item.away_note)
            st, res = self.client.request_json(
                "POST",
                "/presence",
                payload={
                    "hand_id": s_item.hand_id,
                    "state": state,
                    "note": eff_note,
                },
                timeout=timeout,
            )
            last_res = res
            if st == 200 and res.get("ok"):
                any_ok = True
        if any_ok:
            with self._state_lock:
                self._presence_active = state == "in"
                self._marked_away = state == "away"
        return last_res

    def mark_away_once(self) -> None:
        with self._state_lock:
            if not self._presence_active or self._marked_away:
                return
            self._marked_away = True
            self._presence_active = False
        for s_item, _ in self._hands:
            try:
                self.client.request_json(
                    "POST",
                    "/presence",
                    payload={"hand_id": s_item.hand_id, "state": "away", "note": s_item.away_note},
                    timeout=2.5,
                )
                _log(f"Set presence_state=away for hand_id={s_item.hand_id}", stream=self.log_stream)
            except Exception:
                pass

    def stop(self) -> None:
        """Signal loop termination, unblock active /events/wait immediately, and set presence=away."""
        self._stop_event.set()
        self.mark_away_once()
        self.client.interrupt_poll()

    def _install_signal_handlers(self) -> None:
        atexit.register(self.mark_away_once)
        if not self.install_signals or threading.current_thread() is not threading.main_thread():
            return

        def _on_signal(signum: int, _frame: Any) -> None:
            _log(f"Received signal {signum}; shutting down and setting presence=away...", stream=self.log_stream)
            self._stop_event.set()
            self.client.interrupt_poll()
            if self._in_poll_wait and len(self._hands) == 1:
                raise _PollInterrupted()

        self._prev_sigint = signal.getsignal(signal.SIGINT)
        self._prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        self._signals_installed = True

    def _restore_signal_handlers(self) -> None:
        try:
            atexit.unregister(self.mark_away_once)
        except Exception:
            pass
        if not self._signals_installed:
            return
        try:
            if self._prev_sigint is not None:
                signal.signal(signal.SIGINT, self._prev_sigint)
            if self._prev_sigterm is not None:
                signal.signal(signal.SIGTERM, self._prev_sigterm)
        except (ValueError, OSError):
            pass
        self._signals_installed = False

    def __enter__(self) -> "EdgeHandBridge":
        self._stop_event.clear()
        self._install_signal_handlers()
        try:
            self.register_hand_and_card()
        except BaseException:
            self.mark_away_once()
            self._restore_signal_handlers()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self.stop()
        self._restore_signal_handlers()
        closed_ids: set[int] = set()
        for _, ad_item in self._hands:
            if id(ad_item) not in closed_ids:
                closed_ids.add(id(ad_item))
                ad_item.close()

    def _resolve_hand_target(self, event: dict[str, Any]) -> tuple[CardHandSpec, EdgeAdapter]:
        ev_hid = str(event.get("hand_id") or "").strip()
        if ev_hid and ev_hid in self._hand_by_id:
            return self._hand_by_id[ev_hid]
        ev_cid = str(event.get("card_id") or "").strip()
        if ev_cid:
            for s_item, ad_item in self._hands:
                if s_item.card_id == ev_cid:
                    return s_item, ad_item
        return self.spec, self.adapter

    def _resolve_price_cap(self, card_id: str, max_cost_micros: int, *, active_spec: CardHandSpec | None = None) -> int:
        spec_ref = active_spec or self.spec
        with self._state_lock:
            if card_id and card_id in self._card_price_cache:
                return self._card_price_cache[card_id]
        if card_id and card_id == spec_ref.card_id and not spec_ref.skip_card:
            cap = max(0, int(spec_ref.price_micros))
            with self._state_lock:
                self._card_price_cache[card_id] = cap
            return cap
        if card_id:
            cst, cdata = self.client.request_json(
                "GET",
                f"/card/{urllib.parse.quote(card_id, safe='')}",
                timeout=5.0,
            )
            if cst == 200 and isinstance(cdata.get("card"), dict) and cdata["card"].get("price_micros") is not None:
                cap = max(0, int(cdata["card"]["price_micros"]))
                with self._state_lock:
                    self._card_price_cache[card_id] = cap
                return cap
        if not spec_ref.skip_card:
            return max(0, int(spec_ref.price_micros))
        return max(0, int(max_cost_micros or 0))

    def _build_safe_fulfill_payload(
        self,
        run_id: str,
        exec_res: ExecutionResult,
        max_cost_micros: int,
        *,
        price_micros: int | None = None,
        active_spec: CardHandSpec | None = None,
        active_adapter: EdgeAdapter | None = None,
    ) -> dict[str, Any]:
        spec_ref = active_spec or self.spec
        ad_ref = active_adapter or self.adapter
        secrets_to_scrub = [self.client.token] if self.client.token else []
        outcome = exec_res.outcome if exec_res.outcome in ("ok", "fail", "unknown") else "fail"
        clean_output = redact_outbound_secrets(exec_res.output, extra_secrets=secrets_to_scrub)
        clean_did = str(redact_outbound_secrets(exec_res.did or f"{ad_ref.name}:{outcome}", extra_secrets=secrets_to_scrub))[:400]
        clean_note = str(redact_outbound_secrets(exec_res.pitfall_note or "", extra_secrets=secrets_to_scrub))[:400]
        spend = min(max(0, int(exec_res.spend_micros or 0)), max(0, int(max_cost_micros or 0)))

        payload: dict[str, Any] = {
            "run_id": run_id,
            "outcome": outcome,
            "spend_micros": spend,
            "output": clean_output,
            "did": clean_did,
            "pitfall_note": clean_note,
        }
        if exec_res.paid_micros is not None:
            price_cap = (
                max(0, int(price_micros))
                if price_micros is not None
                else max(0, int(spec_ref.price_micros if spec_ref.price_micros > 0 else max_cost_micros))
            )
            payload["paid_micros"] = min(max(0, int(exec_res.paid_micros)), price_cap) if outcome == "ok" else 0
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_FULFILL_BODY_BYTES:
            preview = encoded[:65536].decode("utf-8", errors="replace")
            payload["output"] = {
                "truncated": True,
                "original_bytes": len(encoded),
                "preview": redact_outbound_secrets(preview, extra_secrets=secrets_to_scrub),
            }
        return payload

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        run_id = str(event.get("run_id") or "").strip()
        if not run_id:
            return {"ok": False, "error": "missing run_id"}

        active_spec, active_adapter = self._resolve_hand_target(event)
        with self._state_lock:
            cached_body = self._fulfilled_cache.get(run_id)
        if cached_body is not None:
            fulfill_body = cached_body
        else:
            raw_inputs = event.get("input")
            inputs = dict(raw_inputs) if isinstance(raw_inputs, dict) else {}
            max_cost_micros = int(event.get("max_cost_micros") or 0)
            try:
                exec_res = active_adapter.execute(inputs, event)
            except Exception as exc:
                exec_res = ExecutionResult(
                    outcome="fail",
                    output={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                    did=f"unhandled adapter error: {type(exc).__name__}",
                    pitfall_note=f"{type(exc).__name__}: {exc}"[:240],
                )
            price_cap: int | None = None
            if exec_res.paid_micros is not None:
                ev_card_id = str(event.get("card_id") or active_spec.card_id or "").strip()
                price_cap = self._resolve_price_cap(ev_card_id, max_cost_micros, active_spec=active_spec)
            fulfill_body = self._build_safe_fulfill_payload(
                run_id,
                exec_res,
                max_cost_micros,
                price_micros=price_cap,
                active_spec=active_spec,
                active_adapter=active_adapter,
            )
            with self._state_lock:
                self._fulfilled_cache[run_id] = fulfill_body
                while len(self._fulfilled_cache) > 512:
                    self._fulfilled_cache.popitem(last=False)

        fst = 0
        fres: dict[str, Any] = {"ok": False}
        for attempt in range(3):
            fst, fres = self.client.request_json("POST", "/hand/fulfill", payload=fulfill_body, timeout=12.0)
            if fst == 200 and fres.get("ok"):
                break
            if fst not in (0, 502, 503, 504) or self._stop_event.is_set() or attempt == 2:
                _log(f"POST /hand/fulfill for {run_id} returned HTTP {fst}: {fres}", stream=self.log_stream)
                break
            time.sleep(0.15 * (attempt + 1))
        return fres

    def poll_and_process_once(
        self,
        *,
        hand_id: str | None = None,
        allow_owner_wildcard: bool = False,
    ) -> int:
        raw_hid = self.spec.hand_id if hand_id is None else hand_id
        hid = (raw_hid or "").strip()
        if not hid and not allow_owner_wildcard:
            raise BridgeError("hand_id must be non-empty for poll_once/wait_events unless allow_owner_wildcard=True")
        if self._stop_event.is_set():
            return 0
        with self._state_lock:
            self._active_poll_waiters += 1
            self._in_poll_wait = True
        try:
            st, res = self.client.wait_events(
                hid,
                poll_timeout=self.poll_timeout,
                allow_owner_wildcard=allow_owner_wildcard,
            )
        except _PollInterrupted:
            return 0
        finally:
            with self._state_lock:
                self._active_poll_waiters = max(0, self._active_poll_waiters - 1)
                self._in_poll_wait = self._active_poll_waiters > 0

        if self._stop_event.is_set():
            return 0
        if st != 200 or not res.get("ok"):
            if not self._stop_event.is_set():
                err_detail = res.get("error") or res
                err_sig = f"{st}:{err_detail}"
                with self._state_lock:
                    streaks = getattr(self, "_poll_transient_streaks", None)
                    if streaks is None:
                        streaks = {}
                        self._poll_transient_streaks = streaks
                    prev_sig, prev_cnt = streaks.get(hid, ("", 0))
                    cur_cnt = (prev_cnt + 1) if prev_sig == err_sig else 1
                    streaks[hid] = (err_sig, cur_cnt)
                if cur_cnt == 1:
                    _log(
                        f"GET /events/wait (hand_id={hid!r}) transient status={st}: {err_detail}",
                        stream=self.log_stream,
                    )
                elif cur_cnt % 30 == 0:
                    _log(
                        f"GET /events/wait (hand_id={hid!r}) transient status={st} repeated {cur_cnt}x "
                        f"(suppressed {cur_cnt - 1} duplicates): {err_detail}",
                        stream=self.log_stream,
                    )
                self._stop_event.wait(timeout=0.5)
            return 0

        recovered_cnt = 0
        with self._state_lock:
            streaks = getattr(self, "_poll_transient_streaks", None)
            if streaks and hid in streaks:
                _, recovered_cnt = streaks.pop(hid)
        if recovered_cnt > 1 and not self._stop_event.is_set():
            _log(
                f"GET /events/wait (hand_id={hid!r}) recovered after {recovered_cnt} transient poll errors",
                stream=self.log_stream,
            )

        events = res.get("events") or []
        processed = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            self.handle_event(ev)
            processed += 1
        return processed

    poll_once = poll_and_process_once

    def run_forever(self, *, once: bool = False) -> int:
        with self:
            if len(self._hands) == 1:
                only_hid = self._hands[0][0].hand_id
                while not self._stop_event.is_set():
                    count = self.poll_and_process_once(hand_id=only_hid)
                    if once and count > 0:
                        break
                return 0

            worker_errors: list[BaseException] = []
            err_lock = threading.Lock()

            def _poll_worker(target_hid: str) -> None:
                try:
                    while not self._stop_event.is_set():
                        count = self.poll_and_process_once(hand_id=target_hid)
                        if once and count > 0:
                            self._stop_event.set()
                            self.client.interrupt_poll()
                            break
                except BaseException as exc:
                    with err_lock:
                        worker_errors.append(exc)
                    self._stop_event.set()
                    self.client.interrupt_poll()

            threads = [
                threading.Thread(
                    target=_poll_worker,
                    args=(s_item.hand_id,),
                    name=f"edge-poll-{s_item.hand_id}",
                    daemon=True,
                )
                for s_item, _ in self._hands
            ]
            for t in threads:
                t.start()
            try:
                while not self._stop_event.is_set():
                    if not any(t.is_alive() for t in threads):
                        break
                    self._stop_event.wait(timeout=0.2)
            finally:
                self._stop_event.set()
                self.client.interrupt_poll()
                for t in threads:
                    t.join(timeout=5.0)
            if worker_errors:
                raise worker_errors[0]
        return 0


# ============================================================================
# CLI Argument Parsing & Entrypoint
# ============================================================================


def _parse_headers(raw_headers: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_headers or []:
        name, sep, val = item.partition(":")
        if not sep or not name.strip():
            raise BridgeError(f"Invalid --http-header {item!r}; expected 'Header-Name: value'")
        parsed[name.strip()] = val.strip()
    return parsed


def build_adapter_from_config(
    args: argparse.Namespace,
    manifest: dict[str, Any] | None = None,
) -> tuple[EdgeAdapter, str]:
    m = dict(manifest or {})
    adapter_cfg = m.get("adapter") if isinstance(m.get("adapter"), dict) else {}
    timeout_sec = float(
        args.exec_timeout
        if args.exec_timeout is not None
        else m.get("exec_timeout", adapter_cfg.get("exec_timeout", 30.0))
    )

    http_upstream = args.http_upstream or m.get("http_upstream") or adapter_cfg.get("http_upstream")
    cmd_target = args.cmd or m.get("cmd") or adapter_cfg.get("cmd")
    py_target = args.python_target or m.get("python_target") or adapter_cfg.get("python_target")
    mcp_stdio = args.mcp_stdio or m.get("mcp_stdio") or adapter_cfg.get("mcp_stdio")

    chosen = [bool(x) for x in (http_upstream, cmd_target, py_target, mcp_stdio)]
    if sum(chosen) == 0:
        raise BridgeError(
            "Select one execution adapter: --http-upstream <URL>, --cmd '<command>', "
            "--python-target '<file_or_module>:<callable>', or --mcp-stdio '<command>' --mcp-tool '<tool>'"
        )
    if sum(chosen) > 1:
        raise BridgeError("Specify only one execution adapter (--http-upstream, --cmd, --python-target, or --mcp-stdio)")

    if http_upstream:
        method = args.http_method or m.get("http_method") or adapter_cfg.get("http_method") or "POST"
        manifest_hdrs = adapter_cfg.get("http_headers") if isinstance(adapter_cfg.get("http_headers"), dict) else {}
        hdrs = {str(k): str(v) for k, v in manifest_hdrs.items()}
        hdrs.update(_parse_headers(args.http_header))
        slug = urllib.parse.urlsplit(str(http_upstream)).path.strip("/") or "http_upstream"
        return HttpUpstreamAdapter(str(http_upstream), method=str(method), headers=hdrs, timeout_sec=timeout_sec), slug

    if cmd_target:
        first_tok = shlex.split(str(cmd_target))[0] if str(cmd_target).strip() else "cmd"
        slug = Path(first_tok).stem or "cmd"
        return CommandAdapter(str(cmd_target), timeout_sec=timeout_sec), slug

    if py_target:
        _, _, fn_part = str(py_target).rpartition(":")
        return PythonTargetAdapter(str(py_target), timeout_sec=timeout_sec), (fn_part or "py_target")

    mcp_tool = args.mcp_tool or m.get("mcp_tool") or adapter_cfg.get("mcp_tool") or ""
    return McpStdioAdapter(str(mcp_stdio), str(mcp_tool), timeout_sec=timeout_sec), (str(mcp_tool) or "mcp_tool")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bazaar_edge_hand.py",
        description="Mount any local/deployed HTTP app, CLI, Python callable, or MCP stdio tool onto Causal Store v2.",
    )
    p.add_argument(
        "--store-url",
        default=None,
        help=f"Causal Store v2 base URL (default: $BAZAAR_STORE_URL or {DEFAULT_STORE_URL})",
    )
    p.add_argument("--key-file", default=None, help="Path to 0600 file containing store API key")
    p.add_argument("--seat-key-file", default=None, help="Override path for saved auto-onboard seat key")
    p.add_argument(
        "--auto-onboard",
        "--register-if-needed",
        dest="auto_onboard",
        action="store_true",
        help="Auto-register a Seller-only store seat key via POST <store_url>/bridge/register and save 0600",
    )
    p.add_argument("--owner-label", default="external_edge_hand", help="Owner label for --auto-onboard")
    p.add_argument("--onboarding-url", default="", help="Optional override URL for /bridge/register")
    p.add_argument("--account-token-file", default=None, help="Optional buyer_key or account_session.json path for auto-binding --auto-onboard seats")
    p.add_argument(
        "--no-bind-local-account",
        dest="bind_local_account",
        action="store_false",
        default=True,
        help="Disable auto-binding --auto-onboard seat keys to local ~/.causal-store-v2/buyer_key or account_session.json",
    )
    p.add_argument("--account-token", dest="account_token_discouraged", default=None, help=argparse.SUPPRESS)
    p.add_argument("--api-key", "--token", dest="api_key_discouraged", default=None, help=argparse.SUPPRESS)

    # Card & Hand metadata
    p.add_argument("--manifest", default=None, help="Path to card/hand JSON manifest file")
    p.add_argument("--hand-id", default=None, help="Hand ID to register (kind=async_poll)")
    p.add_argument("--card-id", default=None, help="Card ID to register/update")
    p.add_argument("--title", default=None, help="Human/Agent readable card title")
    p.add_argument("--price-micros", type=int, default=None, help="Card price in micro-USD (1_000_000 = $1.00)")
    p.add_argument("--gate", default=None, help=f"Capability gate category (default: {DEFAULT_GATE})")
    p.add_argument("--inputs-spec", "--inputs", dest="inputs_spec", default=None, help="Inputs contract description/schema")
    p.add_argument("--outputs-spec", "--outputs", dest="outputs_spec", default=None, help="Outputs & commitment boundary spec")
    p.add_argument("--mechanism", default=None, help="How the capability works")
    p.add_argument("--preconditions", default=None, help="Preconditions required before calling")
    p.add_argument("--side-effects", default=None, help="Real-world side effects")
    p.add_argument("--presence-note", default=None, help="Presence note while online")
    p.add_argument("--away-note", default=None, help="Presence note set on shutdown")
    p.add_argument("--private", action="store_true", help="Register card with is_public=False")
    p.add_argument("--skip-card", action="store_true", help="Only register/poll hand without upserting card")

    # 4 Execution adapters
    p.add_argument("--http-upstream", default=None, help="Adapter A: upstream HTTP(S) URL to forward inputs to")
    p.add_argument("--http-method", default=None, choices=["POST", "GET", "post", "get"], help="HTTP method for --http-upstream")
    p.add_argument("--http-header", action="append", default=[], help="Extra header 'Name: Value' for --http-upstream")
    p.add_argument("--cmd", "--exec", dest="cmd", default=None, help="Adapter B: shell/CLI command receiving inputs JSON on stdin")
    p.add_argument("--python-target", default=None, help="Adapter C: '<file_or_module>:<callable>' invoked in-process")
    p.add_argument("--mcp-stdio", default=None, help="Adapter D: local MCP stdio server command")
    p.add_argument("--mcp-tool", default=None, help="Adapter D: MCP tool name to invoke via tools/call")

    # Runtime tuning
    p.add_argument("--exec-timeout", type=float, default=None, help="Execution timeout in seconds (default: 30)")
    p.add_argument("--poll-timeout", type=float, default=20.0, help="Long-poll timeout in seconds (default: 20)")
    p.add_argument("--once", action="store_true", help="Fulfill one batch of events and exit cleanly")
    return p


def _extract_and_scrub_account_token(argv: list[str]) -> tuple[str, list[str]]:
    extracted = ""
    cleaned = list(argv)
    idx = 0
    while idx < len(cleaned):
        arg = cleaned[idx]
        if arg == "--account-token" and idx + 1 < len(cleaned):
            extracted = cleaned[idx + 1].strip()
            cleaned[idx + 1] = "REDACTED"
            idx += 2
            continue
        if arg.startswith("--account-token="):
            _, _, val = arg.partition("=")
            extracted = val.strip()
            cleaned[idx] = "--account-token=REDACTED"
            idx += 1
            continue
        idx += 1
    return extracted, cleaned


def main(argv: list[str] | None = None, *, log_stream: Any = None) -> int:
    target_argv = sys.argv[1:] if argv is None else list(argv)
    argv_acct_tok, pre_scrubbed = _extract_and_scrub_account_token(target_argv)
    argv_tok, scrubbed_argv = scrub_cli_argv_tokens(pre_scrubbed, log_stream=log_stream)
    if argv is None and scrubbed_argv != target_argv:
        sys.argv[1:] = scrubbed_argv

    parser = build_arg_parser()
    args = parser.parse_args(scrubbed_argv)

    try:
        manifest: dict[str, Any] = {}
        if args.manifest:
            mpath = Path(args.manifest).expanduser()
            if not mpath.is_file():
                raise BridgeError(f"Manifest file not found: {mpath}")
            loaded = json.loads(mpath.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise BridgeError("Manifest JSON root must be an object")
            manifest = loaded

        store_url = (
            args.store_url
            or os.environ.get("BAZAAR_STORE_URL")
            or manifest.get("store_url")
            or DEFAULT_STORE_URL
        ).strip().rstrip("/")

        explicit_acct_tok = argv_acct_tok
        if not explicit_acct_tok and args.account_token_file:
            afp = Path(args.account_token_file).expanduser()
            if not afp.is_file():
                raise BridgeError(f"--account-token-file does not exist: {afp}")
            explicit_acct_tok = _read_local_account_session_token(afp) or _read_store_key_file(afp)

        token, _ = resolve_or_onboard_api_key(
            store_url=store_url,
            key_file=args.key_file,
            seat_key_file=args.seat_key_file,
            auto_onboard=bool(args.auto_onboard),
            owner_label=args.owner_label,
            onboarding_url=args.onboarding_url,
            argv_token=argv_tok,
            account_token=explicit_acct_tok,
            bind_local_account=bool(args.bind_local_account),
            log_stream=log_stream,
        )

        raw_hands_manifest = manifest.get("hands")
        if isinstance(raw_hands_manifest, list) and raw_hands_manifest:
            hand_pairs: list[tuple[CardHandSpec, EdgeAdapter]] = []
            for entry in raw_hands_manifest:
                if not isinstance(entry, dict):
                    raise BridgeError("Each entry in manifest 'hands' must be a JSON object")
                entry_adapter, entry_slug = build_adapter_from_config(args, entry)
                entry_spec = CardHandSpec.from_manifest_and_args(entry, None, default_slug=entry_slug)
                hand_pairs.append((entry_spec, entry_adapter))
            bridge = EdgeHandBridge(
                store_url=store_url,
                token=token,
                hands=hand_pairs,
                poll_timeout=args.poll_timeout,
                install_signals=True,
                log_stream=log_stream,
            )
        else:
            adapter, default_slug = build_adapter_from_config(args, manifest)
            spec = CardHandSpec.from_manifest_and_args(manifest, args, default_slug=default_slug)
            bridge = EdgeHandBridge(
                store_url=store_url,
                token=token,
                spec=spec,
                adapter=adapter,
                poll_timeout=args.poll_timeout,
                install_signals=True,
                log_stream=log_stream,
            )
        return bridge.run_forever(once=bool(args.once))
    except BridgeError as err:
        _log(f"ERROR: {err}", stream=log_stream)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
