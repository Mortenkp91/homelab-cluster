---
name: argocd
description: >-
  Argo CD (declarative GitOps continuous delivery for Kubernetes) reference: installing Argo CD, writing Application, ApplicationSet and AppProject manifests, app-of-apps, sync policies, sync waves and hooks, rollbacks, and the argocd CLI. Use whenever the user mentions Argo CD, argocd, Application/ApplicationSet resources, or migrating this cluster from Flux to Argo CD, even if they do not ask for the skill by name.
license: Apache-2.0
compatibility: "Kubernetes cluster with kubectl. Argo CD 3.x and the argocd CLI (Linux, macOS, Windows)."
metadata:
  author: terminal-skills
  version: "1.1.0"
  category: devops
  tags: "argocd, gitops, kubernetes, continuous-delivery, devops"
  repository: https://github.com/argoproj/argo-cd
  source: https://github.com/TerminalSkills/skills/blob/main/skills/argocd/SKILL.md
---

# Argo CD

## Overview

Argo CD runs in the cluster, watches Git (or Helm) sources and compares the manifests with live resources. An `Application` links one source to one destination; `ApplicationSet` generates many Applications; `AppProject` limits which repos, clusters and namespaces a team may use. Drift is shown as OutOfSync and can be repaired automatically. This skill targets Argo CD 3.x (checked against v3.5.5).

## In this repository

This cluster is reconciled by Argo CD (v3.6, chart pinned in `clusters/production/infrastructure/argocd.yaml`). `CLAUDE.md` describes the layout and sync waves; `bootstrap/README.md` is the runbook for building the cluster. The conventions below differ from the generic examples further down, and they win:

- Everything is declarative and committed to Git. Write Application manifests into the repo; do not create or change them with `argocd app create`/`argocd app set`, because the root app has `selfHeal` and reverts it.
- `bootstrap/root-app.yaml` (applied once by hand) syncs `clusters/production/` recursively as plain YAML. Infrastructure is one Application per file in `clusters/production/infrastructure/`; apps come from the ApplicationSet in `clusters/production/apps.yaml`, one per directory under `apps/production/`.
- Helm components are multi-source Applications: the chart is `sources[0]`, the Git `ref: values` source is `sources[1]`, and all values live in `infrastructure/base/<component>/values.yaml`. No inline `valuesObject`: CI, Renovate and `bootstrap/render-chart.sh` depend on that shape. OCI charts use `repoURL` without the `oci://` scheme.
- Ordering is by `argocd.argoproj.io/sync-wave` on the Applications. It only works because `infrastructure/base/argocd/values.yaml` adds the Application health check; a new component needs a wave after everything whose CRDs or secrets it uses.
- Infrastructure Applications carry no `resources-finalizer`, and the ApplicationSet sets `preserveResourcesOnDeletion`: deleting an Application must never tear down the CNI, CRDs or a namespace holding PVCs. The static PVs are never pruned.
- There is one cluster and one node (`mpe-001`): the destination is always `https://kubernetes.default.svc`. The multi-cluster ApplicationSet example below is reference only.
- Secrets come from Azure Key Vault through External Secrets Operator (`ClusterSecretStore` `azure-kv-store`). The repo is public, so Argo CD needs no repository credentials.
- The UI is exposed at `argocd.mkped.com` through Envoy Gateway (`infrastructure/configs/argocd/`), with TLS terminated at the gateway. The CLI therefore needs `argocd login argocd.mkped.com --grpc-web`.
- Cilium and Argo CD are also rendered by hand during bootstrap from their Application and values files, so a change that makes either render differently between two `helm template` runs (generated certificates, random secrets) shows up as permanent drift.
- Run `scripts/validate.sh` after changing any Application, values file or kustomization.

## Instructions

### Installation

```bash
# Install Argo CD on Kubernetes (server-side apply: the ApplicationSet CRD is too large for client-side apply)
kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
# Production: pin a release instead of "stable", e.g. .../argo-cd/v3.5.5/manifests/install.yaml

# Install the CLI
brew install argocd
# Or download argocd-linux-amd64 from the GitHub release and check it against the release's cli_checksums.txt
# (sha256sum -c) before chmod +x and moving it onto your PATH.

# Initial admin password (delete the argocd-initial-admin-secret after changing it)
argocd admin initial-password -n argocd

# Login: through the API server, or straight to the cluster API with no server needed
argocd login argocd.novak-labs.dev --grpc-web
argocd login --core
```

Without an ingress, reach the UI and API with `kubectl port-forward svc/argocd-server -n argocd 8080:443` and log in to `localhost:8080` (add `--insecure` for the default self-signed certificate).

### Application Definitions

```yaml
# apps/web-app.yaml — Basic ArgoCD Application manifest
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web-app
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/myorg/web-app.git
    targetRevision: main
    path: k8s/overlays/production
  destination:
    server: https://kubernetes.default.svc
    namespace: web-app
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - PrunePropagationPolicy=foreground
      - PruneLast=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

```yaml
# apps/helm-app.yaml — Application using Helm chart source
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: monitoring
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://prometheus-community.github.io/helm-charts
    chart: kube-prometheus-stack
    targetRevision: 55.0.0
    helm:
      releaseName: monitoring
      valuesObject:
        grafana:
          enabled: true
        prometheus:
          prometheusSpec:
            retention: 30d
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

### App of Apps Pattern

```yaml
# apps/root-app.yaml — Root application that manages other apps
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root-app
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/myorg/gitops-config.git
    targetRevision: main
    path: apps
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### ApplicationSet

```yaml
# appsets/multi-cluster.yaml — Deploy to multiple clusters automatically
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: web-app-set
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            environment: production
  template:
    metadata:
      name: "web-app-{{name}}"
    spec:
      project: default
      source:
        repoURL: https://github.com/myorg/web-app.git
        targetRevision: main
        path: k8s/overlays/production
      destination:
        server: "{{server}}"
        namespace: web-app
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

### Projects

```yaml
# projects/team-a.yaml — ArgoCD project with RBAC restrictions
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
  namespace: argocd
spec:
  description: "Team A applications"
  sourceRepos:
    - "https://github.com/myorg/team-a-*"
  destinations:
    - namespace: "team-a-*"
      server: https://kubernetes.default.svc
  clusterResourceWhitelist:
    - group: ""
      kind: Namespace
  roles:
    - name: developer
      policies:
        - p, proj:team-a:developer, applications, get, team-a/*, allow
        - p, proj:team-a:developer, applications, sync, team-a/*, allow
      groups:
        - team-a-devs
```

### Sync Waves and Hooks

```yaml
# k8s/namespace.yaml — Resource with sync wave annotation
apiVersion: v1
kind: Namespace
metadata:
  name: web-app
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
---
# k8s/migration-job.yaml — Pre-sync hook for database migration
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: HookSucceeded
spec:
  template:
    spec:
      containers:
        - name: migrate
          image: registry.novak-labs.dev/web-app:2.3.1
          command: ["python", "manage.py", "migrate"]
      restartPolicy: Never
```

### Argo CD 3.x Notes

- Resource tracking defaults to **annotation** (`argocd.argoproj.io/tracking-id`) since 3.0; label tracking is opt-in through `application.resourceTrackingMethod` in `argocd-cm`.
- RBAC is fine-grained: `update` and `delete` on an application no longer cover its sub-resources, and `logs, get` must be granted explicitly (project role policies need `p, proj:team-a:developer, logs, get, team-a/*, allow` to read pod logs).
- Dex SSO RBAC subjects use the `federated_claims.user_id` claim, not `sub`.
- An automated sync policy with `selfHeal` reverts manual `kubectl` changes within minutes, including a manual rollback.

### Common Commands

```bash
# Application management
argocd app create web-app -f apps/web-app.yaml
argocd app list
argocd app get web-app
argocd app sync web-app
argocd app sync web-app --prune --dry-run   # preview; --force replaces resources, use with care
argocd app wait web-app --health --timeout 300
argocd app delete web-app --cascade -y   # --cascade (default) also deletes the app's resources

# Diff and history
argocd app diff web-app
argocd app history web-app
argocd app rollback web-app 3     # ID from `argocd app history`; turn off auto-sync first or it re-syncs

# Repositories
argocd repo add https://github.com/myorg/config.git --username git --password $GITHUB_PAT

# Clusters
argocd cluster add my-cluster-context
```

## Examples

### Example 1: Deploy a Kustomize app with auto-sync

Request: "Deploy k8s/overlays/production from github.com/myorg/web-app into the web-app namespace and keep it in sync."

```bash
argocd app create web-app -f apps/web-app.yaml
argocd app wait web-app --health --timeout 300
argocd app get web-app
```

Result: with the first Application manifest above, the namespace is created, resources are applied, and `argocd app get` shows `Sync Status: Synced` and `Health Status: Healthy`.

### Example 2: Roll back a bad release

Request: "The last deploy broke checkout, go back to the previous version."

```bash
argocd app set web-app --sync-policy none     # stop auto-sync, otherwise it re-applies Git HEAD
argocd app history web-app
argocd app rollback web-app 3
```

Result: the app runs revision 3 and shows OutOfSync against Git. Revert the commit in Git, then re-enable auto-sync (`argocd app set web-app --sync-policy automated`).

## Guidelines

- Pin the install manifest and CLI to a release version in production; `stable` moves.
- Keep application source repos separate from config repos, and pin `targetRevision` (tag or SHA) for prod.
- Use AppProjects to limit `sourceRepos`, `destinations` and cluster-scoped resources per team.
- Do not commit repository credentials; create them with `argocd repo add` from environment variables or Secrets.
- Change the initial admin password and delete `argocd-initial-admin-secret`; prefer SSO.
- Avoid `:latest` image tags: Argo CD cannot see a change behind a tag it already synced.
- `prune: true` deletes resources removed from Git; review `argocd app diff` before enabling it.
