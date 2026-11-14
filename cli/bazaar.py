#!/usr/bin/env python3
"""Causal Bazaar Standalone CLI (`bazaar`).

Subcommands:
  - login     1-click human account & wallet binding (/claim/XXXX-XXXX -> 0600 keys)
  - wallet    Inspect unified wallet credits, spend guardrails & capability counts
  - find      Search live proven hands, pitfall_warnings & wholesale catalogs
  - use       Invoke a capability with automatic idem_key & spend ceiling
  - receipt   Reconcile unknown runs or attach signed qualitative pitfall memos
  - publish   Publish a $0 proven playbook, dead-end warning, or live edge hand
  - toggle    Flip capabilities between 🔒 Private, 🌐 Public, 🟢 Online & ⏸️ Paused
  - mount     Mount any local HTTP / CLI / Python / MCP workflow in 10 seconds
  - whoami    Inspect active key identity & raw store limits

Pure Python 3.12+ standard library only (zero third-party dependencies).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

CLI_VERSION = "2.1.0"
DEFAULT_STORE_URL = "https://store-v2.206.189.67.53.sslip.io"
DEFAULT_KEYRING_DIR = str(Path.home() / ".causal-store-v2")
DEFAULT_BEARER_ENV_VAR = "BAZAAR_API_KEY"

_SELF_DIR = Path(__file__).resolve().parent
_DEFAULT_BRIDGE_CANDIDATE = (
    _SELF_DIR / "bazaar_edge_hand.py"
    if (_SELF_DIR / "bazaar_edge_hand.py").is_file()
    else (_SELF_DIR.parent / "bridge" / "bazaar_edge_hand.py")
)

_TOK_PREFIX_A = "sk2" + "_"
_TOK_PREFIX_GH = "gh" + "p_"
_RE_STORE_TOKEN = re.compile(_TOK_PREFIX_A + r"[A-Za-z0-9_-]{16,}")
_RE_SK_TOKEN = re.compile(r"sk-(?:live_|test_)?[A-Za-z0-9_-]{16,}")
_RE_GH_TOKEN = re.compile(r"(?:" + _TOK_PREFIX_GH[:-1] + r"|gho|ghu|ghs)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_RE_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIV" + r"ATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIV" + r"ATE KEY-----")
_RE_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=:-]{8,}")
_RE_TINY_TAP_SPAM = re.compile(
    r"^(?:tap|click|double[- ]?tap|swipe|scroll|sleep|wait|press\s+key|keyevent)\b",
    re.IGNORECASE,
)


def _redact(value: Any, extra: list[str] | None = None, depth: int = 0) -> Any:
    if depth > 24:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        out = value
        for tok in extra or []:
            if tok and len(tok) >= 8 and tok in out:
                out = out.replace(tok, "[REDACTED:KEY]")
        out = _RE_PEM_BLOCK.sub("[REDACTED:PEM_KEY]", out)
        out = _RE_BEARER.sub("Bearer [REDACTED]", out)
        out = _RE_STORE_TOKEN.sub("[REDACTED:STORE_KEY]", out)
        out = _RE_SK_TOKEN.sub("[REDACTED:API_KEY]", out)
        out = _RE_GH_TOKEN.sub("[REDACTED:GH_TOKEN]", out)
        return out
    if isinstance(value, dict):
        return {str(_redact(str(k), extra, depth + 1)): _redact(v, extra, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, extra, depth + 1) for v in value]
    return _redact(str(value), extra, depth + 1)


def _write_atomic_0600(path: Path, content: str) -> Path:
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    fd, tmp_str = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            os.chmod(tmp_str, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_str, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise
    return target


def _write_env_sh_0600(kr_dir: Path, store_url: str, bearer_env_var: str = DEFAULT_BEARER_ENV_VAR) -> Path:
    buyer_path = kr_dir / "buyer_key"
    seat_path = kr_dir / "edge_seat_key"
    env_sh_path = kr_dir / "env.sh"
    q_buyer = shlex.quote(str(buyer_path))
    q_seat = shlex.quote(str(seat_path))
    q_store = shlex.quote(store_url)
    env_text = (
        "# Managed by Causal Bazaar CLI (mode 0600, zero inline secrets)\n"
        f"export BAZAAR_STORE_URL={q_store}\n"
        f"export BAZAAR_EDGE_SEAT_KEY_FILE={q_seat}\n"
        f"if [ -f {q_buyer} ]; then\n"
        f"  export {bearer_env_var}=\"$(tr -d '\\r\\n' < {q_buyer})\"\n"
        f"elif [ -f {q_seat} ]; then\n"
        f"  export {bearer_env_var}=\"$(tr -d '\\r\\n' < {q_seat})\"\n"
        f"fi\n"
    )
    return _write_atomic_0600(env_sh_path, env_text)


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


def _normalize_token(raw: str | None) -> str:
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


def _read_key_file(path: Path) -> str:
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(obj, dict):
            legacy_field = "sk2" + "_key"
            for k in ("token", "api_key", "buyer_key", "buyer_token", "seat_key", "seller_token", legacy_field):
                v = obj.get(k)
                if isinstance(v, str):
                    norm = _normalize_token(v)
                    if norm:
                        return norm
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return _normalize_token(stripped)
    return ""


def _keyring_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "keyring_dir", None) or os.environ.get("BAZAAR_KEYRING_DIR") or DEFAULT_KEYRING_DIR).expanduser()


def _resolve_token(args: argparse.Namespace, *, prefer_seller_seat: bool = False) -> tuple[str, list[str]]:
    kr_dir = _keyring_dir(args)
    buyer_file = kr_dir / "buyer_key"
    explicit_kr = bool(getattr(args, "keyring_dir", None))
    env_seller_file = (
        ""
        if explicit_kr
        else (
            os.environ.get("BAZAAR_SELLER_KEY_FILE", "").strip()
            or os.environ.get("BAZAAR_EDGE_SEAT_KEY_FILE", "").strip()
        )
    )
    seat_file = Path(env_seller_file).expanduser() if env_seller_file else (kr_dir / "edge_seat_key")
    explicit_file = getattr(args, "key_file", None)

    all_known: list[str] = []
    for p in (buyer_file, seat_file, kr_dir / "edge_seat_key"):
        t = _read_key_file(p)
        if t and t not in all_known:
            all_known.append(t)

    if explicit_file:
        kp = Path(explicit_file).expanduser()
        tok = _read_key_file(kp)
        if not tok:
            raise RuntimeError(f"--key-file not found or empty: {kp}")
        all_known.append(tok)
        return tok, all_known

    env_tok = (
        _normalize_token(os.environ.get(DEFAULT_BEARER_ENV_VAR, ""))
        or _normalize_token(os.environ.get("BAZAAR_API_KEY", ""))
        or _normalize_token(os.environ.get("CAUSAL_STORE_API_KEY", ""))
    )
    seller_env_tok = (
        _normalize_token(os.environ.get("BAZAAR_SELLER_KEY", ""))
        or _normalize_token(os.environ.get("BAZAAR_EDGE_SEAT_KEY", ""))
    )
    for extra_t in (env_tok, seller_env_tok):
        if extra_t and extra_t not in all_known:
            all_known.append(extra_t)

    buyer_tok = _read_key_file(buyer_file)
    seat_tok = _read_key_file(seat_file) or _read_key_file(kr_dir / "edge_seat_key")

    if prefer_seller_seat:
        for cand in (seller_env_tok, seat_tok, env_tok, buyer_tok):
            if cand:
                return cand, all_known
        return "", all_known

    if env_tok and not env_tok.startswith(_TOK_PREFIX_A) and (
        buyer_tok.startswith(_TOK_PREFIX_A) or seat_tok.startswith(_TOK_PREFIX_A)
    ):
        for cand in (buyer_tok, seat_tok, env_tok, seller_env_tok):
            if cand:
                return cand, all_known

    for cand in (env_tok, buyer_tok, seat_tok, seller_env_tok):
        if cand:
            return cand, all_known
    return "", all_known


def _open_url_with_proxy_fallback(
    req: urllib.request.Request,
    *,
    timeout: float,
    ctx: ssl.SSLContext | None = None,
) -> Any:
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


def _http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 25.0,
) -> tuple[int, Any]:
    hdrs: dict[str, str] = {"Accept": "application/json", "User-Agent": f"causal-bazaar-cli/{CLI_VERSION}"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if extra_headers:
        hdrs.update(extra_headers)
    raw_req: bytes | None = None
    if body is not None:
        raw_req = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=raw_req, headers=hdrs, method=method)
    ctx = ssl.create_default_context() if url.startswith("https://") else None
    try:
        with _open_url_with_proxy_fallback(req, timeout=timeout, ctx=ctx) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as err:
        try:
            txt = err.read().decode("utf-8", errors="replace") if err.fp else ""
        finally:
            err.close()
        try:
            parsed = json.loads(txt) if txt.strip() else {"ok": False, "error": str(err)}
        except ValueError:
            parsed = {"ok": False, "error": txt.strip()[:1000] or str(err)}
        return int(err.code), parsed
    except Exception as exc:
        return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _call_mcp_tool(
    store_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    token: str = "",
    timeout: float = 25.0,
) -> tuple[int, bool, dict[str, Any]]:
    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    code, resp = _http_json("POST", f"{store_url.rstrip('/')}/mcp", token=token, body=rpc_body, timeout=timeout)
    if code == 404:
        return 404, True, {"ok": False, "code": "mcp_route_not_found"}
    if code != 200 or not isinstance(resp, dict):
        payload = resp if isinstance(resp, dict) else {"ok": False, "error": str(resp)}
        return code, True, payload
    if "error" in resp:
        return code, True, {"ok": False, "mcp_error": resp["error"]}
    res = resp.get("result") if isinstance(resp.get("result"), dict) else {}
    is_err = bool(res.get("isError", False))
    if isinstance(res.get("structuredContent"), dict):
        return code, is_err, dict(res["structuredContent"])
    content = res.get("content") if isinstance(res.get("content"), list) else []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
                if isinstance(parsed, dict):
                    return code, is_err, parsed
            except ValueError:
                return code, is_err, {"ok": not is_err, "text": block["text"]}
    return code, is_err, dict(res)


def _store_urls(args: argparse.Namespace) -> tuple[str, str]:
    north = (
        getattr(args, "store_url", None)
        or os.environ.get("BAZAAR_STORE_URL")
        or DEFAULT_STORE_URL
    ).strip().rstrip("/")
    south = (
        getattr(args, "southbound_store_url", None)
        or os.environ.get("BAZAAR_SOUTHBOUND_STORE_URL")
        or north
    ).strip().rstrip("/")
    return north, south


def _emit(payload: dict[str, Any], *, secrets_to_scrub: list[str], exit_code: int = 0) -> int:
    clean = _redact(payload, secrets_to_scrub)
    sys.stdout.write(json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    return exit_code


def _extract_claim_tokens(poll_data: dict[str, Any]) -> tuple[str, str]:
    nested_keys = poll_data.get("keys") if isinstance(poll_data.get("keys"), dict) else {}
    pb = poll_data.get("primary_buyer_key") if isinstance(poll_data.get("primary_buyer_key"), dict) else {}
    ps = poll_data.get("primary_seller_key") if isinstance(poll_data.get("primary_seller_key"), dict) else {}
    buyer_tok = str(
        poll_data.get("buyer_token")
        or poll_data.get("buyer_key")
        or nested_keys.get("buyer_token")
        or nested_keys.get("buyer_key")
        or pb.get("token")
        or ""
    ).strip()
    seller_tok = str(
        poll_data.get("seller_token")
        or poll_data.get("seller_key")
        or poll_data.get("seat_key")
        or nested_keys.get("seller_token")
        or nested_keys.get("seller_key")
        or ps.get("token")
        or ""
    ).strip()
    return buyer_tok, seller_tok


def _cmd_login(args: argparse.Namespace) -> int:
    north, _ = _store_urls(args)
    kr_dir = _keyring_dir(args)
    pending_file = kr_dir / "pending_claim.json"

    if args.resume:
        if not pending_file.is_file():
            return _emit(
                {"ok": False, "error": f"No pending claim found at {pending_file}; run `bazaar login` first"},
                secrets_to_scrub=[],
                exit_code=1,
            )
        saved = json.loads(pending_file.read_text(encoding="utf-8"))
        claim_code = str(saved.get("claim_code") or "")
        poll_token = str(saved.get("poll_token") or "")
        claim_url = str(saved.get("claim_url") or f"{north}/claim/{claim_code}")
        expires_in = float(saved.get("expires_in_sec") or 600.0)
    else:
        agent_name = (args.agent_name or "personal-agent").strip()[:64] or "personal-agent"
        host_label = (args.hostname or socket.gethostname() or "local-device").strip()[:120]
        init_code, init_data = _http_json(
            "POST",
            f"{north}/api/account/claim/init",
            body={"agent_name": agent_name, "hostname": host_label},
            timeout=15.0,
        )
        if init_code not in (200, 201) or not isinstance(init_data, dict) or not init_data.get("poll_token"):
            return _emit(
                {
                    "ok": False,
                    "http_status": init_code,
                    "error": (init_data.get("error") if isinstance(init_data, dict) else str(init_data))
                    or "Failed to initialize device claim",
                },
                secrets_to_scrub=[],
                exit_code=1,
            )
        claim_code = str(init_data.get("claim_code") or "").strip()
        poll_token = str(init_data.get("poll_token") or "").strip()
        raw_claim_url = str(init_data.get("claim_url") or "").strip()
        if raw_claim_url.startswith(("http://", "https://")):
            claim_url = raw_claim_url
        elif raw_claim_url.startswith("/"):
            claim_url = f"{north}{raw_claim_url}"
        else:
            claim_url = f"{north}/claim/{claim_code}"
        expires_in = float(init_data.get("expires_in_sec") or 600.0)

        _write_atomic_0600(
            pending_file,
            json.dumps(
                {
                    "store_url": north,
                    "claim_code": claim_code,
                    "poll_token": poll_token,
                    "claim_url": claim_url,
                    "expires_in_sec": expires_in,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        sys.stderr.write(
            f"[bazaar login] Approve in 1 click to bind your Causal Wallet: {claim_url} (code: {claim_code})\n"
        )
        sys.stderr.flush()
        if args.open:
            try:
                webbrowser.open(claim_url)
            except Exception:
                pass
        if args.no_wait:
            return _emit(
                {
                    "ok": True,
                    "status": "pending",
                    "claim_code": claim_code,
                    "claim_url": claim_url,
                    "expires_in_sec": int(expires_in),
                    "next_step": "Click Approve at claim_url in browser, then run `bazaar login --resume`.",
                },
                secrets_to_scrub=[poll_token],
                exit_code=0,
            )

    interval = max(0.05, float(args.poll_interval))
    max_wait = max(interval, min(float(args.timeout), expires_in + 30.0))
    deadline = time.monotonic() + max_wait
    poll_url = f"{north}/api/account/claim/poll?{urllib.parse.urlencode({'poll_token': poll_token})}"

    while True:
        p_code, p_data = _http_json("GET", poll_url, timeout=15.0)
        if isinstance(p_data, dict):
            status = str(p_data.get("status") or "").lower()
            if status == "approved":
                buyer_tok, seller_tok = _extract_claim_tokens(p_data)
                if not buyer_tok and not seller_tok:
                    return _emit(
                        {"ok": False, "error": "Claim approved but server returned no key tokens"},
                        secrets_to_scrub=[poll_token],
                        exit_code=1,
                    )
                buyer_path = kr_dir / "buyer_key"
                seat_path = kr_dir / "edge_seat_key"
                if buyer_tok:
                    _write_atomic_0600(buyer_path, buyer_tok + "\n")
                if seller_tok:
                    _write_atomic_0600(seat_path, seller_tok + "\n")
                env_path = _write_env_sh_0600(kr_dir, north)
                try:
                    pending_file.unlink()
                except OSError:
                    pass
                scrub = [t for t in (poll_token, buyer_tok, seller_tok) if t]
                return _emit(
                    {
                        "ok": True,
                        "status": "approved",
                        "claim_code": claim_code,
                        "claim_url": claim_url,
                        "account_email": p_data.get("account_email"),
                        "display_name": p_data.get("display_name"),
                        "keyring_dir": str(kr_dir),
                        "buyer_key_path": str(buyer_path) if buyer_tok else None,
                        "seller_key_path": str(seat_path) if seller_tok else None,
                        "env_script_path": str(env_path),
                    },
                    secrets_to_scrub=scrub,
                    exit_code=0,
                )
            if status in ("expired", "rejected", "denied"):
                try:
                    pending_file.unlink()
                except OSError:
                    pass
                return _emit(
                    {"ok": False, "status": status, "claim_code": claim_code, "claim_url": claim_url},
                    secrets_to_scrub=[poll_token],
                    exit_code=1,
                )
        if p_code in (404, 410):
            return _emit(
                {"ok": False, "status": "expired", "http_status": p_code, "claim_code": claim_code, "claim_url": claim_url},
                secrets_to_scrub=[poll_token],
                exit_code=1,
            )
        if time.monotonic() >= deadline:
            return _emit(
                {
                    "ok": False,
                    "status": "timeout",
                    "claim_code": claim_code,
                    "claim_url": claim_url,
                    "error": "Timed out waiting for browser approval; run `bazaar login --resume` after clicking Approve.",
                },
                secrets_to_scrub=[poll_token],
                exit_code=1,
            )
        time.sleep(interval)


def _cmd_wallet(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    if not tok:
        return _emit(
            {
                "ok": False,
                "authenticated": False,
                "error": "No wallet key configured in keyring",
                "claim_hint": "Run `bazaar login` to bind your Causal Wallet in 1 click",
                "wallet_url": f"{north}/wallet",
                "console_url": f"{north}/console",
            },
            secrets_to_scrub=scrub,
            exit_code=1,
        )

    me_code, me_data = _http_json("GET", f"{north}/api/account/me", token=tok, timeout=15.0)
    if me_code == 200 and isinstance(me_data, dict) and me_data.get("authenticated") is True:
        for k_field in ("primary_buyer_key", "primary_seller_key"):
            k_obj = me_data.get(k_field)
            if isinstance(k_obj, dict):
                raw_t = k_obj.get("token")
                if isinstance(raw_t, str) and raw_t:
                    scrub.append(raw_t)
                k_obj.pop("token", None)
        if isinstance(me_data.get("session_token"), str) and me_data["session_token"]:
            scrub.append(me_data["session_token"])
            me_data.pop("session_token", None)
        me_data.setdefault("wallet_url", f"{north}/wallet")
        me_data.setdefault("rules_url", f"{north}/rules")
        me_data.setdefault("capabilities_url", f"{north}/capabilities")
        return _emit(me_data, secrets_to_scrub=scrub, exit_code=0)

    m_code, is_err, w_data = _call_mcp_tool(north, "bazaar_whoami", {}, token=tok)
    if m_code == 404:
        r_code, r_data = _http_json("GET", f"{south}/whoami", token=tok)
        w_data = r_data if isinstance(r_data, dict) else {"ok": False}
        is_err = r_code != 200 or not w_data.get("ok")
    if is_err:
        return _emit(w_data, secrets_to_scrub=scrub, exit_code=1)
    return _emit(
        {
            "ok": True,
            "authenticated": False,
            "standalone_key": True,
            "wallet": {
                "balance_micros": w_data.get("balance_micros", 0),
                "inflight_reserved_micros": w_data.get("inflight_reserved_micros", 0),
                "spent_today_micros": w_data.get("spent_today_micros", 0),
                "daily_limit_micros": w_data.get("daily_limit_micros", 0),
                "per_call_limit_micros": w_data.get("per_call_limit_micros", 0),
            },
            "key": w_data,
            "wallet_url": f"{north}/wallet",
            "claim_hint": "Run `bazaar login` to bind to a unified Human Account Wallet",
        },
        secrets_to_scrub=scrub,
        exit_code=0,
    )


def _cmd_whoami(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    if not tok:
        return _emit({"ok": False, "error": "No API key configured in env or keyring"}, secrets_to_scrub=scrub, exit_code=1)
    code, is_err, data = _call_mcp_tool(north, "bazaar_whoami", {}, token=tok)
    if code == 404:
        r_code, r_data = _http_json("GET", f"{south}/whoami", token=tok)
        return _emit(r_data, secrets_to_scrub=scrub, exit_code=(0 if r_code == 200 and r_data.get("ok") else 1))
    return _emit(data, secrets_to_scrub=scrub, exit_code=(1 if is_err else 0))


def _compact_wholesale_item(it: dict[str, Any]) -> dict[str, Any]:
    raw_cat_id = str(it.get("catalog_id") or "")
    src = str(it.get("source") or ("apify" if raw_cat_id.startswith("apify:") else "monid"))
    ep_id = str(it.get("endpoint_id") or (raw_cat_id.split(":", 1)[1] if ":" in raw_cat_id else ""))
    cat_id = raw_cat_id or (f"{src}:{ep_id}" if ep_id else "")
    bridge_cid = str(
        it.get("bridge_card_id")
        or ("apify_actor_universal_bridge" if src == "apify" else "monid_wholesale_tool_bridge")
    )
    default_inputs = (
        {"action": "run", "endpoint": ep_id, "input": {}}
        if src == "monid"
        else {"action": "run", "actor_id": ep_id, "input": {}}
    )
    ex = it.get("run_payload_example")
    return {
        "catalog_id": cat_id,
        "source": src,
        "endpoint_id": ep_id,
        "title": str(it.get("title") or ep_id),
        "description": str(it.get("description") or "")[:240],
        "category": str(it.get("category") or ""),
        "price_display": str(it.get("price_display") or ""),
        "price_micros_est": int(it.get("price_micros_est") or 0),
        "bridge_card_id": bridge_cid,
        "use_card_id": cat_id,
        "bridge_inputs": ex if isinstance(ex, dict) and ex else default_inputs,
    }


def _resolve_wholesale_bridge(
    raw_cid: Any,
    raw_inputs: dict[str, Any],
) -> tuple[str | None, dict[str, Any], str | None]:
    if not isinstance(raw_cid, str):
        return None, raw_inputs, None
    s = raw_cid.strip()
    if s.startswith("monid:"):
        ep = s[6:].strip()
        if not ep or len(ep) > 256 or any(ch.isspace() or ord(ch) < 32 for ch in ep) or ".." in ep:
            raise ValueError("invalid monid endpoint")
        cid = "monid_wholesale_tool_bridge"
        if raw_inputs.get("action") and raw_inputs.get("endpoint"):
            wrapped = dict(raw_inputs)
        elif raw_inputs.get("action") in ("search", "check_run"):
            wrapped = {**raw_inputs, "endpoint": ep}
        else:
            max_usd = float(raw_inputs.get("max_upstream_usd", 0.05))
            if (
                raw_inputs.get("action") == "run"
                and isinstance(raw_inputs.get("input"), dict)
                and set(raw_inputs.keys()) <= {"action", "input", "max_upstream_usd"}
            ):
                wrapped = {"action": "run", "endpoint": ep, "input": raw_inputs["input"], "max_upstream_usd": max_usd}
            else:
                wrapped = {"action": "run", "endpoint": ep, "input": raw_inputs, "max_upstream_usd": max_usd}
        return cid, wrapped, f"monid:{ep}"

    if s.startswith("apify:"):
        actor_id = s[6:].strip()
        if not actor_id or len(actor_id) > 256 or any(ch.isspace() or ord(ch) < 32 for ch in actor_id) or ".." in actor_id:
            raise ValueError("invalid apify actor_id")
        cid = "apify_actor_universal_bridge"
        if raw_inputs.get("action") and raw_inputs.get("actor_id"):
            wrapped = dict(raw_inputs)
        elif raw_inputs.get("action") == "inspect":
            wrapped = {**raw_inputs, "action": "inspect", "actor_id": actor_id}
        else:
            max_usd = float(raw_inputs.get("max_upstream_usd", 0.05))
            if (
                raw_inputs.get("action") == "run"
                and isinstance(raw_inputs.get("input"), dict)
                and set(raw_inputs.keys()) <= {"action", "input", "max_upstream_usd"}
            ):
                wrapped = {"action": "run", "actor_id": actor_id, "input": raw_inputs["input"], "max_upstream_usd": max_usd}
            else:
                wrapped = {"action": "run", "actor_id": actor_id, "input": raw_inputs, "max_upstream_usd": max_usd}
        return cid, wrapped, f"apify:{actor_id}"

    return None, raw_inputs, None


def _fetch_wholesale_catalog(
    north: str,
    south: str,
    *,
    q: str,
    limit: int,
    source: str = "",
    token: str = "",
) -> tuple[dict[str, int], list[dict[str, Any]], dict[str, Any] | None]:
    eff_limit = max(1, min(int(limit), 50))
    qs_dict: dict[str, str] = {"q": q, "limit": str(eff_limit)}
    if source in ("monid", "apify"):
        qs_dict["source"] = source
    qs = urllib.parse.urlencode(qs_dict)
    candidates = [north] + ([south] if south != north else [])
    for base in candidates:
        w_code, w_data = _http_json("GET", f"{base}/gw/catalog?{qs}", token=token, timeout=4.0)
        if w_code == 200 and isinstance(w_data, dict) and w_data.get("ok") is not False:
            raw_totals = w_data.get("totals")
            totals: dict[str, int] = {}
            if isinstance(raw_totals, dict):
                m_cnt = int(raw_totals.get("monid_endpoints") or 0)
                a_cnt = int(raw_totals.get("apify_actors") or 0)
                c_cnt = int(raw_totals.get("combined") or (m_cnt + a_cnt))
                totals = {"monid_endpoints": m_cnt, "apify_actors": a_cnt, "combined": c_cnt}
            raw_items = w_data.get("items")
            matches: list[dict[str, Any]] = []
            if isinstance(raw_items, list):
                for it in raw_items[:eff_limit]:
                    if isinstance(it, dict):
                        matches.append(_compact_wholesale_item(it))
            return totals, matches, w_data
    return {}, [], None


def _cmd_find(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    q = (args.query_opt if args.query_opt is not None else (args.query or "")).strip()
    lim = max(1, min(int(args.limit), 100))
    raw_source = str(getattr(args, "source", "all") or "all").strip().lower()
    legacy_wholesale = str(getattr(args, "wholesale", "") or "").strip().lower()
    no_wholesale = bool(getattr(args, "no_wholesale", False)) or (raw_source == "cards")
    want_cards = raw_source in ("all", "cards")
    want_wholesale = (not no_wholesale) and raw_source in ("all", "monid", "apify")
    cat_source = raw_source if raw_source in ("monid", "apify") else (
        legacy_wholesale if legacy_wholesale in ("monid", "apify") else ""
    )

    is_err = False
    cards_list: list[dict[str, Any]] = []
    pitfalls_list: list[dict[str, Any]] = []
    total_matched = 0
    wholesale_totals: dict[str, int] = {}
    wholesale_matches: list[dict[str, Any]] = []
    mcp_grand_total = 0

    if want_cards:
        tool_args: dict[str, Any] = {
            "q": q,
            "limit": lim,
            "include_dead_ends": bool(args.include_dead_ends),
            "only_in_proven": bool(args.only_in_proven),
            "include_wholesale": bool(want_wholesale and not cat_source),
        }
        if args.gate:
            tool_args["gate"] = args.gate

        code, is_err, mcp_data = _call_mcp_tool(north, "bazaar_find", tool_args, token=tok)
        if code == 404:
            qs: dict[str, str] = {"format": "json"}
            if q:
                qs["q"] = q
            if args.gate:
                qs["gate"] = args.gate
            r_code, r_data = _http_json("GET", f"{south}/find?{urllib.parse.urlencode(qs)}", token=tok)
            if r_code != 200 or not isinstance(r_data, dict):
                return _emit(r_data if isinstance(r_data, dict) else {"ok": False}, secrets_to_scrub=scrub, exit_code=1)
            raw_cards = r_data.get("cards") if isinstance(r_data.get("cards"), list) else []
            for c in raw_cards:
                if not isinstance(c, dict):
                    continue
                dead = bool(c.get("is_dead_end")) or c.get("status_tag") == "dead_end"
                if dead:
                    pitfalls_list.append({
                        "card_id": c.get("card_id", ""),
                        "kind": "dead_end",
                        "title": c.get("title", ""),
                        "warning": c.get("mechanism") or c.get("side_effects") or c.get("title", ""),
                    })
                for rcpt in c.get("recent_receipts") or []:
                    if isinstance(rcpt, dict) and rcpt.get("pitfall_note"):
                        pitfalls_list.append({
                            "card_id": c.get("card_id", ""),
                            "kind": "receipt_pitfall",
                            "run_id": rcpt.get("run_id", ""),
                            "warning": rcpt.get("pitfall_note", ""),
                        })
                if dead and not args.include_dead_ends:
                    continue
                if args.only_in_proven and not (c.get("presence_state") == "in" and c.get("status_tag") == "proven"):
                    continue
                cards_list.append(c)
            total_matched = len(raw_cards)
            cards_list = cards_list[:lim]
            is_err = False
        elif is_err:
            return _emit(mcp_data, secrets_to_scrub=scrub, exit_code=1)
        else:
            cards_list = list(mcp_data.get("cards") or [])
            pitfalls_list = list(mcp_data.get("pitfall_warnings") or [])
            total_matched = int(mcp_data.get("total_matched") or len(cards_list))
            if isinstance(mcp_data.get("wholesale_totals"), dict) and mcp_data["wholesale_totals"]:
                wholesale_totals = dict(mcp_data["wholesale_totals"])
            if isinstance(mcp_data.get("wholesale_matches"), list):
                wholesale_matches = list(mcp_data["wholesale_matches"])
            mcp_grand_total = int(mcp_data.get("grand_total_capabilities") or 0)

    raw_w_data: dict[str, Any] | None = None
    if want_wholesale and (cat_source or not wholesale_totals):
        cat_lim = min(lim, 6) if (not q and want_cards) else min(lim, 20)
        w_totals, w_matches, raw_w_data = _fetch_wholesale_catalog(
            north,
            south,
            q=q,
            limit=cat_lim,
            source=cat_source,
            token=tok,
        )
        if w_totals:
            wholesale_totals = w_totals
        if w_matches or cat_source:
            wholesale_matches = w_matches

    combined_wholesale = int(wholesale_totals.get("combined") or 0)
    computed_grand = total_matched + combined_wholesale
    grand_total = max(mcp_grand_total, computed_grand) if not no_wholesale else total_matched

    data: dict[str, Any] = {
        "ok": True,
        "query": q,
        "gate": args.gate or "",
        "source": raw_source,
        "total_matched": total_matched,
        "count": len(cards_list),
        "cards": cards_list,
        "pitfall_warnings": pitfalls_list,
        "wholesale_matches": wholesale_matches,
        "wholesale_totals": wholesale_totals,
        "grand_total_capabilities": grand_total,
    }
    if legacy_wholesale:
        if raw_w_data is not None:
            data["wholesale_catalog"] = raw_w_data
        elif wholesale_totals:
            data["wholesale_catalog"] = {"ok": True, "totals": wholesale_totals, "items": wholesale_matches}
        else:
            data["wholesale_catalog"] = {"ok": False, "http_status": 404}

    return _emit(data, secrets_to_scrub=scrub, exit_code=(1 if is_err else 0))


def _cmd_use(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    if not tok:
        return _emit(
            {
                "ok": False,
                "error": "Buyer API key required for `use`",
                "claim_url_hint": f"{north}/console?onboarding=1",
            },
            secrets_to_scrub=scrub,
            exit_code=1,
        )

    if args.input_file:
        inputs_obj = json.loads(Path(args.input_file).expanduser().read_text(encoding="utf-8"))
    else:
        inputs_obj = json.loads(args.inputs or "{}")
    if not isinstance(inputs_obj, dict):
        return _emit({"ok": False, "error": "--inputs must decode to a JSON object"}, secrets_to_scrub=scrub, exit_code=2)

    try:
        bridge_cid, effective_inputs, bridged_catalog_id = _resolve_wholesale_bridge(args.card_id, inputs_obj)
    except ValueError as exc:
        return _emit({"ok": False, "code": "bad_request", "error": str(exc)}, secrets_to_scrub=scrub, exit_code=2)

    target_card_id = bridge_cid if bridge_cid is not None else args.card_id
    eff_timeout_sec: float | None = (
        float(args.timeout_sec)
        if args.timeout_sec is not None
        else (45.0 if bridged_catalog_id is not None else None)
    )
    http_wait_sec = max(30.0, float(eff_timeout_sec or 25.0) + 10.0)

    idem_key = (args.idem_key or f"bazaar_{secrets.token_hex(8)}").strip()
    tool_args: dict[str, Any] = {
        "card_id": bridged_catalog_id if bridged_catalog_id is not None else target_card_id,
        "inputs": effective_inputs,
        "idem_key": idem_key,
    }
    if args.max_price_micros is not None:
        tool_args["max_price_micros"] = int(args.max_price_micros)
    if eff_timeout_sec is not None:
        tool_args["timeout_sec"] = eff_timeout_sec
    if args.trace_id:
        tool_args["trace_id"] = args.trace_id

    code, is_err, data = _call_mcp_tool(
        north,
        "bazaar_use",
        tool_args,
        token=tok,
        timeout=http_wait_sec,
    )
    if code == 404 or (
        bridged_catalog_id is not None
        and is_err
        and data.get("code") == "bad_request"
        and "card_id" in str(data.get("error") or "")
    ):
        use_body: dict[str, Any] = {"input": effective_inputs, "idem_key": idem_key}
        if args.max_price_micros is not None:
            use_body["max_cost_micros"] = int(args.max_price_micros)
        if eff_timeout_sec is not None:
            use_body["timeout_sec"] = eff_timeout_sec
        if args.trace_id:
            use_body["trace_id"] = args.trace_id
        r_code, r_data = _http_json(
            "POST",
            f"{south}/use/{urllib.parse.quote(target_card_id, safe='')}",
            token=tok,
            body=use_body,
            extra_headers={"Idempotency-Key": idem_key},
            timeout=http_wait_sec,
        )
        data = r_data if isinstance(r_data, dict) else {"ok": False, "data": r_data}
        is_err = r_code != 200 or not data.get("ok")

    if bridged_catalog_id is not None and isinstance(data, dict):
        data.setdefault("bridged_catalog_id", bridged_catalog_id)
        data.setdefault("bridge_card_id", target_card_id)
    data.setdefault("idem_key", idem_key)
    err_txt = str(data.get("error") or "").lower()
    if is_err and ("balance" in err_txt or "insufficient" in err_txt or "limit" in err_txt):
        data.setdefault("wallet_topup_url", f"{north}/wallet")
    return _emit(data, secrets_to_scrub=scrub, exit_code=(1 if is_err else 0))


def _cmd_receipt(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    run_id = (args.run_id_opt or args.run_id or "").strip()
    card_id = (args.card_id or "").strip()
    memo = (args.memo or "").strip()

    if run_id:
        tool_args: dict[str, Any] = {"run_id": run_id}
        if card_id:
            tool_args["card_id"] = card_id
        if memo:
            tool_args["memo"] = memo
            tool_args["outcome"] = args.outcome
            if args.did:
                tool_args["did"] = args.did
        code, is_err, data = _call_mcp_tool(north, "bazaar_receipt", tool_args, token=tok)
        if code != 404:
            return _emit(data, secrets_to_scrub=scrub, exit_code=(1 if is_err else 0))

    if not card_id:
        return _emit(
            {"ok": False, "error": "Provide <run_id> (to reconcile/attach memo) or --card-id (for recipe receipt)"},
            secrets_to_scrub=scrub,
            exit_code=2,
        )
    if not tok:
        return _emit({"ok": False, "error": "API key required to post receipt"}, secrets_to_scrub=scrub, exit_code=1)

    rcpt_body: dict[str, Any] = {
        "card": card_id,
        "card_id": card_id,
        "ok": args.outcome == "ok",
        "outcome": args.outcome,
        "did": args.did or memo or f"recipe {args.outcome}",
        "note": memo,
        "pitfall_note": memo,
    }
    if run_id:
        rcpt_body["ref_run_id"] = run_id
    r_code, r_data = _http_json("POST", f"{south}/receipt", token=tok, body=rcpt_body)
    ok = r_code == 200 and isinstance(r_data, dict) and bool(r_data.get("ok"))
    return _emit(r_data if isinstance(r_data, dict) else {"ok": False}, secrets_to_scrub=scrub, exit_code=(0 if ok else 1))


def _cmd_publish(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    price_micros = int(getattr(args, "price_micros", 0) or 0)
    if price_micros < 0:
        return _emit({"ok": False, "error": "--price-micros must be >= 0"}, secrets_to_scrub=[], exit_code=2)
    hand_id = str(getattr(args, "hand_id", "") or "").strip()
    need_seller_seat = (price_micros > 0) or bool(hand_id)

    tok, scrub = _resolve_token(args, prefer_seller_seat=need_seller_seat)
    alt_tok, alt_scrub = _resolve_token(args, prefer_seller_seat=(not need_seller_seat))
    for s in alt_scrub:
        if s not in scrub:
            scrub.append(s)
    fallback_pub_tokens: list[str] = []
    if getattr(args, "key_file", None):
        if tok:
            fallback_pub_tokens.append(tok)
    else:
        kr_dir_pub = _keyring_dir(args)
        for cand_t in (
            tok,
            _read_key_file(kr_dir_pub / "edge_seat_key") if need_seller_seat else _read_key_file(kr_dir_pub / "buyer_key"),
            alt_tok,
            _read_key_file(kr_dir_pub / "buyer_key"),
            _read_key_file(kr_dir_pub / "edge_seat_key"),
        ):
            if cand_t and cand_t not in fallback_pub_tokens:
                fallback_pub_tokens.append(cand_t)
    if not tok:
        return _emit(
            {"ok": False, "error": "No API key found in keyring; run `bazaar login` first"},
            secrets_to_scrub=scrub,
            exit_code=1,
        )

    title = (args.title or "").strip()
    if not title:
        return _emit({"ok": False, "error": "--title is required"}, secrets_to_scrub=scrub, exit_code=2)

    if not args.dead_end and not args.allow_atomic_step and _RE_TINY_TAP_SPAM.match(title) and len(title) < 36:
        return _emit(
            {
                "ok": False,
                "code": "granularity_violation_tiny_step",
                "error": (
                    "Refusing to publish tiny per-tap UI step card. Publish outcome-level full workflows "
                    "(`bazaar mount`) or complete $0 proven pitfall playbooks (`bazaar publish`)."
                ),
            },
            secrets_to_scrub=scrub,
            exit_code=2,
        )

    mechanism_parts: list[str] = []
    if args.mechanism:
        mechanism_parts.append(args.mechanism.strip())
    if args.pitfalls:
        mechanism_parts.append(f"[Pitfalls / 实证避坑]: {args.pitfalls.strip()}")
    mechanism = "\n".join(mechanism_parts) or title

    side_effects = (args.side_effects or "").strip()
    if args.pitfalls and not side_effects:
        side_effects = f"Gotchas: {args.pitfalls.strip()}"

    is_private = bool(getattr(args, "private", False))
    is_public = 0 if is_private else 1
    used_cards: list[str] = []
    for raw_u in list(getattr(args, "used_cards", None) or []) + list(getattr(args, "used_card", None) or []):
        for part in str(raw_u).split(","):
            u = part.strip()
            if u and u not in used_cards:
                used_cards.append(u)

    tool_args: dict[str, Any] = {
        "title": title,
        "gate": (args.gate or "06 踩过坑才知道的").strip(),
        "price_micros": price_micros,
        "is_dead_end": bool(args.dead_end),
        "is_public": is_public,
        "mechanism": mechanism,
    }
    if args.card_id:
        tool_args["card_id"] = args.card_id.strip()
    if hand_id:
        tool_args["hand_id"] = hand_id
    if args.inputs_spec:
        tool_args["inputs_spec"] = args.inputs_spec
    if args.outputs_spec:
        tool_args["outputs_spec"] = args.outputs_spec
    elif not hand_id and price_micros == 0:
        tool_args["outputs_spec"] = (
            "Dead-end warning (`is_dead_end=1`); do not execute."
            if args.dead_end
            else "$0 proven local playbook; after running, record empirical outcome via `bazaar receipt`."
        )
    if args.preconditions:
        tool_args["preconditions"] = args.preconditions
    if side_effects:
        tool_args["side_effects"] = side_effects
    if used_cards:
        tool_args["used_cards"] = used_cards
    if args.trace_id:
        tool_args["trace_id"] = args.trace_id

    code, is_err, data = 0, True, {"ok": False}
    for cand_tok in (fallback_pub_tokens or [tok]):
        code, is_err, data = _call_mcp_tool(north, "bazaar_publish_card", tool_args, token=cand_tok)
        if code == 404:
            break
        err_code = str((data or {}).get("code") or "").lower()
        err_http = int((data or {}).get("http_status") or 0) if isinstance((data or {}).get("http_status"), int) else 0
        if not is_err or (code not in (401, 403) and err_http not in (401, 403) and err_code not in ("unauthorized", "forbidden")):
            break
    if code == 404:
        card_body = dict(tool_args)
        card_body["is_dead_end"] = 1 if bool(args.dead_end) else 0
        card_body["is_public"] = is_public
        r_code, r_data = 0, {"ok": False}
        for cand_tok in (fallback_pub_tokens or [tok]):
            r_code, r_data = _http_json("POST", f"{south}/card", token=cand_tok, body=card_body)
            if r_code not in (401, 403):
                break
        ok = r_code == 200 and isinstance(r_data, dict) and bool(r_data.get("ok"))
        return _emit(r_data if isinstance(r_data, dict) else {"ok": False}, secrets_to_scrub=scrub, exit_code=(0 if ok else 1))

    return _emit(data, secrets_to_scrub=scrub, exit_code=(1 if is_err else 0))


def _cmd_toggle(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    tok, scrub = _resolve_token(args)
    seat_tok, seat_scrub = _resolve_token(args, prefer_seller_seat=True)
    for s in seat_scrub:
        if s not in scrub:
            scrub.append(s)

    card_ids: list[str] = []
    for cid in list(args.card_ids or []) + list(args.card_id_opt or []):
        cleaned = str(cid or "").strip()
        if cleaned and cleaned not in card_ids:
            card_ids.append(cleaned)
    hand_id_only = (args.hand_id or "").strip()

    if not card_ids and not hand_id_only:
        return _emit(
            {"ok": False, "error": "Provide at least one <card_id> or --hand-id"},
            secrets_to_scrub=scrub,
            exit_code=2,
        )

    is_public_val: int | None = None
    if args.private:
        is_public_val = 0
    elif args.public:
        is_public_val = 1

    presence_val: str | None = None
    if args.presence_in:
        presence_val = "in"
    elif args.presence_away:
        presence_val = "away"

    dead_val: int | None = None
    if args.dead_end:
        dead_val = 1
    elif args.active:
        dead_val = 0

    if (
        is_public_val is None
        and presence_val is None
        and args.price_micros is None
        and args.title is None
        and dead_val is None
    ):
        return _emit(
            {
                "ok": False,
                "error": "Specify at least one toggle flag (--private, --public, --in, --away, --price-micros, --title, --dead-end, --active)",
            },
            secrets_to_scrub=scrub,
            exit_code=2,
        )

    if not tok and not seat_tok:
        return _emit(
            {"ok": False, "error": "No API key configured; run `bazaar login` first"},
            secrets_to_scrub=scrub,
            exit_code=1,
        )

    if card_ids and tok:
        toggle_req: dict[str, Any] = {"card_ids": card_ids}
        if len(card_ids) == 1:
            toggle_req["card_id"] = card_ids[0]
        if is_public_val is not None:
            toggle_req["is_public"] = is_public_val
        if presence_val is not None:
            toggle_req["presence_state"] = presence_val
        if args.note is not None:
            toggle_req["presence_note"] = args.note
        if args.price_micros is not None:
            toggle_req["price_micros"] = int(args.price_micros)
        if args.title is not None:
            toggle_req["title"] = args.title
        if dead_val is not None:
            toggle_req["is_dead_end"] = dead_val
        if args.clone_if_locked:
            toggle_req["clone_if_locked"] = True

        t_code, t_data = _http_json("POST", f"{north}/api/account/capabilities/toggle", token=tok, body=toggle_req)
        if t_code == 200 and isinstance(t_data, dict) and t_data.get("ok"):
            return _emit(t_data, secrets_to_scrub=scrub, exit_code=0)
        if t_code not in (401, 403, 404):
            return _emit(
                t_data if isinstance(t_data, dict) else {"ok": False, "http_status": t_code},
                secrets_to_scrub=scrub,
                exit_code=1,
            )

    candidate_tokens = [t for t in (seat_tok, tok) if t]
    dedup_tokens: list[str] = []
    for t in candidate_tokens:
        if t not in dedup_tokens:
            dedup_tokens.append(t)

    results: list[dict[str, Any]] = []
    all_ok = True

    if hand_id_only and not card_ids:
        if presence_val is None:
            return _emit(
                {"ok": False, "error": "--hand-id standalone toggle requires --in or --away"},
                secrets_to_scrub=scrub,
                exit_code=2,
            )
        p_body: dict[str, Any] = {"hand_id": hand_id_only, "presence_state": presence_val}
        if args.note is not None:
            p_body["presence_note"] = args.note
        last_resp: Any = {"ok": False}
        matched = False
        for cand_tok in dedup_tokens:
            p_code, p_resp = _http_json("POST", f"{south}/presence", token=cand_tok, body=p_body)
            last_resp = p_resp
            if p_code == 200 and isinstance(p_resp, dict) and p_resp.get("ok"):
                results.append({"hand_id": hand_id_only, "presence": p_resp, "ok": True})
                matched = True
                break
        if not matched:
            all_ok = False
            results.append({"hand_id": hand_id_only, "ok": False, "error": last_resp})
        return _emit(
            {"ok": all_ok, "updated_count": sum(1 for r in results if r.get("ok")), "results": results},
            secrets_to_scrub=scrub,
            exit_code=(0 if all_ok else 1),
        )

    for cid in card_ids:
        card_obj: dict[str, Any] | None = None
        chosen_tok = dedup_tokens[0]
        for cand_tok in dedup_tokens:
            g_code = 404
            g_data: Any = {}
            for base_url in ([south] if south == north else [south, north]):
                g_code, g_data = _http_json("GET", f"{base_url}/card/{urllib.parse.quote(cid, safe='')}", token=cand_tok)
                if g_code == 200 and isinstance(g_data, dict) and isinstance(g_data.get("card"), dict):
                    break
            if g_code == 200 and isinstance(g_data, dict) and isinstance(g_data.get("card"), dict):
                w_code, w_data = _http_json("GET", f"{south}/whoami", token=cand_tok)
                card_candidate = g_data["card"]
                if w_code == 200 and isinstance(w_data, dict) and (
                    w_data.get("owner_id") == card_candidate.get("owner_id") or w_data.get("kind") == "founder"
                ):
                    card_obj = card_candidate
                    chosen_tok = cand_tok
                    break
                if card_obj is None:
                    card_obj = card_candidate
                    chosen_tok = cand_tok

        if card_obj is None:
            all_ok = False
            results.append({"card_id": cid, "ok": False, "error": "card not found or unauthorized"})
            continue

        item_res: dict[str, Any] = {"card_id": cid, "ok": True}
        need_card_post = (
            is_public_val is not None
            or args.price_micros is not None
            or args.title is not None
            or dead_val is not None
        )
        if need_card_post:
            upd_body: dict[str, Any] = {
                "card_id": cid,
                "title": args.title if args.title is not None else card_obj.get("title", cid),
                "is_public": is_public_val if is_public_val is not None else int(card_obj.get("is_public", 1)),
                "price_micros": int(args.price_micros) if args.price_micros is not None else int(card_obj.get("price_micros", 0)),
                "is_dead_end": dead_val if dead_val is not None else int(card_obj.get("is_dead_end", 0)),
            }
            if card_obj.get("hand_id"):
                upd_body["hand_id"] = card_obj["hand_id"]
            c_code, c_resp = _http_json("POST", f"{south}/card", token=chosen_tok, body=upd_body)
            if c_code != 200 or not isinstance(c_resp, dict) or not c_resp.get("ok"):
                if (
                    args.clone_if_locked
                    and args.price_micros is not None
                    and "locked" in str((c_resp or {}).get("error", "")).lower()
                ):
                    clone_id = f"{cid}_v2"
                    clone_body = {
                        "card_id": clone_id,
                        "title": upd_body["title"],
                        "gate": card_obj.get("gate", ""),
                        "inputs_spec": card_obj.get("inputs_spec", ""),
                        "outputs_spec": card_obj.get("outputs_spec", ""),
                        "preconditions": card_obj.get("preconditions", ""),
                        "mechanism": card_obj.get("mechanism", ""),
                        "side_effects": card_obj.get("side_effects", ""),
                        "price_micros": int(args.price_micros),
                        "is_public": upd_body["is_public"],
                        "is_dead_end": upd_body["is_dead_end"],
                    }
                    if card_obj.get("hand_id"):
                        clone_body["hand_id"] = card_obj["hand_id"]
                    cl_code, cl_resp = _http_json("POST", f"{south}/card", token=chosen_tok, body=clone_body)
                    if cl_code == 200 and isinstance(cl_resp, dict) and cl_resp.get("ok"):
                        _http_json(
                            "POST",
                            f"{south}/card",
                            token=chosen_tok,
                            body={"card_id": cid, "title": card_obj.get("title", cid), "is_public": 0},
                        )
                        item_res["cloned_card_id"] = clone_id
                        item_res["card"] = cl_resp.get("card")
                    else:
                        all_ok = False
                        item_res["ok"] = False
                        item_res["error"] = cl_resp
                else:
                    all_ok = False
                    item_res["ok"] = False
                    item_res["error"] = c_resp
            else:
                item_res["is_public"] = upd_body["is_public"]

        if presence_val is not None and item_res["ok"]:
            target_hand = hand_id_only or str(card_obj.get("hand_id") or "").strip()
            if not target_hand:
                all_ok = False
                item_res["ok"] = False
                item_res["error"] = "Card has no bound hand_id for --in/--away presence toggle"
            else:
                p_req: dict[str, Any] = {"hand_id": target_hand, "presence_state": presence_val}
                if args.note is not None:
                    p_req["presence_note"] = args.note
                p_code, p_resp = _http_json("POST", f"{south}/presence", token=chosen_tok, body=p_req)
                if p_code == 200 and isinstance(p_resp, dict) and p_resp.get("ok"):
                    item_res["presence_state"] = presence_val
                    item_res["hand_id"] = target_hand
                else:
                    all_ok = False
                    item_res["ok"] = False
                    item_res["error"] = p_resp

        results.append(item_res)

    return _emit(
        {"ok": all_ok, "updated_count": sum(1 for r in results if r.get("ok")), "results": results},
        secrets_to_scrub=scrub,
        exit_code=(0 if all_ok else 1),
    )


def _cmd_mount(args: argparse.Namespace) -> int:
    north, south = _store_urls(args)
    kr_dir = _keyring_dir(args)
    seat_file = kr_dir / "edge_seat_key"
    buyer_file = kr_dir / "buyer_key"
    edge_script = Path(args.edge_script or _DEFAULT_BRIDGE_CANDIDATE).expanduser()
    if not edge_script.is_file():
        return _emit({"ok": False, "error": f"bazaar_edge_hand.py not found at {edge_script}"}, secrets_to_scrub=[], exit_code=2)

    title = (args.title or "").strip()
    if title and _RE_TINY_TAP_SPAM.match(title) and len(title) < 36:
        return _emit(
            {
                "ok": False,
                "code": "granularity_violation_tiny_step",
                "error": "Mount an Outcome-Level Full-Workflow Hand (1 call = complete workflow), never tiny per-tap steps.",
            },
            secrets_to_scrub=[],
            exit_code=2,
        )

    cmd_argv: list[str] = [sys.executable, str(edge_script), "--store-url", south]
    child_env = dict(os.environ)
    price = int(args.price_micros) if args.price_micros is not None else 0
    explicit_key_file = getattr(args, "key_file", "")

    if explicit_key_file:
        child_env.pop("BAZAAR_API_KEY", None)
        child_env.pop("CAUSAL_STORE_API_KEY", None)
        cmd_argv.extend(["--key-file", str(Path(explicit_key_file).expanduser())])
    elif seat_file.is_file():
        child_env.pop("BAZAAR_API_KEY", None)
        child_env.pop("CAUSAL_STORE_API_KEY", None)
        cmd_argv.extend(["--seat-key-file", str(seat_file)])
        if args.auto_onboard:
            cmd_argv.extend(["--auto-onboard", "--onboarding-url", f"{north}/bridge/register"])
    elif args.auto_onboard or (price == 0 and not buyer_file.is_file()):
        child_env.pop("BAZAAR_API_KEY", None)
        child_env.pop("CAUSAL_STORE_API_KEY", None)
        cmd_argv.extend(
            [
                "--seat-key-file",
                str(seat_file),
                "--auto-onboard",
                "--onboarding-url",
                f"{north}/bridge/register",
            ]
        )
    elif buyer_file.is_file() and not _normalize_token(child_env.get("BAZAAR_API_KEY", "")):
        child_env.pop("BAZAAR_API_KEY", None)
        cmd_argv.extend(["--key-file", str(buyer_file)])

    if args.owner_label:
        cmd_argv.extend(["--owner-label", args.owner_label])
    if args.card_id:
        cmd_argv.extend(["--card-id", args.card_id])
    if args.hand_id:
        cmd_argv.extend(["--hand-id", args.hand_id])
    if title:
        cmd_argv.extend(["--title", title])
    if args.price_micros is not None:
        cmd_argv.extend(["--price-micros", str(price)])
    if getattr(args, "private", False):
        cmd_argv.append("--private")
    if args.gate:
        cmd_argv.extend(["--gate", args.gate])
    if args.inputs_spec:
        cmd_argv.extend(["--inputs-spec", args.inputs_spec])
    if args.outputs_spec:
        cmd_argv.extend(["--outputs-spec", args.outputs_spec])
    if args.mechanism:
        cmd_argv.extend(["--mechanism", args.mechanism])
    if args.preconditions:
        cmd_argv.extend(["--preconditions", args.preconditions])
    if args.side_effects:
        cmd_argv.extend(["--side-effects", args.side_effects])

    if args.http_upstream:
        cmd_argv.extend(["--http-upstream", args.http_upstream])
    if args.http_method:
        cmd_argv.extend(["--http-method", args.http_method])
    for hdr in args.http_header or []:
        cmd_argv.extend(["--http-header", hdr])

    if args.cmd:
        cmd_argv.extend(["--cmd", args.cmd])
    if args.python_target:
        cmd_argv.extend(["--python-target", args.python_target])
    if args.mcp_stdio:
        cmd_argv.extend(["--mcp-stdio", args.mcp_stdio])
    if args.mcp_tool:
        cmd_argv.extend(["--mcp-tool", args.mcp_tool])
    if args.exec_timeout is not None:
        cmd_argv.extend(["--exec-timeout", str(args.exec_timeout)])
    if args.poll_timeout is not None:
        cmd_argv.extend(["--poll-timeout", str(args.poll_timeout)])
    if args.once:
        cmd_argv.append("--once")

    extra = list(args.extra_args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd_argv.extend(extra)

    if args.print_cmd:
        return _emit({"ok": True, "argv": cmd_argv}, secrets_to_scrub=[], exit_code=0)

    proc = subprocess.run(cmd_argv, env=child_env, check=False)
    return int(proc.returncode)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store-url", default=argparse.SUPPRESS, help="Causal Bazaar base URL")
    common.add_argument("--southbound-store-url", default=argparse.SUPPRESS, help="Southbound Store API URL override")
    common.add_argument("--keyring-dir", default=argparse.SUPPRESS, help="0600 keyring directory (~/.causal-store-v2)")
    common.add_argument("--key-file", default=argparse.SUPPRESS, help="Explicit 0600 key file override")

    root = argparse.ArgumentParser(
        prog="bazaar",
        description="Causal Bazaar zero-dependency CLI for Personal Agents & Humans",
        parents=[common],
    )
    root.add_argument("--version", action="version", version=f"causal-bazaar-cli/{CLI_VERSION}")
    sub = root.add_subparsers(dest="subcommand", required=True)

    p_login = sub.add_parser("login", aliases=["claim"], parents=[common], help="1-click device claim to bind human wallet (/claim/XXXX-XXXX)")
    p_login.add_argument("--agent-name", default="personal-agent", help="Agent name shown on /claim approval screen")
    p_login.add_argument("--hostname", default="", help="Optional device hostname override")
    p_login.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between claim poll checks (default: 2.0)")
    p_login.add_argument("--timeout", type=float, default=600.0, help="Max wait seconds for browser approval (default: 600)")
    p_login.add_argument("--no-wait", action="store_true", help="Print claim_url and exit immediately (poll later via --resume)")
    p_login.add_argument("--resume", action="store_true", help="Resume polling a pending claim initiated with --no-wait")
    p_login.add_argument("--open", action="store_true", help="Open claim URL in default system browser")

    sub.add_parser("wallet", parents=[common], help="Inspect unified account wallet balance, caps & capability counts")
    sub.add_parser("whoami", parents=[common], help="Inspect active key identity & limits")

    p_find = sub.add_parser("find", parents=[common], help="Search all 120,000+ capabilities (Tier-1 cards + Monid + Apify)")
    p_find.add_argument("query", nargs="?", default="", help="Goal or domain keywords")
    p_find.add_argument("-q", "--query", dest="query_opt", default=None, help="Keywords (flag form)")
    p_find.add_argument("--gate", default="", help="Optional gate filter")
    p_find.add_argument("--limit", type=int, default=12, help="Max cards (default: 12)")
    p_find.add_argument("--include-dead-ends", action="store_true", help="Include dead_end cards in cards list")
    p_find.add_argument("--only-in-proven", action="store_true", help="Only live in + proven hands")
    p_find.add_argument("--no-wholesale", action="store_true", help="Only return Tier-1 causal cards (skip /gw/catalog)")
    p_find.add_argument(
        "--source",
        choices=["all", "cards", "monid", "apify"],
        default="all",
        help="Filter capability source (default: all)",
    )
    p_find.add_argument("--wholesale", choices=["monid", "apify", "all"], default="", help="Legacy wholesale catalog filter")

    p_use = sub.add_parser("use", parents=[common], help="Invoke capability (or monid:<ep> / apify:<actor>) with idem_key & max-price-micros ceiling")
    p_use.add_argument("card_id", help="Card ID or monid:<endpoint> / apify:<actor_id>")
    p_use.add_argument("--inputs", "--input", dest="inputs", default="{}", help="JSON object payload")
    p_use.add_argument("--input-file", default="", help="Read JSON input object from file")
    p_use.add_argument("--idem-key", default="", help="Idempotency key (auto-generated if omitted)")
    p_use.add_argument("--max-price-micros", type=int, default=None, help="Per-call price ceiling in micro-USD")
    p_use.add_argument("--timeout-sec", type=float, default=None, help="Call timeout in seconds")
    p_use.add_argument("--trace-id", default="", help="Optional trace ID")

    p_rcpt = sub.add_parser("receipt", parents=[common], help="Reconcile run receipt or attach signed pitfall memo")
    p_rcpt.add_argument("run_id", nargs="?", default="", help="Run ID to reconcile or annotate")
    p_rcpt.add_argument("--run-id", dest="run_id_opt", default="", help="Run ID (flag form)")
    p_rcpt.add_argument("--card-id", default="", help="Card ID (for recipe receipt or hint)")
    p_rcpt.add_argument("--memo", "--pitfall-note", "--note", dest="memo", default="", help="Empirical pitfall or experience memo")
    p_rcpt.add_argument("--outcome", choices=["ok", "fail", "unknown"], default="ok")
    p_rcpt.add_argument("--did", default="", help="Short factual summary")

    p_pub = sub.add_parser(
        "publish",
        aliases=["publish-recipe", "publish-playbook", "publish-card"],
        parents=[common],
        help="Publish a $0 proven playbook, pitfall warning, or hand card (supports --private / --public)",
    )
    p_pub.add_argument("--title", required=True, help="Outcome-level playbook or capability title")
    p_pub.add_argument("--card-id", default="", help="Optional stable card_id slug")
    p_pub.add_argument("--gate", default="06 踩过坑才知道的", help="Gate category")
    p_pub.add_argument("--price-micros", type=int, default=0, help="Price in micro-USD (default: 0; >0 uses edge_seat_key)")
    p_pub.add_argument("--hand-id", default="", help="Bound hand_id (automatically selects edge_seat_key)")
    p_pub.add_argument("--mechanism", default="", help="End-to-end proven workflow steps")
    p_pub.add_argument("--pitfalls", "--gotchas", dest="pitfalls", default="", help="Exact gotchas & failure boundaries")
    p_pub.add_argument("--inputs-spec", default="", help="Inputs description")
    p_pub.add_argument("--outputs-spec", default="", help="Commitment boundary & unknown verification spec")
    p_pub.add_argument("--preconditions", default="", help="Required accounts/devices/env")
    p_pub.add_argument("--side-effects", default="", help="External side effects")
    p_pub.add_argument("--dead-end", action="store_true", help="Mark as verified dead_end=1 so others avoid it")
    pub_vis = p_pub.add_mutually_exclusive_group()
    pub_vis.add_argument("--private", action="store_true", help="Save as 🔒 Private capability (is_public=0)")
    pub_vis.add_argument("--public", action="store_true", help="Publish as 🌐 Public capability (is_public=1)")
    p_pub.add_argument(
        "--used-cards",
        nargs="+",
        action="extend",
        default=[],
        help="Upstream card_id(s) composed in a real run (space- or comma-separated)",
    )
    p_pub.add_argument("--used-card", action="append", default=[], help="Upstream card_id composed in a real run")
    p_pub.add_argument("--trace-id", default="", help="Optional trace ID")
    p_pub.add_argument("--allow-atomic-step", action="store_true", help=argparse.SUPPRESS)

    p_toggle = sub.add_parser(
        "toggle",
        parents=[common],
        help="Toggle capability visibility (--private / --public) and hand presence (--in / --away)",
    )
    p_toggle.add_argument("card_ids", nargs="*", help="One or more card IDs to update")
    p_toggle.add_argument("--card-id", dest="card_id_opt", action="append", default=[], help="Card ID (flag form)")
    p_toggle.add_argument("--hand-id", default="", help="Optional hand_id for presence toggle")
    vis_grp = p_toggle.add_mutually_exclusive_group()
    vis_grp.add_argument("--private", action="store_true", help="Set 🔒 Private (is_public=0)")
    vis_grp.add_argument("--public", action="store_true", help="Set 🌐 Public (is_public=1)")
    pres_grp = p_toggle.add_mutually_exclusive_group()
    pres_grp.add_argument("--in", "--online", dest="presence_in", action="store_true", help="Set hand presence_state='in'")
    pres_grp.add_argument("--away", "--pause", dest="presence_away", action="store_true", help="Set hand presence_state='away'")
    p_toggle.add_argument("--note", default=None, help="Optional presence_note")
    p_toggle.add_argument("--price-micros", type=int, default=None, help="Optional updated price in micro-USD")
    p_toggle.add_argument("--title", default=None, help="Optional updated card title")
    dead_grp = p_toggle.add_mutually_exclusive_group()
    dead_grp.add_argument("--dead-end", action="store_true", help="Mark card is_dead_end=1")
    dead_grp.add_argument("--active", action="store_true", help="Clear dead-end flag (is_dead_end=0)")
    p_toggle.add_argument("--clone-if-locked", action="store_true", help="Auto-clone v2 card if raising price on a proven card")

    p_mount = sub.add_parser(
        "mount",
        aliases=["mount-hand", "mount-pinky-or-http-hand"],
        parents=[common],
        help="Mount a local HTTP / CLI / Python / MCP workflow in 10s via bazaar_edge_hand.py",
    )
    p_mount.add_argument("--edge-script", default="", help="Override path to bazaar_edge_hand.py")
    p_mount.add_argument("--auto-onboard", action="store_true", help="Auto-mint Seller seat key if missing")
    p_mount.add_argument("--owner-label", default="personal_edge_seller", help="Seller seat owner label")
    p_mount.add_argument("--card-id", default="", help="Card ID to register/update")
    p_mount.add_argument("--hand-id", default="", help="Hand ID to register")
    p_mount.add_argument("--title", default="", help="Outcome-level capability quote title")
    p_mount.add_argument("--price-micros", type=int, default=None, help="Price in micro-USD (default: 0)")
    p_mount.add_argument("--private", action="store_true", help="Mount as 🔒 Private capability (is_public=0)")
    p_mount.add_argument("--gate", default="03 设备", help="Gate category")
    p_mount.add_argument("--inputs-spec", default="", help="Inputs contract")
    p_mount.add_argument("--outputs-spec", default="", help="Outputs & unknown reconciliation spec")
    p_mount.add_argument("--mechanism", default="", help="How the workflow runs end-to-end")
    p_mount.add_argument("--preconditions", default="", help="Preconditions")
    p_mount.add_argument("--side-effects", default="", help="Real-world side effects")
    p_mount.add_argument("--http-upstream", default="", help="Adapter A: local/deployed HTTP URL")
    p_mount.add_argument("--http-method", choices=["POST", "GET", "post", "get"], default=None)
    p_mount.add_argument("--http-header", action="append", default=[])
    p_mount.add_argument("--cmd", default="", help="Adapter B: shell/CLI workflow command")
    p_mount.add_argument("--python-target", default="", help="Adapter C: file_or_module:callable")
    p_mount.add_argument("--mcp-stdio", default="", help="Adapter D: local MCP stdio command")
    p_mount.add_argument("--mcp-tool", default="", help="Adapter D: MCP tool name")
    p_mount.add_argument("--exec-timeout", type=float, default=None)
    p_mount.add_argument("--poll-timeout", type=float, default=None)
    p_mount.add_argument("--once", action="store_true", help="Fulfill one batch of events and exit")
    p_mount.add_argument("--print-cmd", action="store_true", help="Print delegated bazaar_edge_hand.py argv as JSON")
    p_mount.add_argument("extra_args", nargs=argparse.REMAINDER, help="Additional flags forwarded to bazaar_edge_hand.py")

    return root


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.subcommand
    try:
        if cmd in ("login", "claim"):
            return _cmd_login(args)
        if cmd == "wallet":
            return _cmd_wallet(args)
        if cmd == "whoami":
            return _cmd_whoami(args)
        if cmd == "find":
            return _cmd_find(args)
        if cmd == "use":
            return _cmd_use(args)
        if cmd == "receipt":
            return _cmd_receipt(args)
        if cmd in ("publish", "publish-recipe", "publish-playbook", "publish-card"):
            return _cmd_publish(args)
        if cmd == "toggle":
            return _cmd_toggle(args)
        if cmd in ("mount", "mount-hand", "mount-pinky-or-http-hand"):
            return _cmd_mount(args)
        raise RuntimeError(f"Unhandled subcommand: {cmd}")
    except Exception as exc:
        return _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, secrets_to_scrub=[], exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())
