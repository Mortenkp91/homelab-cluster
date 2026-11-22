#!/usr/bin/env bash
# Validate the repo the way Argo CD will consume it. Same command locally and
# in CI (.github/workflows/ci.yml); needs no cluster.
#
#   scripts/validate.sh [--out DIR]
#
# Checks, all of which run even if an earlier one fails:
#   render      every Application source dir builds with kustomize and every
#               Helm Application templates at its pinned chart version with the
#               values files it names (scripts/render.sh)
#   argocd      what a schema cannot see: paths and $values files exist,
#               projects exist, names are unique, sync waves are set, the chart
#               is sources[0], git sources track main, nothing is orphaned
#   schemas     kubeconform -strict over the Argo CD resources and everything
#               rendered, against Kubernetes $KUBERNETES_VERSION
#   policy      no Flux leftovers, routes attach to a Gateway that exists, no
#               plain Secrets, no resource owned by two Applications
#
# CRD schemas come first from the CRDs this repo itself installs (the pinned
# charts and the vendored Gateway API bundle), so a custom resource is checked
# against the CRD version it will actually meet, and a chart bump that changes
# a CRD is checked against the new one. The datreeio CRDs-catalog is the
# fallback for CRDs a controller creates at runtime (Cilium).
#
# A kind with no schema anywhere FAILS the run (no -ignore-missing-schemas):
# on the last gate before the cluster, "could not check" must not look like
# "checked". Kinds that really have no published schema are listed, with the
# reason, in KUBECONFORM_SKIP_KINDS below, and are printed on every run.
#
# Tools come from PATH: helm, kustomize, kubeconform, jq, git, python3 with
# PyYAML. scripts/install-tools.sh installs the versions CI pins.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$SCRIPT_DIR")
export GITOPS_CACHE_DIR=${GITOPS_CACHE_DIR:-$ROOT/.cache}
export KUBERNETES_VERSION=${KUBERNETES_VERSION:-1.37.0} # the k3s minor the node runs; same default as render.sh
# Comma-separated kinds that are knowingly not schema-validated; say why here.
#   CustomResourceDefinition: the Kubernetes schema set kubeconform uses does not
#     publish apiextensions.k8s.io, so there is nothing to validate against. The
#     CRDs are still parsed, and their schemas are what the custom resources
#     get validated with.
KUBECONFORM_SKIP_KINDS=${KUBECONFORM_SKIP_KINDS-CustomResourceDefinition}
CRD_CATALOG='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

OUT=$GITOPS_CACHE_DIR/validate
if [[ ${1:-} == --out ]]; then
  OUT=$2
  shift 2
fi
if (($#)); then
  sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi

missing=()
for tool in helm kustomize kubeconform jq git python3; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
python3 -c 'import yaml' 2>/dev/null || missing+=("python3-yaml (PyYAML)")
if ((${#missing[@]})); then
  echo "Missing tools: ${missing[*]}. See scripts/install-tools.sh." >&2
  exit 2
fi

# Application repoURLs are compared with this repo's own URL when it is known.
if [[ -z ${GITOPS_REPO_URL:-} ]]; then
  if [[ -n ${GITHUB_REPOSITORY:-} ]]; then
    GITOPS_REPO_URL="https://github.com/$GITHUB_REPOSITORY"
  else
    GITOPS_REPO_URL=$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)
  fi
fi
export GITOPS_REPO_URL

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
RENDERED=$OUT/rendered
SCHEMAS=$OUT/crd-schemas
rm -rf "$RENDERED" "$SCHEMAS"
mkdir -p "$RENDERED" "$SCHEMAS" "$GITOPS_CACHE_DIR/kubeconform"

results=()
record() { # record NAME STATUS
  results+=("$(printf '%s  %s' "$([[ $2 == 0 ]] && echo ok || echo FAIL)" "$1")")
  [[ $2 == 0 ]] || overall=1
}
overall=0
section() { printf '\n==> %s\n' "$1"; }

# --- render -------------------------------------------------------------------
section "render: kustomize build + helm template for everything Argo CD syncs"
status=0
"$SCRIPT_DIR/render.sh" "$RENDERED" || status=$?
INVENTORY=$RENDERED/inventory.json
if [[ ! -s $INVENTORY ]]; then
  echo "render.sh produced no inventory; cannot continue." >&2
  exit 1
fi
if (($(jq '(.dirs | length) + (.helm | length)' "$INVENTORY") == 0)); then
  echo "  FAILED    nothing to render: no Application sources were discovered"
  status=1
fi
record "render" "$status"

# --- Argo CD consistency -------------------------------------------------------
section "argocd: Application / ApplicationSet / AppProject consistency"
jq -r '"  \(.applications | length) Applications (incl. generated), \(.projects | length) AppProjects, " +
       "\(.helm | length) Helm charts, \(.dirs | length) source directories"' "$INVENTORY"
status=0
if (($(jq '.errors | length' "$INVENTORY") > 0)); then
  jq -r '.errors[] | "  FAIL  " + .' "$INVENTORY"
  status=1
else
  echo "  ok    every path, values file, project, name and sync wave checks out"
fi
jq -r '.warnings[] | "  note  " + .' "$INVENTORY"
record "argocd consistency" "$status"

# --- schemas ------------------------------------------------------------------
section "schemas: kubeconform -strict, Kubernetes $KUBERNETES_VERSION"
mapfile -t rendered_files < <(find "$RENDERED/manifests" "$RENDERED/helm" -name '*.yaml' 2>/dev/null | sort)
mapfile -t argo_files < <(jq -r --arg root "$ROOT/" '.argoFiles[] | $root + .' "$INVENTORY")
printf '  '
python3 "$SCRIPT_DIR/lib/gitops.py" crd-schemas --out "$SCHEMAS" "${rendered_files[@]}"

status=0
if ((${#rendered_files[@]} + ${#argo_files[@]} == 0)); then
  echo "  FAIL  nothing to validate"
  status=1
else
  skip=()
  [[ -n $KUBECONFORM_SKIP_KINDS ]] && skip=(-skip "$KUBECONFORM_SKIP_KINDS")
  kubeconform -strict -kubernetes-version "$KUBERNETES_VERSION" \
    -schema-location "$SCHEMAS/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    -schema-location default \
    -schema-location "$CRD_CATALOG" \
    -cache "$GITOPS_CACHE_DIR/kubeconform" "${skip[@]}" \
    -output json -verbose -summary \
    "${argo_files[@]}" "${rendered_files[@]}" >"$OUT/kubeconform.json" 2>"$OUT/kubeconform.err" || true

  if ! jq -e '.summary' "$OUT/kubeconform.json" >/dev/null 2>&1; then
    echo "  FAIL  kubeconform did not produce a report:"
    sed 's/^/          /' "$OUT/kubeconform.err" "$OUT/kubeconform.json" | head -n 20
    status=1
  else
    # One line per file, then every resource that is not valid.
    printf '%s\n' "${argo_files[@]}" "${rendered_files[@]}" | jq -R . | jq -s . >"$OUT/kubeconform.files.json"
    jq -r --arg rendered "$RENDERED/" --arg root "$ROOT/" --slurpfile files "$OUT/kubeconform.files.json" '
      (.resources // [] | group_by(.filename) | map({key: .[0].filename, value: .}) | from_entries) as $by
      | $files[0][] as $file
      | ($by[$file] // []) as $res
      | ($file | ltrimstr($rendered) | ltrimstr($root) | rtrimstr(".yaml")) as $name
      | ($res | map(select(.status == "statusInvalid" or .status == "statusError"))) as $bad
      | (if ($res | length) == 0 then "  FAIL  \($name): contains no resources"
         elif ($bad | length) > 0 then "  FAIL  \($name): \($bad | length) of \($res | length) resources rejected"
         else "  ok    \($name) (\($res | length))" end),
        ($bad[] | "          \(.kind) \(.name) [\(.version)]: " +
           (if (.validationErrors // [] | length) > 0
            then (.validationErrors | map("\(if .path == "" then "/" else .path end): \(.msg)") | join("; "))
            else .msg end))
    ' "$OUT/kubeconform.json" | tee "$OUT/kubeconform.txt"
    grep -q '^  FAIL' "$OUT/kubeconform.txt" && status=1
    jq -r '.summary | "  total: \(.valid) valid, \(.invalid) invalid, \(.errors) errors, \(.skipped) skipped"' \
      "$OUT/kubeconform.json"
    jq -r '[.resources // [] | .[] | select(.status == "statusSkipped") | .kind] | group_by(.)
           | map("\(.[0]) x\(length)") | if length > 0 then "  skipped kinds (not validated): " + join(", ")
             else "  skipped kinds: none" end' "$OUT/kubeconform.json"
  fi
fi
record "schemas" "$status"

# --- policy -------------------------------------------------------------------
section "policy: repo rules over the source tree and the rendered manifests"
status=0
python3 "$SCRIPT_DIR/lib/gitops.py" policy --root "$ROOT" --rendered "$RENDERED" --inventory "$INVENTORY" || status=$?
record "policy" "$status"

section "summary"
printf '  %s\n' "${results[@]}"
echo "  rendered manifests and reports: ${OUT#"$ROOT"/}"
exit "$overall"
