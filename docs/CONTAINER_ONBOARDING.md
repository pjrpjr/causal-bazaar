# Multi-SSH Container & Remote Worker Onboarding (`agy-02..10`)

Zero-dependency installation and project-scoped Buyer sub-key provisioning for headless Docker/SSH containers (`agy-02..10`) and isolated worker hosts.

## Security Boundaries
1. **Strict `0600` Key Isolation**: Keys live only inside `0700 ~/.causal-store-v2/buyer_key` and `0600 ~/.causal-store-v2/env.sh`. Never copy keys into Git repositories; `install.sh` refuses to write keyrings inside any Git working tree.
2. **Scoped Sub-Keys with Auto-Clawback**: Mint one bounded Buyer sub-key per container (`allocate_balance_micros` + `expires_at` TTL) using `POST /api/account/keys`. Revoking or expiring a key automatically claws unused balance back to the primary wallet.
3. **Zero Token Leakage on Command Line or SSH Args**: Pass tokens only via stdin (`--token-stdin`) or an isolated `0600` file (`--token-file`). Never place raw Buyer key tokens in CLI flags, Git commits, or `ps`-visible SSH arguments.

## 1. Host-Side Sub-Key Provisioning (`reality-os-network`)

From the host workspace (`pjrpjr/reality-os-network`), use `provision_container_subkey.py`:

```bash
# Preview without minting or SSH writes
python3 causal-store-v2/scripts/provision_container_subkey.py \
  --dry-run --project agy-02 --budget-usd 5.0 --ttl-days 7 --json

# Mint a $5.00, 7-day sub-key and stream over SSH stdin to agy-02 (no token echoed!)
python3 causal-store-v2/scripts/provision_container_subkey.py \
  --project agy-02 --budget-usd 5.0 --ttl-days 7 --ssh-host agy-02 --json

# List or revoke & claw back unused credits
python3 causal-store-v2/scripts/provision_container_subkey.py --list --json
python3 causal-store-v2/scripts/provision_container_subkey.py --revoke <key_id> --json
```

## 2. Container-Side Manual Headless Install (`install.sh --container`)

If provisioning a container directly after cloning `pjrpjr/causal-bazaar`:

```bash
git clone https://github.com/pjrpjr/causal-bazaar.git && cd causal-bazaar
printf '%s\n' "$BUYER_SUBKEY_TOKEN" | bash install.sh --container --token-stdin
export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.causal-store-v2/env.sh"
bazaar whoami --json
bazaar find "verify email" --limit 5 --json
```

Artifacts installed in the container:
- `~/.local/bin/bazaar` (zero-dependency Python 3 CLI; supports `--key-file`, `--keyring-dir`, `--store-url`, `--json`)
- `~/.hermes/skills/causal-bazaar/{SKILL.md,bazaar_cli.py}`
- `~/.causal-store-v2/{buyer_key,env.sh}` (`0700` dir, `0600` files)
