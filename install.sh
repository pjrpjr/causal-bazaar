#!/usr/bin/env bash
# Clean, auditable zero-dependency installer for Causal Bazaar CLI, 0600 Keyring & Hermes/Claude/Codex Skills.
set -euo pipefail

STORE_URL="${BAZAAR_STORE_URL:-https://store-v2.206.189.67.53.sslip.io}"
KEYRING_DIR="${BAZAAR_KEYRING_DIR:-$HOME/.causal-store-v2}"
BIN_DIR="${BAZAAR_BIN_DIR:-$HOME/.local/bin}"
HERMES_DIR="${BAZAAR_HERMES_DIR:-$HOME/.hermes}"
CLAUDE_DIR="${BAZAAR_CLAUDE_DIR:-$HOME/.claude}"
CODEX_DIR="${BAZAAR_CODEX_DIR:-$HOME/.codex}"
DRY_RUN=0
RUN_LOGIN=0
SKIP_HERMES=0
SKIP_AGENT_SKILLS=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --store-url <url>     Causal Bazaar URL (default: https://store-v2.206.189.67.53.sslip.io)
  --keyring-dir <dir>   0600 keyring & runtime directory (default: ~/.causal-store-v2)
  --bin-dir <dir>       Directory for `bazaar` CLI symlink (default: ~/.local/bin)
  --hermes-dir <dir>    Hermes root directory (default: ~/.hermes)
  --claude-dir <dir>    Claude Code root directory (default: ~/.claude)
  --codex-dir <dir>     Codex root directory (default: ~/.codex)
  --skip-hermes         Skip installing ~/.hermes/skills/causal-bazaar/{SKILL.md,bazaar_cli.py}
  --skip-agent-skills   Skip installing Claude/Codex SKILL.md bundles
  --login               Immediately launch `bazaar login` after installation
  --dry-run             Print planned actions without modifying any files
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --store-url)
      STORE_URL="$2"
      shift 2
      ;;
    --keyring-dir)
      KEYRING_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="$2"
      shift 2
      ;;
    --hermes-dir)
      HERMES_DIR="$2"
      shift 2
      ;;
    --claude-dir)
      CLAUDE_DIR="$2"
      shift 2
      ;;
    --codex-dir)
      CODEX_DIR="$2"
      shift 2
      ;;
    --skip-hermes)
      SKIP_HERMES=1
      shift
      ;;
    --skip-agent-skills)
      SKIP_AGENT_SKILLS=1
      shift
      ;;
    --login)
      RUN_LOGIN=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[causal-bazaar install] Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "[causal-bazaar install] ERROR: python3 (3.12+) is required." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CLI="${SCRIPT_DIR}/cli/bazaar.py"
SRC_BRIDGE="${SCRIPT_DIR}/bridge/bazaar_edge_hand.py"
SRC_HERMES_SKILL="${SCRIPT_DIR}/integrations/hermes/SKILL.md"
SRC_UNIVERSAL_SKILL="${SCRIPT_DIR}/SKILL.md"

for req_file in "${SRC_CLI}" "${SRC_BRIDGE}" "${SRC_HERMES_SKILL}" "${SRC_UNIVERSAL_SKILL}"; do
  if [[ ! -f "${req_file}" ]]; then
    echo "[causal-bazaar install] ERROR: missing bundle file: ${req_file}" >&2
    exit 1
  fi
done

STORE_URL="${STORE_URL%/}"
KR_BIN_DIR="${KEYRING_DIR}/bin"
ENV_SH="${KEYRING_DIR}/env.sh"
BUYER_KEY="${KEYRING_DIR}/buyer_key"
SEAT_KEY="${KEYRING_DIR}/edge_seat_key"
HERMES_SKILL_DIR="${HERMES_DIR}/skills/causal-bazaar"
CLAUDE_SKILL_DIR="${CLAUDE_DIR}/skills/causal-bazaar"
CODEX_SKILL_DIR="${CODEX_DIR}/skills/causal-bazaar"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[causal-bazaar install] DRY RUN (no files modified):"
  echo "  1. Create 0700 keyring dir: ${KEYRING_DIR}"
  echo "  2. Install CLI & 10s Edge Bridge -> ${KR_BIN_DIR}/{bazaar,bazaar_edge_hand.py}"
  echo "  3. Link executable -> ${BIN_DIR}/bazaar"
  echo "  4. Write 0600 env helper -> ${ENV_SH} (STORE_URL=${STORE_URL})"
  if [[ "${SKIP_HERMES}" -eq 0 ]]; then
    echo "  5. Install Ambient Hermes Skill -> ${HERMES_SKILL_DIR}/{SKILL.md,bazaar_cli.py}"
  fi
  if [[ "${SKIP_AGENT_SKILLS}" -eq 0 ]]; then
    echo "  6. Install Claude & Codex Skills -> ${CLAUDE_SKILL_DIR}/SKILL.md, ${CODEX_SKILL_DIR}/SKILL.md"
  fi
  exit 0
fi

mkdir -p "${KR_BIN_DIR}" "${BIN_DIR}"
chmod 700 "${KEYRING_DIR}"

cp "${SRC_CLI}" "${KR_BIN_DIR}/bazaar"
cp "${SRC_BRIDGE}" "${KR_BIN_DIR}/bazaar_edge_hand.py"
chmod 755 "${KR_BIN_DIR}/bazaar" "${KR_BIN_DIR}/bazaar_edge_hand.py"
ln -sf "${KR_BIN_DIR}/bazaar" "${BIN_DIR}/bazaar"

umask 077
cat > "${ENV_SH}" <<EOF
# Managed by Causal Bazaar install.sh (mode 0600, zero inline secrets)
export BAZAAR_STORE_URL='${STORE_URL}'
export BAZAAR_KEYRING_DIR='${KEYRING_DIR}'
export BAZAAR_EDGE_SEAT_KEY_FILE='${SEAT_KEY}'
if [ -f '${BUYER_KEY}' ]; then
  export BAZAAR_API_KEY="\$(tr -d '\r\n' < '${BUYER_KEY}')"
elif [ -f '${SEAT_KEY}' ]; then
  export BAZAAR_API_KEY="\$(tr -d '\r\n' < '${SEAT_KEY}')"
fi
EOF
chmod 600 "${ENV_SH}"

if [[ "${SKIP_HERMES}" -eq 0 ]]; then
  mkdir -p "${HERMES_SKILL_DIR}"
  cp "${SRC_HERMES_SKILL}" "${HERMES_SKILL_DIR}/SKILL.md"
  cp "${SRC_CLI}" "${HERMES_SKILL_DIR}/bazaar_cli.py"
  chmod 644 "${HERMES_SKILL_DIR}/SKILL.md"
  chmod 755 "${HERMES_SKILL_DIR}/bazaar_cli.py"
fi

if [[ "${SKIP_AGENT_SKILLS}" -eq 0 ]]; then
  mkdir -p "${CLAUDE_SKILL_DIR}" "${CODEX_SKILL_DIR}"
  cp "${SRC_UNIVERSAL_SKILL}" "${CLAUDE_SKILL_DIR}/SKILL.md"
  cp "${SRC_UNIVERSAL_SKILL}" "${CODEX_SKILL_DIR}/SKILL.md"
  chmod 644 "${CLAUDE_SKILL_DIR}/SKILL.md" "${CODEX_SKILL_DIR}/SKILL.md"
fi

echo "[causal-bazaar] Installed cleanly:"
echo "  - CLI:          ${BIN_DIR}/bazaar"
echo "  - Edge Bridge:  ${KR_BIN_DIR}/bazaar_edge_hand.py"
echo "  - Keyring:      ${KEYRING_DIR} (0700, env.sh 0600)"
if [[ "${SKIP_HERMES}" -eq 0 ]]; then
  echo "  - Hermes Skill: ${HERMES_SKILL_DIR}/SKILL.md"
fi
if [[ "${SKIP_AGENT_SKILLS}" -eq 0 ]]; then
  echo "  - Claude Skill: ${CLAUDE_SKILL_DIR}/SKILL.md"
  echo "  - Codex Skill:  ${CODEX_SKILL_DIR}/SKILL.md"
fi
echo ""
echo "Next step — bind your Causal Wallet in 1 click:"
echo "  ${BIN_DIR}/bazaar login --store-url \"${STORE_URL}\""

if [[ "${RUN_LOGIN}" -eq 1 ]]; then
  exec python3 "${KR_BIN_DIR}/bazaar" login --store-url "${STORE_URL}" --keyring-dir "${KEYRING_DIR}"
fi
