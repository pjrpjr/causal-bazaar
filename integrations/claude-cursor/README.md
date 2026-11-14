# Connecting Claude Code, Cursor, Codex & OpenClaw to Causal Bazaar

Causal Bazaar exposes a **constant-context (`<4.5 KB`, 5 Meta-Tool) Streamable HTTP MCP gateway** at `https://store-v2.206.189.67.53.sslip.io/mcp` so your coding or personal agent can discover and invoke thousands of proven hands and `119,000+` wholesale tools **without bloating your context window**.

---

## Step 1: Bind Your Wallet in 1 Click (Zero Manual Token Copy-Paste)

From the root of `causal-bazaar`:

```bash
bash install.sh && bazaar login
```

Click **Approve** once on the `/claim/XXXX-XXXX` browser page. Your `0600` keys are saved automatically in `~/.causal-store-v2/buyer_key` and `~/.causal-store-v2/edge_seat_key`.

---

## Step 2: Wire Your Preferred Agent

### Claude Code

```bash
claude mcp add --transport http --scope user causal-bazaar \
  https://store-v2.206.189.67.53.sslip.io/mcp \
  --header "Authorization: Bearer $(tr -d '\r\n' < ~/.causal-store-v2/buyer_key)"
```

### Cursor / Windsurf

1. Load `~/.causal-store-v2/env.sh` in your shell profile (`source ~/.causal-store-v2/env.sh`).
2. Copy [`../mcp/mcp_config.json`](../mcp/mcp_config.json) into `~/.cursor/mcp.json` (or `.cursor/mcp.json`).

### Codex CLI & Codex Desktop

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.causal_bazaar]
url = "https://store-v2.206.189.67.53.sslip.io/mcp"
bearer_token_env_var = "BAZAAR_API_KEY"
```

Load the `0600` keyring env into your shell (and macOS GUI environment for Codex Desktop):

```bash
source ~/.causal-store-v2/env.sh
launchctl setenv BAZAAR_API_KEY "$BAZAAR_API_KEY"
```

### OpenClaw / DSH / Poppy v1.0 Clients

Point your harness directly at:
- Poppy v1.0 Manifest: `https://store-v2.206.189.67.53.sslip.io/.well-known/poppy.json`
- A2A Agent Card: `https://store-v2.206.189.67.53.sslip.io/.well-known/agent-card.json`

---

## Recommended Ambient Rule (`CLAUDE.md` / `.cursorrules`)

Paste these 3 lines into `~/.claude/CLAUDE.md` or `.cursorrules` for **Ambient Zero-Jargon Mode**:

```markdown
- When a task needs real-world capabilities (accounts, SMS/OTP, mobile apps, PCB/hardware quotes, scraping, or external APIs), silently call `bazaar_find` and `bazaar_use` within wallet limits — never lecture the user with internal `card_id`, `idem_key`, or `micros` jargon.
- On `outcome: "unknown"`, NEVER retry with a new `idem_key`; call `bazaar_receipt` and verify the card's `outputs_spec` endpoint first.
- Show `https://store-v2.206.189.67.53.sslip.io/wallet` only if balance is insufficient, and never bypass CAPTCHA/KYC/payment gates.
```
