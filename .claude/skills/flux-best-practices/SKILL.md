---
name: flux-best-practices
description: Review Flux CD and Kubernetes configurations for best practices. Use when creating or modifying HelmReleases, Kustomizations, or other Flux resources.
allowed-tools: Read, Grep, Glob
argument-hint: "[file-or-component]"
---

# Flux CD and Kubernetes Best Practices

Review the specified file or component ($ARGUMENTS) against these best practices.

## Flux CD Kustomization Best Practices

### Dependencies and Ordering
- Use `dependsOn` to ensure proper reconciliation order (CRDs before resources that use them)
- Infrastructure should deploy before applications
- SecretStores/ClusterSecretStores before ExternalSecrets

### Health Checks and Timeouts
- Set appropriate `timeout` values (default 5m may be too short for large Helm charts)
- Use `retryInterval` for transient failures (recommend 1m-5m)
- Enable `wait: true` for critical dependencies

### Pruning and Drift Detection
- Enable `prune: true` to remove orphaned resources
- Consider `driftDetection.mode: enabled` for production workloads
- Use `driftDetection.ignore` for fields modified by controllers (e.g., prometheus-operator-validated annotations)

### Source References
- Always specify `interval` on both Kustomization and source
- Use semantic versioning constraints for HelmRepository charts

## HelmRelease Best Practices

### CRD Management
```yaml
install:
  crds: Create
upgrade:
  crds: CreateReplace
```
- Use `CreateReplace` for upgrades to handle CRD changes
- Consider separate CRD-only HelmRelease for large CRD sets

### Values Management
- Prefer `valuesFrom` with ConfigMaps over inline `values` for large configs
- Use ExternalSecrets for sensitive Helm values
- Keep inline `values` minimal and environment-specific

### Rollback and Recovery
```yaml
install:
  remediation:
    retries: 3
upgrade:
  remediation:
    retries: 3
    remediateLastFailure: true
```

## Kustomize Overlay Pattern

### Directory Structure
```
component/
  base/
    kustomization.yaml
    chart.yaml (HelmRelease)
    namespace.yaml
    repository.yaml
  production/
    kustomization.yaml  # patches/overrides only
```

### Overlay Best Practices
- Base should be deployable standalone for testing
- Production overlays should only contain environment-specific patches
- Use `patchesStrategicMerge` or `patchesJson6902` instead of duplicating resources

## Secrets Management

### ExternalSecrets Pattern
- Define SecretStore/ClusterSecretStore in `infrastructure/configs/`
- Reference secrets by name, not by copying values
- Set appropriate `refreshInterval` (15m-1h for most cases)

### Never Commit
- Actual secret values
- kubeconfig files
- Service account keys
- .tfvars with sensitive data

## Resource Organization

### Namespace Isolation
- Each application should have its own namespace
- Infrastructure components can share namespaces by function (monitoring, cert-manager)
- Use ResourceQuotas and LimitRanges in production

### Labels and Annotations
Required labels for all resources:
```yaml
metadata:
  labels:
    app.kubernetes.io/name: <app-name>
    app.kubernetes.io/instance: <instance>
    app.kubernetes.io/component: <component>
```

## Common Anti-Patterns to Flag

1. **Missing health checks** - Kustomizations without `healthChecks` for critical resources
2. **Hardcoded secrets** - Any Secret resource with actual values in `data` or `stringData`
3. **No resource limits** - Deployments without `resources.limits`
4. **Latest tags** - Container images using `:latest` instead of pinned versions
5. **Missing namespace** - Resources without explicit namespace (relying on default)
6. **Circular dependencies** - `dependsOn` chains that could deadlock
7. **Over-permissive RBAC** - ClusterRoleBindings when namespace-scoped would suffice
