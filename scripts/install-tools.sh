#!/usr/bin/env bash
# Install the pinned CLI tools the CI checks use.
#
#   scripts/install-tools.sh [--dir DIR] <tool>...     # install the named tools
#   scripts/install-tools.sh [--dir DIR] all           # install everything
#
# This file is the single place tool versions are pinned. Renovate bumps them
# through the regex manager in renovate.json, which keys on the
# "# renovate: ..." comment directly above each *_VERSION line.
#
# Binaries land in DIR (default: .cache/bin in the repo, which is gitignored).
# Nothing is installed system-wide. In CI the directory is appended to
# $GITHUB_PATH; locally run:  export PATH="$PWD/.cache/bin:$PATH"
set -euo pipefail

# Argo CD's repo-server renders with its own bundled helm and kustomize (see
# hack/tool-versions.sh in argoproj/argo-cd). Keep these two on the same major
# version as the Argo CD release that is deployed, or CI renders something
# different from what gets applied.
# renovate: datasource=github-releases depName=helm/helm
HELM_VERSION=v4.3.2
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize extractVersion=^kustomize/(?<version>.+)$
KUSTOMIZE_VERSION=v5.8.3
# renovate: datasource=github-releases depName=yannh/kubeconform
KUBECONFORM_VERSION=v0.8.0
# renovate: datasource=github-releases depName=rhysd/actionlint
ACTIONLINT_VERSION=v1.7.12
# renovate: datasource=github-releases depName=koalaman/shellcheck
SHELLCHECK_VERSION=v0.11.0
# renovate: datasource=pypi depName=yamllint
YAMLLINT_VERSION=1.38.0
# renovate: datasource=pypi depName=pathspec
PATHSPEC_VERSION=1.1.1
# renovate: datasource=github-releases depName=gitleaks/gitleaks
GITLEAKS_VERSION=v8.30.1
# renovate: datasource=github-releases depName=stackrox/kube-linter
KUBE_LINTER_VERSION=v0.8.3

ALL_TOOLS=(helm kustomize kubeconform actionlint shellcheck yamllint gitleaks kube-linter)

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BIN_DIR="$REPO_ROOT/.cache/bin"

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  echo
  echo "Tools: ${ALL_TOOLS[*]}"
}

tools=()
while (($#)); do
  case "$1" in
    --dir) BIN_DIR=$2; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    all) tools=("${ALL_TOOLS[@]}"); shift ;;
    *) tools+=("$1"); shift ;;
  esac
done
if ((${#tools[@]} == 0)); then
  usage >&2
  exit 2
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) ;;
  *) echo "install-tools.sh only knows the linux/amd64 release assets (got $(uname -sm))." >&2; exit 1 ;;
esac

mkdir -p "$BIN_DIR"
BIN_DIR=$(cd "$BIN_DIR" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fetch() { curl --fail --silent --show-error --location --retry 3 --retry-delay 2 "$1" --output "$2"; }

# fetch_tar URL MEMBER NAME: extract one member of a tarball to $BIN_DIR/NAME.
fetch_tar() {
  local url=$1 member=$2 name=$3 work="$TMP/$3"
  mkdir -p "$work"
  fetch "$url" "$work/archive"
  tar --extract --auto-compress --file "$work/archive" --directory "$work" "$member"
  install -m 0755 "$work/$member" "$BIN_DIR/$name"
}

install_tool() {
  local gh=https://github.com v
  case "$1" in
    helm)
      fetch_tar "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" linux-amd64/helm helm ;;
    kustomize)
      fetch_tar "$gh/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" kustomize kustomize ;;
    kubeconform)
      fetch_tar "$gh/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" kubeconform kubeconform ;;
    actionlint)
      v=${ACTIONLINT_VERSION#v}
      fetch_tar "$gh/rhysd/actionlint/releases/download/${ACTIONLINT_VERSION}/actionlint_${v}_linux_amd64.tar.gz" actionlint actionlint ;;
    shellcheck)
      fetch_tar "$gh/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" "shellcheck-${SHELLCHECK_VERSION}/shellcheck" shellcheck ;;
    gitleaks)
      v=${GITLEAKS_VERSION#v}
      fetch_tar "$gh/gitleaks/gitleaks/releases/download/${GITLEAKS_VERSION}/gitleaks_${v}_linux_x64.tar.gz" gitleaks gitleaks ;;
    kube-linter)
      fetch_tar "$gh/stackrox/kube-linter/releases/download/${KUBE_LINTER_VERSION}/kube-linter-linux.tar.gz" kube-linter kube-linter ;;
    yamllint)
      # Pure-Python wheels unpacked next to the binaries, so this needs neither
      # pip nor venv and touches no site-packages. PyYAML comes from the system
      # python3 (the validation scripts need it anyway).
      local lib="$BIN_DIR/.yamllint" pypi=https://files.pythonhosted.org/packages/py3
      rm -rf "$lib"
      mkdir -p "$lib"
      fetch "$pypi/y/yamllint/yamllint-${YAMLLINT_VERSION}-py3-none-any.whl" "$TMP/yamllint.whl"
      fetch "$pypi/p/pathspec/pathspec-${PATHSPEC_VERSION}-py3-none-any.whl" "$TMP/pathspec.whl"
      python3 -m zipfile -e "$TMP/yamllint.whl" "$lib"
      python3 -m zipfile -e "$TMP/pathspec.whl" "$lib"
      printf '#!/bin/sh\nPYTHONPATH="%s" exec python3 -m yamllint "$@"\n' "$lib" >"$BIN_DIR/yamllint"
      chmod 0755 "$BIN_DIR/yamllint" ;;
    *)
      echo "unknown tool: $1 (known: ${ALL_TOOLS[*]})" >&2
      return 1 ;;
  esac
}

for tool in "${tools[@]}"; do
  install_tool "$tool"
  echo "installed $tool -> $BIN_DIR/$tool"
done

if [[ -n ${GITHUB_PATH:-} ]]; then
  echo "$BIN_DIR" >>"$GITHUB_PATH"
fi
