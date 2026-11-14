#!/usr/bin/env python3
"""Zero-dependency Stdio <-> Streamable HTTP MCP bridge for Causal Store v2.

Reads Bearer tokens dynamically at request time from validated environment
variables or the `0600` keyring files (`~/.causal-store-v2/buyer_key` for
discovery/use/$0 recipes, `~/.causal-store-v2/edge_seat_key` when
`bazaar_publish_card` has `price_micros > 0` or a non-empty `hand_id`, and
optional `~/.causal-store-v2/account_session.json` fallback).

Hardened against common Personal-Agent & IDE MCP failure modes:
  1. Unexpanded template strings (`${BAZAAR_API_KEY}`), `REDACTED`, `null`,
     quoted tokens, or `Bearer ` prefixes in `$BAZAAR_API_KEY` are sanitized or
     ignored so valid `0600` keyring files on disk take effect immediately.
  2. Stale/revoked `$BAZAAR_API_KEY` tokens (`401`), wrong-scope tokens (`403`
     operation permission), or unbound/wrong-seat env tokens hitting
     pre-execution `404 not_found` on same-account private cards (`is_public=0`)
     automatically fail over to `0600` keyring tokens while reusing the exact
     caller payload (never generating a new `idem_key`, and never retrying if a
     `run_id` or `unknown`/`fail` execution outcome is already present).
  3. Keyring & session tokens are only transmitted over HTTPS or loopback
     (`127.0.0.1` / `localhost` / `::1`), never over cleartext remote HTTP or
     unsafe URL schemes, and are always redacted from error logs.
  4. Dead local proxy environment variables (`HTTP_PROXY` / `HTTPS_PROXY`)
     transparently retry once with `ProxyHandler({})` and a fresh `Request`.

Pure Python 3.12+ standard library only (zero third-party dependencies).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import sys
from typing import Any, Sequence
import urllib.error
import urllib.parse
import urllib.request

BRIDGE_VERSION = "2.3.0"
DEFAULT_MCP_URL = "https://store-v2.206.189.67.53.sslip.io/mcp"
DEFAULT_KEYRING_DIR = Path.home() / ".causal-store-v2"
DEFAULT_KEY_FILE = DEFAULT_KEYRING_DIR / "buyer_key"
DEFAULT_SELLER_KEY_FILE = DEFAULT_KEYRING_DIR / "edge_seat_key"
DEFAULT_TIMEOUT_SEC = 65.0

_TOK_PREFIX = "sk2" + "_"
_SESS_PREFIX = "bacs" + "_"
_SK_PREFIX = "sk" + "-"
_GH_PREFIX = "gh" + "p_"
_RE_STORE_TOK = re.compile(_TOK_PREFIX + r"[A-Za-z0-9_-]{16,}")
_RE_SESS_TOK = re.compile(_SESS_PREFIX + r"[A-Za-z0-9_-]{16,}")
_RE_SK_TOK = re.compile(_SK_PREFIX + r"[A-Za-z0-9_-]{20,}")
_RE_GH_TOK = re.compile(r"(?:" + _GH_PREFIX[:-1] + r"|gho|ghu|ghs)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_RE_BEARER_HDR = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=:-]{8,}")

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_INVALID_TOKEN_SENTINELS: frozenset[str] = frozenset(
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


def _redact_text(text: str, extra_secrets: Sequence[str] = ()) -> str:
    out = str(text or "")
    for sec in extra_secrets:
        if sec and len(sec) >= 8 and sec in out:
            out = out.replace(sec, "[REDACTED:KEY]")
    out = _RE_BEARER_HDR.sub("Bearer [REDACTED]", out)
    out = _RE_STORE_TOK.sub("[REDACTED:STORE_KEY]", out)
    out = _RE_SESS_TOK.sub("[REDACTED:SESSION_TOKEN]", out)
    out = _RE_SK_TOK.sub("[REDACTED:API_KEY]", out)
    out = _RE_GH_TOK.sub("[REDACTED:GH_TOKEN]", out)
    return out


def normalize_bearer_token(raw: str | None) -> str:
    """Normalize a candidate bearer token and reject template/sentinel placeholders."""
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


def _validate_mcp_url(url: str, *, environ: MappingLike | None = None) -> str:
    """Ensure MCP URL uses HTTPS or loopback HTTP so tokens never traverse cleartext networks."""
    cleaned = str(url or "").strip()
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("https", "http") or not host:
        raise ValueError(f"Unsupported or invalid MCP URL scheme/host: {cleaned!r}")
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        env_map = environ if environ is not None else os.environ
        allow_insecure = str(env_map.get("BAZAAR_ALLOW_INSECURE_HTTP", "")).strip().lower() in ("1", "true", "yes")
        if not allow_insecure:
            raise ValueError(
                f"Refusing to transmit keyring credentials over cleartext HTTP to non-loopback host {host!r}; use https:// or loopback"
            )
    return cleaned


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block unsafe redirect targets and strip Authorization across origin changes."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        orig = urllib.parse.urlsplit(req.full_url)
        dest = urllib.parse.urlsplit(newurl)
        if orig.scheme.lower() == "https" and dest.scheme.lower() != "https":
            raise urllib.error.URLError("Refusing HTTPS -> HTTP downgrade redirect on MCP endpoint")
        try:
            _validate_mcp_url(newurl)
        except ValueError as exc:
            raise urllib.error.URLError(str(exc)) from exc
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (orig.scheme.lower(), orig.netloc.lower()) != (dest.scheme.lower(), dest.netloc.lower()):
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


def _read_key_file(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        resolved = path.expanduser()
        if not resolved.is_file():
            return ""
        raw = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"[causal-bazaar-mcp] failed reading key file: {_redact_text(str(exc))}", file=sys.stderr, flush=True)
        return ""
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(obj, dict):
            legacy_key_field = _TOK_PREFIX + "key"
            for k in (
                "token",
                "api_key",
                "buyer_key",
                "buyer_token",
                "seat_key",
                "seller_token",
                legacy_key_field,
                "session_token",
            ):
                v = obj.get(k)
                if isinstance(v, str):
                    norm = normalize_bearer_token(v)
                    if norm:
                        return norm
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return normalize_bearer_token(stripped)
    return ""


def _read_account_session_tokens(session_file: Path | None) -> tuple[str, str, list[str]]:
    """Extract `(buyer_or_session_tok, seller_tok, all_secrets)` from `account_session.json` if present."""
    if session_file is None:
        return "", "", []
    try:
        resolved = session_file.expanduser()
        if not resolved.is_file():
            return "", "", []
        raw = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        return "", "", []
    if not raw or not raw.startswith("{"):
        return "", "", []
    try:
        obj = json.loads(raw)
    except ValueError:
        return "", "", []
    if not isinstance(obj, dict):
        return "", "", []

    legacy_key_field = _TOK_PREFIX + "key"
    secrets_found: list[str] = []
    for k in (
        "buyer_key",
        "buyer_token",
        "token",
        "api_key",
        legacy_key_field,
        "seat_key",
        "seller_token",
        "session_token",
    ):
        v = obj.get(k)
        if isinstance(v, str):
            norm = normalize_bearer_token(v)
            if norm and norm not in secrets_found:
                secrets_found.append(norm)

    buyer_cand = ""
    for k in ("buyer_key", "buyer_token", "token", "api_key", legacy_key_field, "session_token"):
        v = obj.get(k)
        if isinstance(v, str):
            norm = normalize_bearer_token(v)
            if norm:
                buyer_cand = norm
                break

    seller_cand = ""
    for k in ("seat_key", "seller_token"):
        v = obj.get(k)
        if isinstance(v, str):
            norm = normalize_bearer_token(v)
            if norm:
                seller_cand = norm
                break

    return buyer_cand, seller_cand, secrets_found


class MappingLike:  # pragma: no cover - structural typing alias helper
    def get(self, key: str, default: str = "") -> str:
        return default


def _env_token(environ: MappingLike | None = None, *names: str) -> str:
    env_map = environ if environ is not None else os.environ
    for name in names:
        cand = normalize_bearer_token(env_map.get(name, ""))
        if cand:
            return cand
    return ""


def _load_bearer_token(
    key_file: Path,
    seller_key_file: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    candidates = _ordered_token_candidates(
        key_file=key_file,
        seller_key_file=seller_key_file,
        needs_seller=False,
        env=env,
    )
    return candidates[0] if candidates else ""


def _ordered_token_candidates(
    *,
    key_file: Path,
    seller_key_file: Path | None,
    needs_seller: bool,
    preferred_token: str = "",
    env: dict[str, str] | None = None,
    session_file: Path | None = None,
) -> list[str]:
    environ = env if env is not None else os.environ
    prefer_file = str(environ.get("BAZAAR_KEYRING_PREFER_FILE", "")).strip().lower() in ("1", "true", "yes")
    env_tok = _env_token(environ, "BAZAAR_API_KEY", "CAUSAL_STORE_API_KEY")
    seller_env_tok = _env_token(environ, "BAZAAR_SELLER_KEY", "BAZAAR_EDGE_SEAT_KEY")
    buyer_tok = _read_key_file(key_file)
    seller_tok = _read_key_file(seller_key_file)

    eff_session_file = session_file if session_file is not None else (key_file.expanduser().parent / "account_session.json")
    sess_buyer_tok, sess_seller_tok, _ = _read_account_session_tokens(eff_session_file)

    raw_order: list[str] = []
    if needs_seller:
        raw_order.extend([seller_env_tok, seller_tok, sess_seller_tok])
        if prefer_file:
            raw_order.extend([buyer_tok, sess_buyer_tok, env_tok])
        elif env_tok and not env_tok.startswith(_TOK_PREFIX) and buyer_tok.startswith(_TOK_PREFIX):
            raw_order.extend([buyer_tok, sess_buyer_tok, env_tok])
        else:
            raw_order.extend([env_tok, buyer_tok, sess_buyer_tok])
    else:
        if preferred_token:
            raw_order.append(preferred_token)
        if prefer_file:
            raw_order.extend([buyer_tok, sess_buyer_tok, seller_tok, sess_seller_tok, env_tok, seller_env_tok])
        elif env_tok and not env_tok.startswith(_TOK_PREFIX) and (
            buyer_tok.startswith(_TOK_PREFIX) or seller_tok.startswith(_TOK_PREFIX)
        ):
            # A non-store token string in BAZAAR_API_KEY must not shadow a real 0600 store key file.
            raw_order.extend([buyer_tok, sess_buyer_tok, seller_tok, sess_seller_tok, env_tok, seller_env_tok])
        else:
            raw_order.extend([env_tok, buyer_tok, sess_buyer_tok, seller_tok, sess_seller_tok, seller_env_tok])

    dedup: list[str] = []
    for tok in raw_order:
        if tok and tok not in dedup:
            dedup.append(tok)
    return dedup


def _item_needs_seller_key(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("method") != "tools/call":
        return False
    params = item.get("params")
    if not isinstance(params, dict) or params.get("name") != "bazaar_publish_card":
        return False
    args = params.get("arguments")
    if not isinstance(args, dict):
        return False
    raw_price = args.get("price_micros")
    has_price = isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool) and raw_price > 0
    has_hand = bool(str(args.get("hand_id") or "").strip())
    return has_price or has_hand


def _needs_seller_key(msg: Any) -> bool:
    if isinstance(msg, list):
        return any(_item_needs_seller_key(x) for x in msg)
    return _item_needs_seller_key(msg)


def _extract_tool_error_meta(reply: Any) -> dict[str, Any]:
    """Extract normalized tool error metadata from a JSON-RPC reply."""
    meta: dict[str, Any] = {
        "is_err": False,
        "code": "",
        "http_status": 0,
        "run_id": "",
        "outcome": "",
        "status": "",
        "error": "",
        "charged_micros": 0,
        "aborted_before_invoke": False,
        "has_reconciliation_warning": False,
    }
    if not isinstance(reply, dict):
        return meta
    result = reply.get("result")
    if not isinstance(result, dict) or not result.get("isError"):
        return meta
    meta["is_err"] = True
    sc = result.get("structuredContent")
    if not isinstance(sc, dict):
        for block in result.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                    if isinstance(parsed, dict):
                        sc = parsed
                        break
                except ValueError:
                    pass
    if not isinstance(sc, dict):
        return meta

    meta["code"] = str(sc.get("code") or "").strip().lower()
    try:
        meta["http_status"] = int(sc.get("http_status") or 0)
    except (TypeError, ValueError):
        meta["http_status"] = 0
    meta["run_id"] = str(sc.get("run_id") or "").strip()
    meta["outcome"] = str(sc.get("outcome") or "").strip().lower()
    meta["status"] = str(sc.get("status") or "").strip().lower()
    meta["error"] = str(sc.get("error") or "").strip().lower()
    meta["aborted_before_invoke"] = bool(sc.get("aborted_before_invoke"))
    meta["has_reconciliation_warning"] = bool(sc.get("reconciliation_warning"))

    for amt_field in ("charged_micros", "paid_micros", "spend_micros"):
        raw_amt = sc.get(amt_field)
        if isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool) and raw_amt > meta["charged_micros"]:
            meta["charged_micros"] = int(raw_amt)
    return meta


def _should_retry_with_fallback_token(replies: list[Any]) -> bool:
    """True ONLY when a tool call failed BEFORE execution due to key auth/scope/visibility."""
    if len(replies) != 1:
        return False
    meta = _extract_tool_error_meta(replies[0])
    if not meta["is_err"]:
        return False
    # NEVER retry if a run already executed, minted a run_id, timed out as unknown, or incurred charges.
    if (
        meta["run_id"]
        or meta["outcome"] in ("ok", "fail", "unknown")
        or meta["status"] in ("ok", "fail", "unknown", "recipe", "away")
        or meta["has_reconciliation_warning"]
        or meta["charged_micros"] > 0
        or meta["aborted_before_invoke"]
    ):
        return False

    code = meta["code"]
    http_status = meta["http_status"]
    err_txt = meta["error"]

    if code in (
        "exceeds_max_price_micros",
        "bad_request",
        "conflict",
        "self_deadlock_hand_id",
        "micro_step_granularity_rejected",
    ):
        return False

    # Never bypass wallet balance or per-call/daily spending guardrails by failing over to another key.
    if "balance_micros" in err_txt or "limit_micros" in err_txt:
        return False

    if http_status in (401, 403, 404):
        return True
    return code in ("unauthorized", "forbidden", "not_found")


def _is_op_permission_rejection(replies: list[Any]) -> bool:
    """True when a fallback seat token was rejected solely because its allow_ops lacks the requested op."""
    if len(replies) != 1:
        return False
    meta = _extract_tool_error_meta(replies[0])
    return bool(meta["is_err"] and "not permitted for operation" in meta["error"])


def _open_url_with_proxy_fallback(
    req: urllib.request.Request,
    *,
    timeout: float,
    ctx: ssl.SSLContext | None = None,
) -> Any:
    safe_handlers: list[urllib.request.BaseHandler] = [_SafeRedirectHandler()]
    if ctx is not None:
        safe_handlers.append(urllib.request.HTTPSHandler(context=ctx))
    default_opener = urllib.request.build_opener(*safe_handlers)
    try:
        return default_opener.open(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as err:
        reason_txt = str(err).lower()
        if "downgrade redirect" in reason_txt or "unsupported or invalid mcp url" in reason_txt:
            raise
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
        direct_handlers: list[urllib.request.BaseHandler] = [
            urllib.request.ProxyHandler({}),
            _SafeRedirectHandler(),
        ]
        if ctx is not None:
            direct_handlers.append(urllib.request.HTTPSHandler(context=ctx))
        direct_opener = urllib.request.build_opener(*direct_handlers)
        return direct_opener.open(fresh_req, timeout=timeout)


def _read_framed_message(stream: Any = None) -> tuple[Any, str, bool]:
    """Read one JSON-RPC message from stdin (newline-delimited JSON or LSP Content-Length framing)."""
    buf = stream if stream is not None else sys.stdin.buffer
    while True:
        raw_line = buf.readline()
        if not raw_line:
            return None, "line", True

        stripped = raw_line.strip()
        if not stripped:
            continue

        lower = stripped.lower()
        if lower.startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                return {"_parse_error": "Invalid Content-Length header"}, "cl", False
            if length < 0 or length > 16 * 1024 * 1024:
                return {"_parse_error": "Content-Length out of bounds"}, "cl", False
            while True:
                hdr_line = buf.readline()
                if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                    break
            body = buf.read(length)
            if len(body) < length:
                return None, "cl", True
            try:
                return json.loads(body.decode("utf-8")), "cl", False
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return {"_parse_error": f"Parse error: {exc}"}, "cl", False

        try:
            return json.loads(stripped.decode("utf-8")), "line", False
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"_parse_error": f"Parse error: {exc}"}, "line", False


def _write_message(payload: Any, framing: str, stream: Any = None) -> None:
    out = stream if stream is not None else sys.stdout.buffer
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "cl":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        out.write(header + encoded)
    else:
        out.write(encoded + b"\n")
    out.flush()


def _extract_sse_json(raw_body: bytes) -> list[Any]:
    events: list[Any] = []
    data_lines: list[str] = []
    for line in raw_body.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            chunk = "\n".join(data_lines).strip()
            data_lines.clear()
            if chunk:
                try:
                    events.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass
    if data_lines:
        chunk = "\n".join(data_lines).strip()
        if chunk:
            try:
                events.append(json.loads(chunk))
            except json.JSONDecodeError:
                pass
    return events


class BridgeState:
    def __init__(
        self,
        mcp_url: str,
        key_file: Path,
        timeout_sec: float,
        seller_key_file: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        session_file: Path | None = None,
    ) -> None:
        self.mcp_url = mcp_url
        self.key_file = key_file.expanduser()
        self.seller_key_file = (seller_key_file or DEFAULT_SELLER_KEY_FILE).expanduser()
        self.session_file = (
            session_file.expanduser()
            if session_file is not None
            else (self.key_file.parent / "account_session.json")
        )
        self.timeout_sec = timeout_sec
        self.session_id = ""
        self._env = env
        self._preferred_token = ""

    def candidate_tokens(self, msg: Any) -> list[str]:
        return _ordered_token_candidates(
            key_file=self.key_file,
            seller_key_file=self.seller_key_file,
            needs_seller=_needs_seller_key(msg),
            preferred_token=self._preferred_token,
            env=self._env,
            session_file=self.session_file,
        )

    def _all_known_secrets(self, active_tokens: Sequence[str]) -> list[str]:
        known = list(active_tokens)
        _, _, sess_secrets = _read_account_session_tokens(self.session_file)
        for sec in sess_secrets:
            if sec and sec not in known:
                known.append(sec)
        return known

    def select_token(self, msg: Any) -> str:
        cands = self.candidate_tokens(msg)
        return cands[0] if cands else ""

    def _post_once(self, target_url: str, body_bytes: bytes, token: str) -> tuple[int, list[Any] | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"causal-bazaar-mcp-stdio-bridge/{BRIDGE_VERSION}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method="POST")
        ctx = ssl.create_default_context() if target_url.lower().startswith("https://") else None
        try:
            with _open_url_with_proxy_fallback(req, timeout=self.timeout_sec, ctx=ctx) as resp:
                sess = (resp.headers.get("Mcp-Session-Id") or "").strip()
                if sess:
                    self.session_id = sess
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read()
                if resp.status == 202 or not raw.strip():
                    return int(resp.status), []
                if "text/event-stream" in ctype:
                    return int(resp.status), _extract_sse_json(raw)
                return int(resp.status), [json.loads(raw.decode("utf-8"))]
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read() if exc.fp else b""
            finally:
                exc.close()
            if raw.strip():
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0":
                        return int(exc.code), [parsed]
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            return int(exc.code), None

    def forward(self, msg: Any) -> list[Any]:
        tokens = self.candidate_tokens(msg)
        all_secrets = self._all_known_secrets(tokens)
        try:
            validated_url = _validate_mcp_url(self.mcp_url, environ=self._env)
        except ValueError as exc:
            safe_err = _redact_text(str(exc), all_secrets)
            return self._error_for_request(msg, f"MCP endpoint security error: {safe_err}")

        # Serialize caller payload ONCE before attempts so retries reuse the exact payload & idem_key.
        body_bytes = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        attempt_tokens = tokens if tokens else [""]
        needs_seller = _needs_seller_key(msg)

        primary_replies: list[Any] | None = None
        last_replies: list[Any] | None = None
        last_http_code = 0

        for idx, tok in enumerate(attempt_tokens):
            try:
                http_code, replies = self._post_once(validated_url, body_bytes, tok)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                safe_err = _redact_text(str(exc), all_secrets)
                return self._error_for_request(msg, f"MCP upstream error: {safe_err}")

            last_http_code = http_code
            has_more = idx + 1 < len(attempt_tokens)

            if replies is None:
                if has_more and http_code in (401, 403):
                    continue
                if primary_replies is not None:
                    return primary_replies
                return self._error_for_request(msg, f"HTTP {http_code} from MCP endpoint")

            if primary_replies is None and not _is_op_permission_rejection(replies):
                primary_replies = replies
            if not _is_op_permission_rejection(replies) or last_replies is None:
                last_replies = replies

            if has_more and _should_retry_with_fallback_token(replies):
                continue

            if idx > 0 and tok and not needs_seller and not _should_retry_with_fallback_token(replies):
                # Promote working keyring fallback token so subsequent calls succeed on first attempt.
                self._preferred_token = tok
                return replies

            if _is_op_permission_rejection(replies) and primary_replies is not None:
                return primary_replies
            return replies

        if primary_replies is not None:
            return primary_replies
        if last_replies is not None:
            return last_replies
        return self._error_for_request(msg, f"HTTP {last_http_code} from MCP endpoint")

    @staticmethod
    def _error_for_request(msg: Any, message: str) -> list[Any]:
        if isinstance(msg, dict) and "id" in msg:
            return [{"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": message}}]
        return []


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp_stdio_bridge.py",
        description="Zero-dependency Stdio <-> Streamable HTTP MCP bridge for Causal Store v2",
    )
    p.add_argument("--version", action="version", version=f"causal-bazaar-mcp-stdio-bridge/{BRIDGE_VERSION}")
    p.add_argument("--mcp-url", default=None, help=f"Streamable HTTP MCP URL (default: $BAZAAR_MCP_URL or {DEFAULT_MCP_URL})")
    p.add_argument("--keyring-dir", default=None, help="0600 keyring directory (~/.causal-store-v2)")
    p.add_argument("--key-file", default=None, help="Buyer 0600 key file path override")
    p.add_argument("--seller-key-file", default=None, help="Seller 0600 seat key file path override")
    p.add_argument("--timeout-sec", type=float, default=None, help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SEC})")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    mcp_url = (args.mcp_url or os.environ.get("BAZAAR_MCP_URL") or DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL
    kr_raw = (args.keyring_dir or os.environ.get("BAZAAR_KEYRING_DIR") or "").strip()
    kr_dir = Path(kr_raw).expanduser() if kr_raw else DEFAULT_KEYRING_DIR.expanduser()

    key_env = (args.key_file or os.environ.get("BAZAAR_KEY_FILE") or "").strip()
    key_file = Path(key_env).expanduser() if key_env else (kr_dir / "buyer_key" if kr_raw else DEFAULT_KEY_FILE.expanduser())

    seller_env = (
        args.seller_key_file
        or os.environ.get("BAZAAR_SELLER_KEY_FILE")
        or os.environ.get("BAZAAR_EDGE_SEAT_KEY_FILE")
        or ""
    ).strip()
    seller_key_file = (
        Path(seller_env).expanduser()
        if seller_env
        else (kr_dir / "edge_seat_key" if kr_raw else DEFAULT_SELLER_KEY_FILE.expanduser())
    )

    timeout_sec = (
        float(args.timeout_sec)
        if args.timeout_sec is not None
        else float(os.environ.get("BAZAAR_MCP_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SEC)))
    )
    state = BridgeState(
        mcp_url=mcp_url,
        key_file=key_file,
        timeout_sec=timeout_sec,
        seller_key_file=seller_key_file,
    )

    while True:
        msg, framing, eof = _read_framed_message()
        if eof:
            break
        if isinstance(msg, dict) and "_parse_error" in msg and len(msg) == 1:
            _write_message(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": msg["_parse_error"]}},
                framing,
            )
            continue
        for reply in state.forward(msg):
            _write_message(reply, framing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
