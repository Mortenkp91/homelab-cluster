#!/usr/bin/env bash
# Render everything Argo CD will sync, without a cluster.
#
#   scripts/render.sh [--root DIR] <outdir>
#
# What to render is discovered from the Argo CD resources themselves
# (scripts/lib/gitops.py inventory): every Application / ApplicationSet source
# directory, every Helm chart Application, plus any directory under the
# conventional locations that nothing references.
#
#   <outdir>/manifests/<repo path>.yaml   kustomize build (or plain YAML dir)
#   <outdir>/helm/<application>.yaml      helm template at the pinned version,
#                                         with the Application's values files,
#                                         release name and namespace (a second
#                                         chart in one Application lands in
#                                         <application>--<chart>.yaml)
#   <outdir>/inventory.json               what was discovered
#   <outdir>/FAILED                       one line per target that did not render
#
# --root renders another checkout with THIS copy of the scripts; the PR diff
# uses it to render the base branch. Exit status is 1 if any target failed, but
# every target is attempted.
#
# Environment:
#   GITOPS_CACHE_DIR    chart cache (default: .cache in this repo)
#   KUBERNETES_VERSION  passed to helm as --kube-version (default: 1.35.0)
#   GITOPS_REPO_URL     this repo's URL, to check Application repoURLs against
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$SCRIPT_DIR")
CACHE_DIR=${GITOPS_CACHE_DIR:-$ROOT/.cache}
KUBERNETES_VERSION=${KUBERNETES_VERSION:-1.35.0} # the k3s minor the node runs; same default as validate.sh

if [[ ${1:-} == --root ]]; then
  ROOT=$(cd "$2" && pwd)
  shift 2
fi
if (($# != 1)); then
  sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi
mkdir -p "$1" "$CACHE_DIR/charts"
OUT=$(cd "$1" && pwd)
rm -rf "$OUT/manifests" "$OUT/helm" "$OUT/FAILED" "$OUT/inventory.json"

INVENTORY=$OUT/inventory.json
python3 "$SCRIPT_DIR/lib/gitops.py" inventory --root "$ROOT" --repo-url "${GITOPS_REPO_URL:-}" >"$INVENTORY"

failed=0
# finish TARGET STATUS: report one target; on failure keep its stderr next to the output.
finish() {
  local target=$1 status=$2 out="$OUT/$1.yaml"
  if ((status == 0)); then
    rm -f "$out.err"
    printf '  rendered  %s\n' "$target"
  else
    rm -f "$out"
    printf '  FAILED    %s\n' "$target"
    sed 's/^/            /' "$out.err" | head -n 15
    printf '%s\t%s\n' "$target" "$(grep -m1 . "$out.err" | cut -c1-300)" >>"$OUT/FAILED"
    failed=1
  fi
}

# --- directories: kustomize build, or the YAML files as they are -------------
read -r -a kustomize_opts <<<"$(jq -r '.kustomizeBuildOptions' "$INVENTORY")"
while IFS=$'\t' read -r path recurse; do
  target="manifests/$path"
  out="$OUT/$target.yaml"
  mkdir -p "$(dirname "$out")"
  status=0
  if compgen -G "$ROOT/$path/[kK]ustomization*" >/dev/null; then
    kustomize build "${kustomize_opts[@]}" "$ROOT/$path" >"$out" 2>"$out.err" || status=$?
  else
    depth=(-maxdepth 1)
    [[ $recurse == true ]] && depth=()
    find "$ROOT/$path" "${depth[@]}" -type f \( -name '*.yaml' -o -name '*.yml' \) -print0 | sort -z |
      while IFS= read -r -d '' file; do
        printf -- '---\n# Source: %s\n' "${file#"$ROOT"/}"
        cat "$file"
        echo
      done >"$out" 2>"$out.err" || status=$?
  fi
  finish "$target" "$status"
done < <(jq -r '.dirs[] | [.path, .recurse] | @tsv' "$INVENTORY")

# --- Helm chart Applications ---------------------------------------------------
# Charts are pulled once per (repo, chart, version) into the cache; neither
# 'helm repo add' nor the user's helm repo config is involved.
pull_chart() {
  local repo=$1 chart=$2 version=$3 oci_ref=$4 dest=$5 tmp
  tmp=$(mktemp -d "$CACHE_DIR/charts/.pull.XXXXXX")
  if [[ -n $oci_ref ]]; then
    helm pull "$oci_ref" --version "$version" --destination "$tmp"
  else
    helm pull "$chart" --repo "$repo" --version "$version" --destination "$tmp"
  fi && mv "$tmp"/*.tgz "$dest"
  local status=$?
  rm -rf "$tmp"
  return $status
}

count=$(jq '.helm | length' "$INVENTORY")
for ((i = 0; i < count; i++)); do
  IFS=$'\t' read -r name repo chart version oci_ref release namespace skip_crds < <(
    jq -r --argjson i "$i" \
      '.helm[$i] | [.target, .repoURL, .chart, .version, (.ociRef // "-"), .releaseName, .namespace, .skipCrds] | @tsv' \
      "$INVENTORY"
  )
  [[ $oci_ref == - ]] && oci_ref=""
  target="helm/$name"
  out="$OUT/$target.yaml"
  mkdir -p "$OUT/helm"
  status=0

  chart_key=$(printf '%s' "$repo" | sha256sum | cut -c1-12)
  tgz="$CACHE_DIR/charts/$chart_key-$chart-$version.tgz"
  if [[ ! -s $tgz ]]; then
    pull_chart "$repo" "$chart" "$version" "$oci_ref" "$tgz" >"$out.err" 2>&1 || status=$?
  fi

  if ((status == 0)); then
    args=(template "$release" "$tgz" --namespace "$namespace" --kube-version "$KUBERNETES_VERSION")
    [[ $skip_crds == true ]] || args+=(--include-crds)
    while IFS= read -r values; do
      args+=(--values "$ROOT/$values")
    done < <(jq -r --argjson i "$i" '.helm[$i].valueFiles[]' "$INVENTORY")
    helm "${args[@]}" >"$out" 2>"$out.err" || status=$?
  fi
  finish "$target" "$status"
done

exit "$failed"
