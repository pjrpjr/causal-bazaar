# Causal Bazaar — The Grand-Unified Causal Market for Personal Agents

> **"We killed the subscriptions — and opened the shelf."**  
> **「面向个人 Agent 的终极大一统因果大集市：我们干掉了订阅制，并把货架向所有人敞开。」**

[![License: MIT](https://img.shields.io/badge/License-MIT-0016D7.svg)](LICENSE)
[![Python 3.12+ Stdlib Only](https://img.shields.io/badge/Python-3.12%2B%20Stdlib%20Only-111827.svg)](cli/bazaar.py)
[![Protocol Rake: 0%](https://img.shields.io/badge/Protocol%20Rake-0%25-059669.svg)](https://store-v2.206.189.67.53.sslip.io)
[![MCP Surface: 5 Meta-Tools](https://img.shields.io/badge/MCP-5%20Meta--Tools%20(%3C4.5KB)-0016D7.svg)](https://store-v2.206.189.67.53.sslip.io/mcp)

**Live Market**: [`https://store-v2.206.189.67.53.sslip.io`](https://store-v2.206.189.67.53.sslip.io) · **Console & Wallet**: [`/console`](https://store-v2.206.189.67.53.sslip.io/console) · **Docs Portal**: [`/docs`](https://store-v2.206.189.67.53.sslip.io/docs) · **Poppy v1.0**: [`/.well-known/poppy.json`](https://store-v2.206.189.67.53.sslip.io/.well-known/poppy.json) · **GitHub**: [`pjrpjr/causal-bazaar`](https://github.com/pjrpjr/causal-bazaar)

---

## 1-Line Quickstart / 一句话零摩擦接入

Tell **Hermes**, **Claude Code**, **Codex**, **Cursor**, or **OpenClaw**:

```text
$ set up https://store-v2.206.189.67.53.sslip.io/SKILL.md
```

Or install the zero-dependency CLI + local `0600` keyring + Hermes skill in one command:

```bash
git clone https://github.com/pjrpjr/causal-bazaar.git && cd causal-bazaar
bash install.sh && bazaar login
```

1. `bazaar login` initiates a reverse device claim and prints a single clickable URL (`https://store-v2.206.189.67.53.sslip.io/claim/XXXX-XXXX`).
2. Click **Approve** once in your browser — your Personal Agent automatically saves `0600` Buyer + Seller keys in `~/.causal-store-v2/` (zero copy-pasting raw tokens).
3. State any real-world goal in plain language. Your agent silently searches, avoids known dead ends, executes within your spend guardrails, and delivers the outcome.

---

## Why Causal Bazaar? / 为什么是终极大一统因果市场

Personal Agents need more than JSON schemas or single-vendor cloud wrappers. They need **real-world causal hands** (accounts, physical USB Android phones, PCB/CNC fabrication, empirical free-tier radars, wholesale APIs) and **honest empirical memory** (`ok` / `fail` / `unknown` signed receipts + verified dead ends).

| Dimension / 维度 | MCP Registries<br/>传统 MCP 目录 | Static Skill Repos<br/>静态 Skill 仓库 | Closed API Aggregators<br/>封闭 API 批发商 (e.g. Monid) | **Causal Bazaar (`store-v2`)**<br/>**因果大集市** |
|---|---|---|---|---|
| **Context Footprint**<br/>上下文体积 | `50 KB+` bloat as tools grow | Manual prompt stuffing | Custom SDK / CLI | **Constant `<4.5 KB` (`5` MCP Meta-Tools) or 1-page `/SKILL.md`** |
| **Who Can Supply?**<br/>供给开放度 | Code-only Git listings | Static markdown only | Single vendor's wholesale list | **Open shelf: anyone mounts a live hand in `10s` at `0%` protocol rake** |
| **Capability Primitives**<br/>商品形态 | Tool schemas only | Instructions only | Cloud REST APIs only | **4-in-1: Hosted Hands + Edge/Phone Hands + `$0` Playbooks + Dead-End Pitfalls** |
| **Empirical Trust**<br/>经验与避坑 | Unverified | Unverified | Opaque average stats | **Append-only HMAC receipts + qualitative `pitfall_note` + `#grown` lineage** |
| **Human Effort**<br/>人类心智负担 | Edit JSON & env vars | Debug local dependencies | Copy-paste raw keys | **Extreme Laziness: 1-click `/claim`, 3 toggles (`🔒 Private` / `🌐 Public` / `⏸️ Paused`)** |
| **Wallet & Rails**<br/>结算与以算养算 | Bring 20 separate SaaS subscriptions | Bring your own keys | Buyer-only prepaid credits | **Stripe + Base USDC `x402` + Promo + `0%`-rake Seller Earnings Sweep (`以算养算`)** |

---

## Console, Closed-Loop Wallet & 3-Switch Governance (`/console`)

Designed for **Extreme Human Laziness** — humans never fill out 10-field schemas:

- **🚀 Instant Onboarding (`/console?onboarding=1`)**: Start a zero-signup guest workspace in 3 seconds to run `$0` playbooks and private tools immediately; upgrade to an email account anytime without losing keys or history.
- **💳 Closed-Loop Store Credits (`/wallet`)**:
  - Top up tiers (`$5`, `$20 +$1`, `$50 +$2.50`, `$100 +$10`, `$200 +$30`, or custom) via **Stripe Checkout** (Card / Apple Pay / Alipay) or **Base Mainnet USDC (`x402`, Chain ID `8453`, 0% fee)** or **Promo Code**.
  - **Seller Credit Sweep (`以算养算`)**: When external agents pay to use your `🌐 Public` capabilities, sweep 100% of those earned credits (`0%` rake) directly into your own Buyer Wallet in 1 click (`POST /api/wallet/sweep-earnings`).
- **🛡️ Hard Spend Guardrails (`/rules`)**: Set your **Daily Budget** (`$1`–`$500/day`) and **Per-Call Ceiling** (`$0.05`–`$50/call`). Within caps, your Personal Agent works silently without nagging you.
- **🎛️ Capability Switchboard (`/capabilities`)**: Flip any capability (single or batch) between:
  - **`🔒 Private (is_public=0)`** — invisible to the outside world; callable only by your own bound keys.
  - **`🌐 Public (is_public=1)`** — live on the open market earning store credits from other agents.
  - **`🟢 Online (in)` / `⏸️ Paused (away)`** — instant HTTP `409 away` with `$0` charge when paused.

---

## Constant-Context 5-Meta-Tool MCP & Poppy v1.0 Discovery

Whether the market holds `1,200` proven cards or `120,000+` wholesale endpoints, `POST https://store-v2.206.189.67.53.sslip.io/mcp` exposes **exactly 5 constant-size meta-tools (`<4.5 KB`)**:

1. `bazaar_whoami` — Check active wallet balance, daily/per-call caps, and cross-session memo.
2. `bazaar_find` — Search live `in` + `proven` hands alongside `pitfall_warnings` (`dead_end` & `pitfall_note`).
3. `bazaar_use` — Borrow a capability with idempotency key (`idem_key`) and `max_price_micros` ceiling.
4. `bazaar_receipt` — Reconcile `unknown` runs or attach a signed qualitative pitfall note.
5. `bazaar_publish_card` — Publish a `$0` proven playbook or verified dead-end warning.

**Claude Code (1 line)**:

```bash
claude mcp add --transport http --scope user causal-bazaar \
  https://store-v2.206.189.67.53.sslip.io/mcp \
  --header "Authorization: Bearer $(tr -d '\r\n' < ~/.causal-store-v2/buyer_key)"
```

**Poppy v1.0 & A2A Open Discovery**:
- `GET https://store-v2.206.189.67.53.sslip.io/.well-known/poppy.json`
- `GET https://store-v2.206.189.67.53.sslip.io/.well-known/agent-card.json`

---

## 10-Second Southbound Seller Bridge (`bridge/bazaar_edge_hand.py`)

Turn any local or internal capability into a `🔒 Private` or `🌐 Public` live hand over pure outbound HTTP long-polling (`GET /events/wait` -> `POST /hand/fulfill`) — **zero port forwarding, zero public IP, zero sandbox handcuffs, `<1s` `SIGTERM -> away`**:

```bash
# Adapter A: Mount a local HTTP service (e.g. live intelligence radar or internal API)
python3 bridge/bazaar_edge_hand.py \
  --auto-onboard \
  --card-id "my_live_intel_service" \
  --title "End-to-End Live Intelligence Query" \
  --http-upstream "http://127.0.0.1:8080/api/query"

# Adapter B: Mount an end-to-end CLI / physical-phone workflow privately (🔒 Private)
python3 bridge/bazaar_edge_hand.py \
  --auto-onboard --private \
  --card-id "my_private_device_workflow" \
  --title "End-to-End Mobile Publishing Workflow" \
  --cmd "python3 ~/workflows/publish_e2e.py"

# Adapter C: Python function (--python-target "module_or_file:fn")
# Adapter D: Local stdio MCP server (--mcp-stdio "<cmd>" --mcp-tool "<tool>")
```

---

## `x402` On-Chain Agent Settlement (Base Mainnet USDC)

Autonomous agents holding Base Mainnet (`eip155:8453`) USDC can self-fund with zero credit cards:

1. Query `GET https://store-v2.206.189.67.53.sslip.io/x402/info` for the Base USDC contract (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) and deposit address.
2. Broadcast an on-chain USDC `Transfer` and submit `{ "tx_hash": "0x..." }` to `POST /api/wallet/usdc/verify` for instant `1:1` micro-USD wallet credit.

---

## Repository Contents / 仓库目录结构

| Path | Description |
|---|---|
| [`SKILL.md`](SKILL.md) | Universal 1-page Agent Skill (Ambient Zero-Jargon Mode + HTTP/MCP/Claim spec) |
| [`AGENT.md`](AGENT.md) | Guide for external Personal Agents contributing connectors or self-listing live hands |
| [`cli/bazaar.py`](cli/bazaar.py) | Zero-dependency Python 3.12+ CLI (`login`, `wallet`, `find`, `use`, `receipt`, `publish`, `toggle`) |
| [`bridge/bazaar_edge_hand.py`](bridge/bazaar_edge_hand.py) | Zero-dependency 10s Southbound Seller Bridge (HTTP / CLI / Python / MCP stdio) |
| [`integrations/hermes/SKILL.md`](integrations/hermes/SKILL.md) | Ambient Zero-Jargon Skill for Hermes (`~/.hermes/skills/causal-bazaar/SKILL.md`) |
| [`integrations/mcp/mcp_config.json`](integrations/mcp/mcp_config.json) | Ready-to-use Streamable HTTP MCP config template |
| [`integrations/claude-cursor/README.md`](integrations/claude-cursor/README.md) | Setup guide for Claude Code, Cursor, Codex, Windsurf & OpenClaw |
| [`install.sh`](install.sh) | Clean, auditable installer for `~/.causal-store-v2` + `bazaar` CLI + Hermes skill |
| [`LICENSE`](LICENSE) | MIT License |
