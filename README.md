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

I've started out simple with a small cluster consisting of 3 nodes, 1 master and two worker nodes. I'm using `k3s` as my Kubernetes distribution along with FluxCD as my GitOps toolkit.


# Hardware

My homelab consists of a couple of machines, some Ubiquiti devices and AXIS Surveilance Cameras

| Device                                             | Count | OS Disk Size     | RAM   | Operating System          |
| -------------------------------------------------- | ----- | ------------     | ----  | ------------------------- |
| Intel NUC NUC12WSKI50Z                             | 1     | 1TB              | 16GB  | Ubuntu Desktop 24.04      |
| Lenovo ThinkCentre M910q Tiny                             | 2     | 256 GB           | 8GB   | Ubuntu Server 24.04       |


