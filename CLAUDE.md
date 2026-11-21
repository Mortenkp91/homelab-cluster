# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a GitOps-managed Kubernetes homelab cluster running on k3s with Argo CD. The cluster runs on a single node (`mpe-001`, an Intel NUC) running Ubuntu. All workloads and local PersistentVolumes must target `mpe-001`. All configuration is declarative YAML managed through Git; Argo CD pulls from `main`.

## Repository Structure

```
bootstrap/                    # One-time manual steps: runbook, k3s config, root app, chart render helper
clusters/production/          # Argo CD entry point, synced recursively as plain YAML by the root app
  projects.yaml               # AppProjects `infrastructure` and `apps`
  apps.yaml                   # ApplicationSet: one Application per directory under apps/production
  infrastructure/<name>.yaml  # One Application per infrastructure component
infrastructure/
  base/<component>/           # Helm components: values.yaml. Raw-manifest components: kustomize base
  production/<component>/     # Kustomize overlays for raw-manifest components
  configs/<component>/        # Post-install config that needs the component's CRDs
apps/
  base/                       # Application base definitions
  production/                 # Production app overlays (each directory is one Argo CD Application)
scripts/                      # validate.sh / render.sh, shared by CI and local use
```

## GitOps Flow

`bootstrap/root-app.yaml` (applied once by hand) syncs `clusters/production/`. The Applications there carry `argocd.argoproj.io/sync-wave` annotations, and the root app waits for each wave to be Healthy before starting the next:

| Wave | Applications |
|------|--------------|
| 0 | `cilium` (CNI and kube-proxy replacement) |
| 1 | `gateway-api` (CRDs), `cilium-config` (LoadBalancer IP pool, L2 announcements), `local` (static PVs) |
| 2 | `argocd`, `external-secrets`, `cert-manager`, `envoy-gateway` |
| 3 | `external-secrets-config` (ClusterSecretStore) |
| 4 | `cert-manager-config` (ClusterIssuer), `external-dns-config` |
| 5 | `envoy-gateway-config` (GatewayClass, Gateway, wildcard certificate), `external-dns` |
| 6 | `argocd-config`, `hubble-config` (UI routes) |
| 7 | `apps` ApplicationSet |

`bootstrap/README.md` is the runbook for building the cluster from an empty node and explains what each wave waits for.

## Key Patterns

**Adding a Helm-based infrastructure component:**
- Put the values in `infrastructure/base/<component>/values.yaml` (no inline `valuesObject`)
- Add `clusters/production/infrastructure/<component>.yaml`: a multi-source Application with the chart as `sources[0]` and the `ref: values` Git source as `sources[1]`, project `infrastructure`, a sync-wave annotation, and no resources finalizer. Copy an existing one such as `cert-manager.yaml`
- Resources that need the component's CRDs go in `infrastructure/configs/<component>/` with their own `<component>-config` Application in a later wave

**Adding a raw-manifest infrastructure component:**
- Kustomize base in `infrastructure/base/<component>/`, overlay in `infrastructure/production/<component>/`
- A single-source Application in `clusters/production/infrastructure/` pointing at the overlay

**Adding new applications:**
- Create the base in `apps/base/<app>/` and the overlay in `apps/production/<app>/`
- Nothing else: the ApplicationSet picks up every directory under `apps/production/`. The directory name must equal the namespace
- Expose it with an HTTPRoute whose `parentRefs` is `homelab-gateway` in namespace `envoy-gateway-system`, `sectionName: https`

**Secrets management:**
- External Secrets Operator syncs from Azure Key Vault through the `azure-kv-store` ClusterSecretStore
- Never commit actual secrets - use ExternalSecret resources (CI fails on plain `Secret` manifests)

## Infrastructure Components

- **Argo CD**: GitOps engine, self-managed through its own Application; UI at `argocd.mkped.com`
- **Cilium**: CNI and kube-proxy replacement (k3s runs with flannel, kube-proxy and its network policy controller disabled). Also assigns LoadBalancer IPs (LB-IPAM pool 10.20.22.10-20, announced over ARP on `enp2s0`); pin one with the `lbipam.cilium.io/ips` annotation and keep `externalTrafficPolicy: Cluster`. Hubble relay and UI are on (`hubble.mkped.com`); the L7 proxy is off. The API server is reached on the node IP 10.20.22.81 (no control-plane VIP)
- **Envoy Gateway**: Gateway API implementation on 10.20.22.11, with HTTP->HTTPS redirect. The vendored Gateway API CRDs in `infrastructure/base/gateway-api/` must match the version Envoy Gateway supports
- **cert-manager**: TLS certificates via Let's Encrypt (DNS-01 through Cloudflare)
- **external-dns**: Cloudflare records for every HTTPRoute, all pointing at the gateway IP
- **Local PersistentVolumes** (`infrastructure/base/local/`): static PVs on `mpe-001`; app config lives under `/srv/nfs/configs/<app>`. Argo CD never prunes them

## Validating changes

There is nothing to build, but every change should pass the same checks CI runs on pull requests:

```bash
scripts/validate.sh          # kustomize build + helm template of every Application, schema and cross-reference checks
scripts/render.sh <outdir>   # write the fully rendered manifests, e.g. to diff before and after a chart bump
```

CI pins its tool versions in `scripts/install-tools.sh` (Helm 4, matching what Argo CD renders with). `scripts/install-tools.sh all && export PATH="$PWD/.cache/bin:$PATH"` gives the same versions locally. The required status check on pull requests is `ci-ok`.

Changes are applied by merging to `main`; Argo CD syncs automatically. To force it (requires cluster access):

```bash
argocd app sync root         # or: kubectl -n argocd annotate application <name> argocd.argoproj.io/refresh=hard --overwrite
```

Cilium and Argo CD are also installed by hand during bootstrap from the same Application and values files (`bootstrap/render-chart.sh`), so keep everything they need in those files.
