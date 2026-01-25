# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a GitOps-managed Kubernetes homelab cluster running on k3s with Flux CD. The cluster consists of 3 nodes (1 master Intel NUC, 2 worker Lenovo ThinkCentre) running Ubuntu. All configuration is declarative YAML managed through Git.

## Repository Structure

```
clusters/production/          # Flux entry point - references infrastructure and apps
infrastructure/
  base/                       # Base component definitions (HelmReleases, namespaces, RBAC)
  configs/                    # Post-deployment configs (e.g., SecretStore for ESO)
  production/                 # Production overlays using Kustomize
apps/
  base/                       # Application base definitions
  production/                 # Production app overlays
boostrapping/                 # Manual setup docs (Azure Key Vault, service principals)
```

## GitOps Flow

Flux reconciles in this order (defined in `clusters/production/`):
1. `infra-resources` - deploys `infrastructure/production/` (namespaces, CRDs, controllers)
2. `infra-configs` - deploys `infrastructure/configs/` (depends on infra-resources)
3. `apps` - deploys `apps/production/`

## Key Patterns

**Adding new infrastructure components:**
- Create base definition in `infrastructure/base/<component>/` with HelmRelease, namespace, repository
- Add Kustomize overlay in `infrastructure/production/<component>/`
- Reference in `infrastructure/production/kustomization.yaml`

**Adding new applications:**
- Create base in `apps/base/<app>/`
- Add production overlay in `apps/production/<app>/`
- Reference in `apps/production/kustomization.yaml`

**Secrets management:**
- External Secrets Operator syncs from Azure Key Vault
- SecretStore config in `infrastructure/configs/external-secrets-operator/`
- Never commit actual secrets - use ExternalSecret resources

## Infrastructure Components

- **kubevip + kubevip-cloud-controller**: LoadBalancer IP assignment (10.20.22.10-20 range)
- **Traefik**: Ingress controller with automatic HTTP->HTTPS redirect
- **cert-manager**: TLS certificates via Let's Encrypt
- **kube-prometheus-stack + Loki**: Monitoring and logging
- **NFS**: Shared storage from master node

## No Build/Test Commands

This is a declarative infrastructure repository. Changes are applied by:
1. Committing YAML changes to the main branch
2. Flux automatically reconciles within configured intervals

To force immediate reconciliation (requires cluster access):
```bash
flux reconcile kustomization flux-system --with-source
```
