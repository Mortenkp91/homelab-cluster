#!/usr/bin/env bash
# Print the manifests of a Helm-chart Application the way Argo CD will render it.
#
#   bootstrap/render-chart.sh clusters/production/infrastructure/cilium.yaml
#
# Chart, repository, version, release name and namespace are read from the Application file, and the
# values from the file(s) its `$values/...` entries point at, so the two installs that have to happen
# before Argo CD can do them itself (Cilium, Argo CD) use exactly what is pinned in Git.
# Only renders; pipe the output to kubectl yourself. Needs helm, yq (mikefarah v4) and kubectl.
#
# Set KUBE_VERSION (e.g. KUBE_VERSION=1.35.4) to render without asking a cluster for its version.
set -euo pipefail

app=${1:?usage: render-chart.sh <path to an Application yaml with spec.sources>}
root=$(cd "$(dirname "$0")/.." && pwd)

repo=$(yq '.spec.sources[0].repoURL' "$app")
chart=$(yq '.spec.sources[0].chart' "$app")
version=$(yq '.spec.sources[0].targetRevision' "$app")
release=$(yq '.spec.sources[0].helm.releaseName' "$app")
namespace=$(yq '.spec.destination.namespace' "$app")

for field in repo chart version release namespace; do
  if [[ -z ${!field} || ${!field} == null ]]; then
    echo "render-chart.sh: could not read $field from $app" >&2
    exit 1
  fi
done

args=(template "$release")
if [[ $repo == http://* || $repo == https://* ]]; then
  args+=("$chart" --repo "$repo")
else
  # Argo CD writes OCI registries without the scheme.
  args+=("oci://$repo/$chart")
fi
args+=(--version "$version" --namespace "$namespace" --include-crds)

while IFS= read -r file; do
  # shellcheck disable=SC2016 # literal $values, the Argo CD ref name
  args+=(--values "$root/${file#'$values/'}")
done < <(yq '.spec.sources[0].helm.valueFiles[]' "$app")

if [[ -n ${KUBE_VERSION:-} ]]; then
  args+=(--kube-version "$KUBE_VERSION")
else
  # Argo CD passes the cluster's version and API groups to Helm; do the same so charts that branch on
  # .Capabilities render identically.
  server_version=$(kubectl version -o json | yq '.serverVersion.gitVersion')
  args+=(--kube-version "${server_version%%+*}")
  while IFS= read -r api; do
    args+=(--api-versions "$api")
  done < <(kubectl api-versions)
fi

exec helm "${args[@]}"
