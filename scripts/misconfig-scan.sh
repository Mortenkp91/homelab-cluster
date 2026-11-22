#!/usr/bin/env bash
# Kubernetes misconfiguration report (kube-linter) over the rendered manifests.
#
#   scripts/misconfig-scan.sh [RENDERED_DIR]
#
# RENDERED_DIR is the output of scripts/render.sh; without it everything is
# rendered first. Prints Markdown: findings per check, split into this repo's
# own manifests (manifests/) and third-party chart output (helm/), which is not
# ours to fix beyond chart values.
#
# Advisory by default (exit 0 with findings). To make it a gate, clear or
# annotate the findings in the own manifests (per object:
#   metadata.annotations: {ignore-check.kube-linter.io/<check>: "<why>"}
# for the deliberate ones such as Home Assistant's hostNetwork), then run it with
# MISCONFIG_FAIL_ON=manifests (or "all" to include chart output).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$SCRIPT_DIR")
FAIL_ON=${MISCONFIG_FAIL_ON:-none}

RENDERED=${1:-}
if [[ -z $RENDERED ]]; then
  RENDERED=${GITOPS_CACHE_DIR:-$ROOT/.cache}/misconfig
  "$SCRIPT_DIR/render.sh" "$RENDERED" >&2 || echo "Some targets did not render; scanning the rest." >&2
fi
RENDERED=$(cd "$RENDERED" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

declare -A titles=(
  [manifests]="This repo's manifests (kustomize output)"
  [helm]="Third-party Helm chart output"
)
exit_code=0
echo "## Misconfiguration scan (kube-linter, advisory)"
for scope in manifests helm; do
  echo
  echo "### ${titles[$scope]}"
  echo
  if [[ ! -d $RENDERED/$scope ]]; then
    echo "Nothing rendered."
    continue
  fi
  # kube-linter exits 1 when it has findings; only a missing report is an error.
  kube-linter lint "$RENDERED/$scope" --format json >"$WORK/$scope.json" 2>"$WORK/$scope.err" || true
  if ! jq -e '.Summary' "$WORK/$scope.json" >/dev/null 2>&1; then
    echo "kube-linter did not produce a report:" >&2
    cat "$WORK/$scope.err" >&2
    exit 2
  fi
  total=$(jq '.Reports // [] | length' "$WORK/$scope.json")
  if ((total == 0)); then
    echo "No findings."
    continue
  fi
  echo "$total findings."
  echo
  echo "| Check | Findings | Remediation |"
  echo "|---|---:|---|"
  jq -r '.Reports | group_by(.Check) | sort_by(-length)[]
         | "| `\(.[0].Check)` | \(length) | \(.[0].Remediation | gsub("\\|"; "/") | gsub("\n"; " ")) |"' \
    "$WORK/$scope.json"
  echo
  echo "<details><summary>Every finding</summary>"
  echo
  jq -r --arg prefix "$RENDERED/" '.Reports | sort_by(.Object.Metadata.FilePath, .Check)[]
         | .Object.K8sObject as $o
         | "- `\(.Object.Metadata.FilePath | ltrimstr($prefix) | rtrimstr(".yaml"))` " +
           "\($o.GroupVersionKind.Kind) `\(if $o.Namespace != "" then $o.Namespace + "/" else "" end)\($o.Name)`: " +
           "\(.Diagnostic.Message) (`\(.Check)`)"' "$WORK/$scope.json"
  echo
  echo "</details>"
  if [[ $FAIL_ON == all || $FAIL_ON == "$scope" ]]; then
    exit_code=1
  fi
done
exit "$exit_code"
