# k8s homelab

<div align="center">

<img src="https://raw.githubusercontent.com/Mortenkp91/homelab-cluster/main/docs/assets/k8s-homelab-logo.png" align="center" width="256px" height="256px"/>

<!-- markdownlint-disable no-trailing-punctuation -->

### My k8s based homelab cluster

</div>
</div>

# Overview

This repo contains all the configurations and documentation needed to bootstrap and operate my own homelab.

The purpose is to strengthen my skills within the Kubernetes and Cloud Native landscape and to use it as a showcase for interviews. I've been working withing the software engineering industry as a backend engineer for the last 10 years and within the recent years, I've become fascinated of the evolving landscape of cloud native technologies and want to shift my career to focus solely on this. By having a proven track record of working with backend engineering and moving on to platform engineering, I'm hoping to create a even stronger foundation going forward

# Cluster Architecture

I've started out simple with a small cluster consisting of 3 nodes, 1 master and two worker nodes. I'm using `k3s` as my Kubernetes distribution along with FluxCD as my GitOps toolkit. The bootstrapping process is currently done manually by installing Ubuntu Server along side with k3s with master/worker node configuration. I'm planning on getting upgrading to a more beefy Mini PC to host all of the nodes using Proxmox with Talos Linux OS provisioned through Terraform for a more streamlined setup.

The cluster is completely private a not exposed to the public internet. This is by design since I don't have a need for exposing my homelab. It opens up for a completely different set of security problems I would have to deal with. In case I need to connect to my cluster when not at home, my Ubiquiti Dream Router hosts a WireGuard VPN Gateway.


# Hardware

My homelab consists of a couple of machines, some Ubiquiti devices and AXIS Surveilance Cameras

| Device                                             | Information |
| -------------------------------------------------- | ----------- |
| Intel NUC NUC12WSKI50Z                             | Functions as the master node. Intel i5 1240P, 16 GB RAM, 1 TB SSD. Ubuntu Desktop 24.04. Also runs a NFS server for shared storage across the cluster. Some apps are being forced to run on this server to utilize Intel QuickSync (Jellyfin/Frigate). I know it's not best practice... |
| Lenovo ThinkCentre M910q Tiny                      | 2x. of these as worker nodes. Refurbished hardware I got for a spot price. Intel i7 processor, 8 GB RAM, 256 GB SSD. Ubuntu Server 24.04 |
| Ubiquiti Dream Router                              | Network Router. Configured with VLAN segregation |
| AXIS M3115-LVE                                     | 4x of these |                                          


## Infrastructure
Here's all the components I used to run my homelab

| Name | Description |
|------|-------------|
| **[Flux CD](https://fluxcd.io/)** | My GitOps solution of choice. The core functionality that allows for simple and iterative changes to my cluster. Using GitOps, I've my desired state stored in Git and I dont need to think about backing up the current state |
| **[Cert Manager](https://cert-manager.io/)** | X.509 certificate management for Kubernetes. I'm using Let's Encrypted as a provider for getting free TLS certs on my apps. |
| **[Cloudflare](https://www.cloudflare.com/)** | DNS Management. Planning on getting ``external-dns`` installed as well to automatically sync my Ingress DNS records to Cloudfare. |
| **[External Secrets Operator](https://external-secrets.io/)** | Syncs my secrets stored in Azure Key Vault to my cluster. |
| **[Traefik](https://traefik.io/)** | My Ingress Controller of choice. Natively supports Ingress and the new Gateway API |
| **[Grafana](https://grafana.com/)** | The best in class open source observability platform. Why use something else? |
| **[Prometheus](https://prometheus.io/)** | Open sourced pull based monitoring system |
| **[Renovate](https://www.mend.io/renovate/)** | Automated dependency updates. Currently installed a GitHub Bot in my repository. I could host it my self, but the freely managed GitHub Bot works just fine! |
| **[kubevip and kubevip-cloud-controller](https://kube-vip.io//)** | LoadBalancer IP assigment for bare metal setups. Whenever a new type of ``Service:LoadBalancer`` appears it assigns an IP address from a pre-configured IP range. This is configured in the VLAN. I'm using a small range of 10.20.22.10 -> 10.20.22.20 since I basically only need one LoadBalancer for the Traefik Ingress Controller |


## Applications
Here's a list of different application that I run my homelab

| Name | Description |
|------|-------------|
| **[Frigate NVR](https://frigate.video/)** | Open Source NVR software that can use the GPU to perform object and motion detection. Much better than the built-in detection mechanism in my AXIS cameras (They are pretty old) |
| **[QBittorrent](https://www.qbittorrent.org/)** | Downloading of cat videos. Legitimate, ofc! |
| **[Sonarr](https://sonarr.tv/)** | Getting all the latest cat TV shows. Legitimate, ofc! |
| **[Radarr](https://radarr.video/)** | Getting all the latest cat movies. Legitimate, ofc! |
| **[Prowlarr](https://traefik.io/)** | Index manager for cat stuff... |
| **[Jellyfin](https://jellyfin.org/)** | Media Streaming Server. Better than Plex IMO |
| **[Jellyseer](https://seerr.dev)** | A nice GUI for requesting new cat content with automatic integration to Radarr, Sonarr and Prowlarr |

