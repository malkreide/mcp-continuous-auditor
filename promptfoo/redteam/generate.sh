#!/usr/bin/env bash
#
# generate.sh — expand the committed red-team set with `promptfoo redteam generate`
# (Analysis T-A). This is the DELIBERATE, reviewed generation step: it synthesises
# adversarial cases from redteam.config.yaml with a PINNED attacker model and
# writes them to redteam.generated.yaml. That file is committed via a reviewed PR
# (see .github/workflows/redteam-regen.yml.template) and then evaluated like any
# other committed test — so the "deterministic gate" never becomes a moving target.
#
# Needs a model key for the attacker/synthesiser (this is NOT run on the
# credential-free Worker — it runs on the keyed Broker/CI side or a dev machine).
#
# Usage:
#   promptfoo/redteam/generate.sh            # generate -> redteam.generated.yaml
#
# Env:
#   PROMPTFOO_VERSION  pinned truth-engine version (must match nightly-audit.sh /
#                      CI — never @latest). Default: 0.121.17.
#   REDTEAM_ATTACKER   attacker/synthesiser model, passed as --provider. Pin it so
#                      a regeneration is a reviewable change. Default:
#                      openai:gpt-4o-mini (a resolvable, non-writer-family model).
#   NUM_TESTS          cases per plugin (--numTests). Default: 5.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPTFOO_VERSION="${PROMPTFOO_VERSION:-0.121.17}"
REDTEAM_ATTACKER="${REDTEAM_ATTACKER:-openai:gpt-4o-mini}"
NUM_TESTS="${NUM_TESTS:-5}"

config="${HERE}/redteam.config.yaml"
out="${HERE}/redteam.generated.yaml"

command -v npx >/dev/null || { echo "FATAL: npx (node) required" >&2; exit 127; }
[ -f "${config}" ] || { echo "FATAL: ${config} not found" >&2; exit 1; }

# Prefer the PINNED local install (reproducible transitive tree from the committed
# lockfile, Analysis T-G); fall back to a version-pinned npx.
pf_dir="$(cd "${HERE}/.." && pwd)"
pf_cmd=(npx -y "promptfoo@${PROMPTFOO_VERSION}")
if pf_bin="$("${HERE}/../../scripts/install-promptfoo.sh" "${pf_dir}")"; then
  pf_cmd=("${pf_bin}")
fi

echo "==> redteam generate (${pf_cmd[0]}, attacker=${REDTEAM_ATTACKER}, numTests=${NUM_TESTS})"
echo "    config : ${config}"
echo "    output : ${out}  (commit this via a reviewed PR)"

# `redteam generate` reads the redteam: block and writes concrete adversarial test
# cases. Pinned version + attacker model so the output is a deliberate artifact.
"${pf_cmd[@]}" redteam generate \
  -c "${config}" \
  -o "${out}" \
  --provider "${REDTEAM_ATTACKER}" \
  --numTests "${NUM_TESTS}"
rc=$?

if [ "${rc}" -ne 0 ]; then
  echo "FATAL: redteam generate failed (rc=${rc}) — attacker model resolvable? key set?" >&2
  exit "${rc}"
fi
echo "==> wrote ${out}. Review the diff, then open a PR. Evaluate it with:"
echo "    npx -y promptfoo@${PROMPTFOO_VERSION} eval -c ${out}"
