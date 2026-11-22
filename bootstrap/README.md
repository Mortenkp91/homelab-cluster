# Bootstrap: building the cluster from an empty node

Everything in this cluster is installed by Argo CD from Git, except the four things Argo CD needs
before it can run at all. Those are the manual steps in this runbook, in this order:

| # | Manual step | Why it cannot come from Argo CD |
|---|-------------|---------------------------------|
| 1 | k3s, with flannel, kube-proxy, the network policy controller, Traefik and ServiceLB turned off | There is no cluster yet. |
| 2 | Cilium | It is the CNI. Without it the node is `NotReady` and no pod (Argo CD included) gets an IP. |
| 3 | Secret `azure-secret-sp` in namespace `external-secrets` | The one secret that is not in Azure Key Vault: it is the credential for reading the vault. |
| 4 | Argo CD, then `root-app.yaml` | Something has to install the installer. |

After step 4, Argo CD installs the rest in sync-wave order ([what happens next](#7-what-happens-next)) and
takes over Cilium and itself, so later upgrades of both are ordinary Git changes.

Run everything on `mpe-001`, from a clone of this repository. Needed on the node: `git`, `helm`,
[`yq` v4](https://github.com/mikefarah/yq), `jq`, `az`. Use Helm 4.3, the version Argo CD 3.6 renders
with (`scripts/install-tools.sh helm && export PATH="$PWD/.cache/bin:$PATH"` installs the version CI
pins); see the note at the end of step 4 for what Helm 3 costs. `kubectl` is installed by k3s. The `argocd`
CLI is optional.

Files in this directory:

- `k3s-config.yaml`: becomes `/etc/rancher/k3s/config.yaml`.
- `render-chart.sh`: prints the manifests of a Helm-chart Application, reading chart, version and values
  from the Application file in `clusters/production/infrastructure/`. Used for steps 2 and 4 so they can
  never drift from what Argo CD will apply.
- `root-app.yaml`: the app-of-apps that syncs `clusters/production/`.

## 0. Cutover from the Flux cluster: read this before merging

`main` drives the live Flux cluster, and every Argo CD Application in this repo tracks `main`. So the new
cluster can only be built after this work is merged, and the merge is the point of no return for the old
one:

- Expect Flux's `flux-system` Kustomization to fail on `clusters/production`: it now holds Argo CD
  kinds the old cluster has no CRDs for, and no Flux manifests.
- The `infra-resources`, `infra-configs` and `apps` Kustomizations already in the cluster keep
  reconciling with `prune: true`. Expect them to start removing what is gone from Git (Traefik, the
  monitoring stack, the Flux HelmReleases) while the app HTTPRoutes move to a Gateway that does not exist
  there. The old cluster ends up half dismantled. Data under `/srv/nfs` is not touched.

Safe order:

1. Do the backups in step 1.
2. Freeze the old cluster so nothing reacts to the merge. Workloads keep running:

   ```bash
   flux suspend kustomization --all -n flux-system
   flux get kustomizations -n flux-system        # SUSPENDED True on all four
   ```

   Suspending the Kustomizations is enough: the HelmReleases stay in the cluster unchanged and keep
   their pinned chart versions.

3. Merge the pull request into `main`.
4. Continue with step 2 of this runbook. The downtime starts there.

If you would rather go straight to the wipe, you can skip the suspend: uninstall k3s (step 2) first, then
merge. Do not merge and leave the old cluster running unsuspended.

Way back: revert the merge commit on `main`, reinstall k3s with only `disable: [traefik, servicelb]` in
its config, create `azure-secret-sp` again and re-run `flux bootstrap github` for
`--path=clusters/production`. The app data is the same either way.

## 1. Back up, and know what survives

`k3s-uninstall.sh` deletes `/etc/rancher/k3s`, `/var/lib/rancher/k3s` (the datastore with every
Kubernetes object, all container images, and any `local-path` volume) and `/var/lib/kubelet`.

What survives:

- **App data under `/srv/nfs`.** The uninstall script does not touch it. Every volume the apps use is a
  static `local` PersistentVolume with `persistentVolumeReclaimPolicy: Retain` pointing at a directory
  there (`infrastructure/base/local/local-volume.yaml`), and every PVC under `apps/base` selects its PV by
  name (`spec.volumeName`, `storageClassName: ""`). On the new cluster Argo CD recreates the PV objects
  (wave 1) and the PVCs (wave 7), and each PVC binds to the same directory again. Nothing is copied or
  provisioned. No workload uses `local-path` today, so nothing of value lives under `/var/lib/rancher`.
- **Secrets in Azure Key Vault**, and the Cloudflare DNS records.

What does not: Prometheus/Grafana/Loki data (the monitoring stack is removed anyway), TLS certificates
(reissued), and the `azure-secret-sp` Secret (see below).

`/srv/nfs` is on the same disk as the OS (`/`), so "survives" means "is not deleted by these steps", not
"is safe from a mistake". Copy at least the app configuration to another machine or disk first. Stop the
apps for a consistent copy of their SQLite databases (Flux must be suspended, step 0, or it scales them
back up):

```bash
for ns in media frigate homeassistant; do kubectl -n "$ns" scale deployment --all --replicas=0; done
sudo tar --zstd -cf /path/on/another/disk/nfs-configs-$(date +%F).tar.zst -C /srv/nfs configs
```

Keep the Key Vault credential, so step 4 is one command. The file contains a secret: keep it out of the
repository and delete it afterwards.

```bash
(umask 077; kubectl -n external-secrets get secret azure-secret-sp -o json \
  | jq '{apiVersion, kind, type, data, metadata: {name: .metadata.name, namespace: .metadata.namespace}}' \
  > ~/azure-secret-sp.json)
```

Pre-flight checks:

```bash
# The node name is baked into the PVs' nodeAffinity. It must stay mpe-001.
hostname

# Every directory a static PV points at must exist before the apps sync.
ls -d /srv/nfs/media /srv/nfs/nvr \
  /srv/nfs/configs/{qbittorrent,jellyfin,prowlarr,sonarr,radarr,jellyseer,frigate,homeassistant}

# The vault must hold every key the ExternalSecrets ask for.
az login
az keyvault secret list --vault-name kv-mkped-homelab --query '[].name' -o tsv
```

The vault needs `cloudfare-api-token` (sic; used by cert-manager and external-dns). The parking CronJob,
which is switched off in `clusters/production/apps.yaml`, additionally needs `license-plate`,
`phone-number`, `twilio-account-sid`, `twilio-auth-token`, `twilio-phone-number` and `ghcr-read-token`.

## 2. Remove the old cluster

```bash
/usr/local/bin/k3s-uninstall.sh
```

This stops k3s and every pod, deletes the directories listed in step 1, removes the flannel interfaces
(`cni0`, `flannel.1`, the pod veths), deletes `/var/lib/cni` and `/run/flannel`, and strips the `KUBE-*`,
`CNI-*` and flannel rules from iptables. flannel's CNI config lives under `/var/lib/rancher/k3s`, so it
goes too.

Cilium must start on a node with none of that left, otherwise old rules and routes shadow its datapath.
Check, and clean up by hand if anything shows:

```bash
ip -br link | grep -E 'cni0|flannel|veth'                  # expect no output
ip -br addr show dev enp2s0 | grep -F 10.20.22.5           # expect no output (old kube-vip VIP)
sudo ls /etc/cni/net.d /var/lib/cni /run/flannel           # expect "No such file or directory"
sudo iptables-save | grep -ciE 'KUBE-|CNI-|flannel'        # expect 0

sudo ip link delete cni0
sudo ip link delete flannel.1
sudo ip addr del 10.20.22.5/32 dev enp2s0
sudo rm -rf /etc/cni/net.d /var/lib/cni /run/flannel
sudo iptables-save  | grep -v KUBE- | grep -v CNI- | grep -iv flannel | sudo iptables-restore
sudo ip6tables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | sudo ip6tables-restore
```

Then reboot. It is the one step that guarantees no stale interface, route, address or rule is left:

```bash
sudo reboot
```

If you ever repeat this on a node that already ran Cilium: delete the Cilium interfaces **before**
`k3s-uninstall.sh` (`sudo ip link delete cilium_host`, which takes `cilium_net` with it), remove its
iptables rules (`sudo iptables-save | grep -iv cilium | sudo iptables-restore`) and
`sudo rm -rf /etc/cni/net.d`. The k3s docs warn that the node can lose connectivity otherwise.

## 3. Install k3s

The config file has to be in place before the installer starts k3s, because the CNI and kube-proxy
choices cannot be changed cleanly on a running cluster.

```bash
sudo mkdir -p /etc/rancher/k3s
sudo install -m 0644 bootstrap/k3s-config.yaml /etc/rancher/k3s/config.yaml
curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=v1.37 sh -

# The old kubeconfig has the old cluster's CA and credentials.
mkdir -p ~/.kube
sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config
chmod 600 ~/.kube/config

kubectl get nodes
```

The API server is reached on the node address, `https://10.20.22.81:6443`. The old cluster also had a
kube-vip address for it, `10.20.22.5`; that is gone with kube-vip, so a kubeconfig on another machine that
still points at `10.20.22.5` needs the new `k3s.yaml` with `127.0.0.1` replaced by `10.20.22.81`.

`mpe-001` shows `NotReady`, and CoreDNS, metrics-server and local-path-provisioner stay `Pending`. That is
correct: there is no CNI yet.

Gateway API CRDs are **not** a manual step. Cilium runs with `gatewayAPI.enabled: false` (Envoy Gateway
is the gateway) and the Argo CD chart is not asked to render any route, so nothing needs them before
Argo CD installs them itself in wave 1.

## 4. Install Cilium

```bash
bootstrap/render-chart.sh clusters/production/infrastructure/cilium.yaml \
  | kubectl apply --server-side --field-manager=argocd-controller -f -

kubectl -n kube-system rollout status daemonset/cilium --timeout=10m
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=5m
kubectl wait node/mpe-001 --for=condition=Ready --timeout=5m
kubectl -n kube-system wait --for=condition=complete job -l k8s-app=hubble-generate-certs --timeout=5m
kubectl -n kube-system rollout status deployment/hubble-relay --timeout=5m
kubectl -n kube-system get pods
```

Hubble's relay and UI start a little after the agent: they wait for the `hubble-generate-certs` Job to
create their TLS Secrets.

The node turns `Ready` and the pending system pods start. Do not continue until they are `Running`:
everything after this needs pod networking and cluster DNS.

### Why `helm template | kubectl apply`, not `helm install`

Argo CD never runs `helm install`. It renders a chart with `helm template` and applies the result with
server-side apply under the field manager `argocd-controller`. The two bootstrap installs do exactly the
same thing, so that when the `cilium` and `argocd` Applications sync for the first time they find the
objects they would have created and only add their tracking annotation. No pod restarts.

`helm install` would work, but leaves things Argo CD never cleans up:

- a `sh.helm.release.v1.*` Secret, so `helm list` shows a release that is frozen at the bootstrap version
  while Argo CD keeps changing the real objects, and an accidental `helm upgrade` would fight Argo CD;
- every field owned by a `helm` field manager. Argo CD takes ownership of what it sets, but a field that
  a later chart version drops would stay behind, still owned by `helm`. With the same manager name on
  both sides, the bootstrap apply and Argo CD's apply are one continuous history.

Hooks need no special handling. Cilium's chart renders none with these values (the Hubble certificate
Job is an ordinary Job, and the Secrets it creates are not in the chart, so Argo CD leaves them alone). The Argo CD chart has one
(`argocd-redis-secret-init`, a `pre-install` Job that generates the Redis password): applied as a plain
Job it does the same thing, and Argo CD later runs it as a PreSync hook, replacing the leftover objects.

`render-chart.sh` asks the cluster for its version and API groups and passes them to Helm, as Argo CD
does. Argo CD 3.6 renders with its bundled Helm 4.3. Helm 3.18 renders the Cilium chart slightly
differently (three empty ConfigMap keys and one empty agent argument), which changes the config checksum
on the Cilium DaemonSet and operator. Bootstrapping with Helm 3 therefore works, but costs one extra
Cilium agent and operator restart when Argo CD adopts them in wave 0.

## 5. Create the bootstrap secret

External Secrets (wave 2) reads Azure Key Vault through the `ClusterSecretStore` `azure-kv-store` (wave
3), which authenticates with the service principal in this Secret. It has to exist **before** the root app
is applied: created late, the store controller has already backed off (up to about 16 minutes) and it
does not watch Secrets.

The namespace is created here by hand. The `external-secrets` Application has `CreateNamespace=true` and
simply reuses it, and Argo CD does not prune the Secret because it never created it.

```bash
kubectl create namespace external-secrets
```

Either restore the Secret saved in step 1:

```bash
kubectl apply -f ~/azure-secret-sp.json && rm ~/azure-secret-sp.json
```

or mint a new client secret for the existing service principal (nothing is written to disk or shell
history):

```bash
az login
APP_ID=$(az ad app list --display-name external-secret-operator --query '[0].appId' -o tsv)
kubectl -n external-secrets create secret generic azure-secret-sp \
  --from-literal=ClientID="$APP_ID" \
  --from-literal=ClientSecret="$(az ad app credential reset --id "$APP_ID" --append \
      --display-name "homelab $(date +%F)" --years 1 --query password -o tsv)"
```

Creating the vault and the service principal from nothing is in the [appendix](#appendix-key-vault-and-service-principal-from-scratch).

## 6. Install Argo CD and hand over

Same chart version and values file as the `argocd` Application
(`clusters/production/infrastructure/argocd.yaml`, `infrastructure/base/argocd/values.yaml`):

```bash
kubectl create namespace argocd
bootstrap/render-chart.sh clusters/production/infrastructure/argocd.yaml \
  | kubectl apply --server-side --field-manager=argocd-controller -f -

kubectl -n argocd wait --for=create secret/argocd-redis --timeout=5m
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=5m
kubectl -n argocd wait --for=condition=Available deployment --all --timeout=5m
```

For a minute the pods sit in `CreateContainerConfigError`: they wait for the Redis password Secret
`argocd-redis`, which the `argocd-redis-secret-init` Job creates (the Job removes itself 60 seconds after
it finishes).

Then give Argo CD the repository. This is the last manual `kubectl apply`:

```bash
kubectl apply -f bootstrap/root-app.yaml
```

## 7. What happens next

The `root` Application applies everything under `clusters/production/`: two AppProjects, one Application
per infrastructure component, and the `apps` ApplicationSet. They carry sync waves, and `root` does not
start a wave until every Application in the previous one is `Healthy` (Argo CD only waits for child
Applications because of the health check configured in `infrastructure/base/argocd/values.yaml`).

This table is the answer to "what must be in place before the applications deploy":

| Wave | Application | Healthy when | Needed by |
|------|-------------|--------------|-----------|
| -1 | AppProjects `infrastructure`, `apps` | created | every Application |
| 0 | `cilium` | agent, operator and Hubble running (already true: adopted from step 4) | everything |
| 1 | `gateway-api` | Gateway API CRDs established | Envoy Gateway, every HTTPRoute |
| 1 | `cilium-config` | LoadBalancer pool 10.20.22.10-20 and the L2 (ARP) announcement policy created | the Gateway's LoadBalancer IP |
| 1 | `local` | static PVs created | every app PVC |
| 2 | `argocd` | Argo CD adopts itself (already true: step 6) | |
| 2 | `external-secrets` | operator, webhook and CRDs ready | the ClusterSecretStore |
| 2 | `cert-manager` | controller, webhook and CRDs ready | ClusterIssuer, Certificate |
| 2 | `envoy-gateway` | controller and its CRDs ready | GatewayClass, Gateway |
| 3 | `external-secrets-config` | `ClusterSecretStore/azure-kv-store` Ready: Key Vault reachable with `azure-secret-sp` | every ExternalSecret |
| 4 | `cert-manager-config` | Cloudflare token synced from the vault, `ClusterIssuer/cloudfare-clusterissuer` Ready | the wildcard Certificate |
| 4 | `external-dns-config` | Cloudflare token synced from the vault | external-dns |
| 5 | `envoy-gateway-config` | `Gateway/homelab-gateway` Programmed with 10.20.22.11, wildcard Certificate for `*.mkped.com` issued (DNS-01, a few minutes) | every HTTPRoute |
| 5 | `external-dns` | Deployment ready | DNS records for the routes |
| 6 | `argocd-config` | `HTTPRoute/argocd-route` accepted: UI at https://argocd.mkped.com | |
| 6 | `hubble-config` | `HTTPRoute/hubble-route` accepted: Hubble UI at https://hubble.mkped.com | |
| 7 | ApplicationSet `apps` | `media`, `frigate`, `homeassistant` generated; they then sync on their own | |

Watch it:

```bash
kubectl -n argocd get applications.argoproj.io --watch
```

Or use the UI before the Gateway exists (plain HTTP, because TLS is terminated at the Gateway):

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80    # http://localhost:8080
```

A full build takes roughly 10 to 20 minutes, most of it image pulls and the ACME DNS-01 challenge.

If it stalls:

- A wave waits while something is `Progressing`. `kubectl -n argocd get applications.argoproj.io` shows
  which Application; its own resources show why (`kubectl -n argocd describe application <name>`, or the
  UI).
- A `Degraded` child fails the root sync. `root` retries forever with a back-off of up to 5 minutes, so
  it recovers by itself once the cause is fixed. To retry immediately:

  ```bash
  argocd app sync root
  # without the CLI:
  kubectl -n argocd patch application root --type merge \
    -p '{"operation":{"initiatedBy":{"username":"kubectl"},"sync":{"prune":true,"syncOptions":["ServerSideApply=true"]}}}'
  ```

- `azure-kv-store` not Ready after fixing `azure-secret-sp`: the controller is in back-off. Kick it:

  ```bash
  kubectl annotate clustersecretstore azure-kv-store retry=$(date +%s) --overwrite
  ```

- A PVC stays `Pending` with its PV `Released`: the PV still remembers a claim that was deleted. This
  cannot happen on a fresh cluster, only after a namespace or PVC was deleted on a running one:

  ```bash
  kubectl patch pv <pv-name> --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'
  ```

- The wildcard Certificate is refused by Let's Encrypt: there is a limit of 5 identical certificates per
  week. It only matters if the cluster is rebuilt several times in a row.

## 8. First login

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

Log in as `admin` at https://argocd.mkped.com, change the password under *User Info*, then delete the
initial secret (it is only a record of the generated password):

```bash
kubectl -n argocd delete secret argocd-initial-admin-secret
```

The CLI needs `--grpc-web`: the UI is exposed with a plain HTTPRoute, which carries gRPC-Web but not
native gRPC. The flag is remembered after login.

```bash
argocd login argocd.mkped.com --grpc-web --username admin
argocd account update-password
```

## 9. Post-checks

```bash
kubectl -n argocd get applications.argoproj.io        # all Synced and Healthy
kubectl get nodes -o wide                             # mpe-001 Ready
kubectl -n kube-system exec ds/cilium -- cilium-dbg status --brief
kubectl -n kube-system exec ds/cilium -- cilium-dbg status | grep -E 'KubeProxyReplacement|Hubble'
kubectl get ciliumloadbalancerippool                  # homelab: DISABLED false, CONFLICTING false, 9 IPs available
kubectl -n kube-system get lease | grep l2announce    # one lease per LoadBalancer Service, held by mpe-001
kubectl get clustersecretstore,clusterissuer          # both Ready
kubectl get externalsecret -A                         # all SecretSynced
kubectl get certificate -A                            # wildcard certificate READY True
kubectl -n envoy-gateway-system get gateway homelab-gateway   # PROGRAMMED True, ADDRESS 10.20.22.11
kubectl get svc -A | grep LoadBalancer                # Envoy holds 10.20.22.11, qbittorrent-udp-svc 10.20.22.12
kubectl get httproute -A
kubectl get pv,pvc -A                                 # every PV and PVC Bound

dig +short argocd.mkped.com jellyfin.mkped.com homeassistant.mkped.com   # 10.20.22.11
for h in argocd hubble jellyfin jellyseer sonarr radarr prowlarr qbittorrent frigate homeassistant; do
  printf '%-14s %s\n' "$h" "$(curl -s -o /dev/null -w '%{http_code}' "https://$h.mkped.com")"
done
```

The Gateway must have 10.20.22.11: external-dns points every record at that address. If a LoadBalancer
Service has an address but nothing on the LAN can reach it, the ARP announcement is the place to look:
`ip neigh | grep 10.20.22.11` on another machine should show the MAC address of `enp2s0` on `mpe-001`, and
the interface name in `infrastructure/configs/cilium/l2-announcement-policy.yaml` must match the node's.

Two checks that need a browser, because they are the behaviour that differs most from Traefik: play a
Jellyfin stream (or open a Frigate live view) for longer than 15 seconds, and open a Home Assistant
dashboard. Envoy's default 15 second request timeout is switched off for the whole Gateway in
`infrastructure/configs/envoy-gateway/backend-traffic-policy.yaml`; a stream that dies at 15 seconds means
that policy is not attached (`kubectl -n envoy-gateway-system get backendtrafficpolicy`).

Clean up by hand: external-dns runs `upsert-only` and never deletes records, so `prometheus.mkped.com`
and `grafana.mkped.com` (and their TXT ownership records) stay in Cloudflare until you remove them.

## Living with it

- **Upgrades** are Git changes. Renovate bumps `targetRevision` in
  `clusters/production/infrastructure/*.yaml`; Argo CD upgrades Cilium and itself like anything else.
  The commands above read those same files, so this runbook does not go stale.
- **A Cilium change that breaks pod networking** takes Argo CD down with it, so Argo CD cannot roll it
  back. Check out a good commit and repeat [step 4](#4-install-cilium) by hand. This is the one upgrade
  worth reading the rendered diff for before merging.
- **`root-app.yaml`** is not under `clusters/production`, so Argo CD does not manage it. After editing
  it, run `kubectl apply -f bootstrap/root-app.yaml` again.
- **Removing an infrastructure Application file** only makes Argo CD forget the component. Its
  resources stay (no finalizer, on purpose: a deleted file must not tear down the CNI or CRDs). Delete
  them by hand.
- **Removing an app directory** under `apps/production` behaves the same way
  (`preserveResourcesOnDeletion` in `clusters/production/apps.yaml`). Delete the namespace by hand when
  you mean it. Removing a single manifest inside an app that still exists is pruned normally.
- **Retiring a static PV**: the `local` Application never prunes and the PVs carry
  `Prune=false,Delete=false`. Remove the PV from Git, then `kubectl delete pv <name>`. The directory
  under `/srv/nfs` is yours to delete.
- **The parking CronJob** (`apps/production/automatic-parking-registration`) is excluded in
  `clusters/production/apps.yaml`, because it was not deployed under Flux either. Remove the exclude to
  run it.

## Appendix: Key Vault and service principal from scratch

Only needed if the vault `kv-mkped-homelab` or the app registration `external-secret-operator` no longer
exist. The vault URL and tenant ID are in
`infrastructure/configs/external-secrets-operator/azkv-secret-store.yaml`.

```bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
az login

RESOURCE_GROUP=rg-homelab
VAULT_NAME=kv-mkped-homelab

az keyvault create --name "$VAULT_NAME" --resource-group "$RESOURCE_GROUP" --enable-rbac-authorization true
VAULT_ID=$(az keyvault show --name "$VAULT_NAME" --query id -o tsv)

# Let yourself write secrets (the vault uses Azure RBAC, so being its creator is not enough).
az role assignment create --role "Key Vault Secrets Officer" --scope "$VAULT_ID" \
  --assignee-object-id "$(az ad signed-in-user show --query id -o tsv)" --assignee-principal-type User

# One per key listed in step 1. `read -s` keeps the value out of the shell history.
read -rs VALUE && az keyvault secret set --vault-name "$VAULT_NAME" --name cloudfare-api-token --value "$VALUE"; unset VALUE

# App registration and service principal for External Secrets, with read access to secrets only.
APP_ID=$(az ad app create --display-name external-secret-operator --query appId -o tsv)
SP_OBJECT_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
az role assignment create --role "Key Vault Secrets User" --scope "$VAULT_ID" \
  --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal
```

Then create `azure-secret-sp` with the second variant in [step 5](#5-create-the-bootstrap-secret). If the
tenant or vault name changed, update `azkv-secret-store.yaml` as well.
