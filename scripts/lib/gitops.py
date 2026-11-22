#!/usr/bin/env python3
"""Helpers behind scripts/render.sh and scripts/validate.sh.

Everything here works on files only: no cluster access, no network.

  inventory    Read the Argo CD resources and work out what Argo CD will sync
               (kustomize/plain dirs and Helm charts), plus every consistency
               problem a CRD schema cannot express. Prints JSON.
  crd-schemas  Turn the CustomResourceDefinitions found in rendered manifests
               into JSON schemas kubeconform can load.
  policy       Repo rules over the source tree and the rendered manifests:
               Flux leftovers, routes without a Gateway, plain Secrets, the
               same resource owned by two Applications, stale vendored bundles.
  diff         Per-resource diff of two render directories, as a patch file and
               a size-capped Markdown PR comment.
"""
import argparse
import difflib
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

import yaml

try:
    from yaml import CSafeLoader as Loader
except ImportError:  # PyYAML without libyaml: slower, same result
    from yaml import SafeLoader as Loader

# --- repo conventions (see CLAUDE.md) ---------------------------------------
# Where Argo CD resources live. Applications below SYNCED_TREE are created by
# the root app-of-apps, so they need a sync wave and a declared AppProject.
ARGO_SCAN = ("clusters", "bootstrap")
SYNCED_TREE = "clusters/"
# Every directory matching these must be the source of some Application.
SWEEP = ("infrastructure/production/*", "infrastructure/configs/*", "apps/production/*")
# Every Helm values file matching these must be used by some Application.
VALUES_SWEEP = ("infrastructure/base/*/values.yaml",)

PINNED_VERSION = re.compile(r"^v?\d+(\.\d+){1,2}([-+][0-9A-Za-z.+-]+)?$")
APPSET_VAR = re.compile(r"\{\{\s*\.?path(?:\.(\w+))?\s*\}\}")
GATEWAY_GROUP = "gateway.networking.k8s.io"


def load_docs(path):
    """All non-empty YAML documents in a file."""
    with open(path, encoding="utf-8") as fh:
        return [d for d in yaml.load_all(fh, Loader=Loader) if d is not None]


def group_of(doc):
    api = str(doc.get("apiVersion", ""))
    return api.rsplit("/", 1)[0] if "/" in api else ""


def norm_repo(url):
    """Comparable form of a git URL: host/owner/repo, lower case, no .git."""
    url = re.sub(r"^(?:\w+://)?(?:[^@/]+@)?", "", str(url).strip())
    url = re.sub(r"^([^:/]+):(?!\d+/)", r"\1/", url)  # scp-style git@host:owner/repo
    return re.sub(r"\.git$", "", url).rstrip("/").lower()


# --- inventory ---------------------------------------------------------------


class Inventory:
    def __init__(self, root, git_revision, repo_url):
        self.root = root
        self.git_revision = git_revision
        self.repo_url = repo_url
        self.errors = []
        self.warnings = []
        self.argo_files = []
        self.applications = []  # every Application name, declared or generated
        self.projects = {}  # name -> spec
        self.helm = []
        self.dirs = {}  # path -> target
        self.excluded = {}  # path -> ApplicationSet that explicitly excludes it
        self.git_urls = {}  # normalised url -> first file that used it

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def exists(self, rel, want_dir=False):
        full = os.path.join(self.root, rel)
        inside = os.path.realpath(full).startswith(os.path.realpath(self.root) + os.sep)
        return inside and (os.path.isdir(full) if want_dir else os.path.isfile(full))

    def add_dir(self, path, ref, namespace=None, recurse=False, project=None):
        path = os.path.normpath(path)
        target = self.dirs.setdefault(
            path, {"path": path, "refs": [], "namespace": namespace, "recurse": recurse, "project": project}
        )
        if ref:
            target["refs"].append(ref)

    def scan(self):
        resources = []
        for top in ARGO_SCAN:
            for dirpath, dirnames, filenames in os.walk(os.path.join(self.root, top)):
                dirnames.sort()
                for name in sorted(filenames):
                    if not name.endswith((".yaml", ".yml")):
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                    try:
                        docs = load_docs(os.path.join(self.root, rel))
                    except yaml.YAMLError as exc:
                        self.err(rel, f"YAML does not parse: {str(exc).splitlines()[0]}")
                        continue
                    argo = [d for d in docs if isinstance(d, dict) and group_of(d) == "argoproj.io"]
                    if argo:
                        self.argo_files.append(rel)
                    resources += [(rel, d) for d in argo]

        for rel, doc in resources:
            if doc.get("kind") == "AppProject":
                self.projects[(doc.get("metadata") or {}).get("name")] = doc.get("spec") or {}
        for rel, doc in resources:
            if doc.get("kind") == "Application":
                self.application(rel, doc)
            elif doc.get("kind") == "ApplicationSet":
                self.applicationset(rel, doc)

        self.cross_checks()
        return self

    def common(self, rel, doc, spec, label):
        """Rules shared by Applications and ApplicationSet templates."""
        meta = doc.get("metadata") or {}
        synced = rel.startswith(SYNCED_TREE)
        if synced and "argocd.argoproj.io/sync-wave" not in (meta.get("annotations") or {}):
            self.err(rel, f"{label} has no argocd.argoproj.io/sync-wave annotation")
        project = spec.get("project")
        allowed = set(self.projects) if synced else set(self.projects) | {"default"}
        if project not in allowed:
            self.err(rel, f"{label} uses project '{project}', which is not an AppProject in this repo "
                          f"(declared: {', '.join(sorted(p for p in self.projects if p)) or 'none'})")

    def git_source(self, rel, label, src):
        self.git_urls.setdefault(norm_repo(src.get("repoURL", "")), rel)
        rev = src.get("targetRevision")
        if rev not in (self.git_revision, "HEAD"):
            self.err(rel, f"{label} tracks git revision '{rev}', expected '{self.git_revision}' "
                          "(a leftover feature-branch pin would stop Argo CD following main)")

    def application(self, rel, doc):
        name = (doc.get("metadata") or {}).get("name")
        label = f"Application '{name}'"
        spec = doc.get("spec") or {}
        self.applications.append({"name": name, "file": rel})
        self.common(rel, doc, spec, label)
        sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
        if not sources:
            self.err(rel, f"{label} has neither spec.source nor spec.sources")
        namespace = (spec.get("destination") or {}).get("namespace")
        refs = {s["ref"]: i for i, s in enumerate(sources) if s.get("ref")}

        def is_chart(src):
            return bool(src.get("chart")) or str(src.get("repoURL", "")).startswith("oci://")

        # Renovate and bootstrap/render-chart.sh read the chart from sources[0]. An
        # Application may carry further charts after the values ref (e.g. a CRD chart).
        charts = [i for i, src in enumerate(sources) if is_chart(src)]
        if charts and charts[0] != 0:
            self.err(rel, f"{label}: the chart source must be sources[0], found it at sources[{charts[0]}]")

        for i, src in enumerate(sources):
            if is_chart(src):
                self.helm_source(rel, label, name, namespace, src, refs, primary=(i == charts[0]))
                self.helm[-1]["project"] = spec.get("project")
            elif src.get("path"):
                self.git_source(rel, label, src)
                if not self.exists(src["path"], want_dir=True):
                    self.err(rel, f"{label}: source path '{src['path']}' does not exist")
                    continue
                recurse = bool((src.get("directory") or {}).get("recurse"))
                self.add_dir(src["path"], f"Application/{name}", namespace, recurse, spec.get("project"))
            elif src.get("ref"):
                self.git_source(rel, label, src)
                if charts and src["ref"] == "values" and i != 1:
                    self.err(rel, f"{label}: the 'ref: values' source must be sources[1], found it at sources[{i}]")
            else:
                self.err(rel, f"{label}: sources[{i}] has no chart, path or ref")

    def helm_source(self, rel, label, name, namespace, src, refs, primary=True):
        repo = str(src.get("repoURL", "")).rstrip("/")
        chart = src.get("chart")
        version = str(src.get("targetRevision", ""))
        helm = src.get("helm") or {}
        if not PINNED_VERSION.match(version):
            self.err(rel, f"{label}: chart targetRevision '{version}' is not an exact version")
        for key in ("values", "valuesObject", "parameters", "fileParameters"):
            if helm.get(key):
                self.err(rel, f"{label}: inline helm.{key} is not allowed, keep every value in the values file "
                              "(CI and the bootstrap runbook render with 'helm template -f')")
        value_files = []
        for entry in helm.get("valueFiles") or []:
            match = re.match(r"^\$([^/]+)/(.+)$", str(entry))
            if not match:
                self.err(rel, f"{label}: valueFiles entry '{entry}' must look like $values/<path in this repo>")
            elif match[1] not in refs:
                self.err(rel, f"{label}: valueFiles entry '{entry}' uses ref '{match[1]}', "
                              "but no source declares that ref")
            elif not self.exists(match[2]):
                self.err(rel, f"{label}: values file '{match[2]}' does not exist")
            else:
                value_files.append(match[2])
        # OCI charts are written three ways in Argo CD; all pull as oci://<registry>/<chart>.
        if repo.startswith("oci://"):
            pull = f"{repo}/{chart}" if chart else repo
        elif "://" not in repo:
            pull = f"oci://{repo}/{chart}"
        else:
            pull = None
        chart_name = chart or repo.rsplit("/", 1)[-1]
        self.helm.append({
            # Render target: helm/<application>, or helm/<application>--<chart> for extra charts.
            "target": name if primary else f"{name}--{chart_name}",
            "app": name,
            "file": rel,
            "repoURL": repo,
            "chart": chart_name,
            "version": version,
            "ociRef": pull,
            "releaseName": helm.get("releaseName") or name,
            "namespace": namespace or "default",
            "skipCrds": bool(helm.get("skipCrds")),
            "valueFiles": value_files,
        })

    def applicationset(self, rel, doc):
        name = (doc.get("metadata") or {}).get("name")
        label = f"ApplicationSet '{name}'"
        spec = doc.get("spec") or {}
        template = spec.get("template") or {}
        tspec = template.get("spec") or {}
        self.common(rel, doc, tspec, label)
        src = tspec.get("source") or {}
        if not src.get("path"):
            self.err(rel, f"{label}: only templates with spec.source.path are understood by scripts/lib/gitops.py")
            return
        self.git_source(rel, label, src)

        def expand(pattern):
            # Same semantics as the git directory generator: '*' stays inside one
            # path segment and dot-directories never match.
            hits = glob.glob(os.path.join(self.root, pattern))
            return {os.path.relpath(h, self.root) for h in hits if os.path.isdir(h)}

        for gen in spec.get("generators") or []:
            directories = (gen.get("git") or {}).get("directories")
            if not directories:
                self.err(rel, f"{label}: generator {sorted(gen)} is not a git directory generator; "
                              "teach scripts/lib/gitops.py about it so its output gets validated")
                continue
            self.git_urls.setdefault(norm_repo(gen["git"].get("repoURL", "")), rel)
            if gen["git"].get("revision") not in (self.git_revision, "HEAD"):
                self.err(rel, f"{label}: generator tracks git revision '{gen['git'].get('revision')}', "
                              f"expected '{self.git_revision}'")
            included, excluded = set(), set()
            for entry in directories:
                (excluded if entry.get("exclude") else included).update(expand(entry.get("path", "")))
            matched = sorted(included - excluded)
            for directory in included & excluded:
                self.excluded[directory] = name
            if not matched:
                self.err(rel, f"{label}: the git directory generator matches no directory")
            for directory in matched:
                def fill(text, directory=directory):
                    base = os.path.basename(directory)
                    values = {None: directory, "path": directory, "basename": base,
                              "basenameNormalized": re.sub(r"[^a-z0-9-]", "-", base.lower())}
                    return APPSET_VAR.sub(lambda m: values.get(m[1], m[0]), str(text))

                path = fill(src["path"])
                app_name = fill((template.get("metadata") or {}).get("name", ""))
                namespace = fill((tspec.get("destination") or {}).get("namespace", ""))
                if "{{" in path:
                    self.err(rel, f"{label}: cannot resolve template path '{src['path']}'")
                    continue
                if not self.exists(path, want_dir=True):
                    self.err(rel, f"{label}: generated source path '{path}' does not exist")
                    continue
                if "{{" not in app_name:
                    self.applications.append({"name": app_name, "file": f"{rel} (generated for {directory})"})
                self.add_dir(path, f"ApplicationSet/{name}", namespace if "{{" not in namespace else None,
                             project=tspec.get("project"))

    def cross_checks(self):
        seen = {}
        for app in self.applications:
            if app["name"] in seen:
                self.err(app["file"], f"Application name '{app['name']}' is already used in {seen[app['name']]}")
            seen.setdefault(app["name"], app["file"])

        urls = dict(self.git_urls)
        expected = norm_repo(self.repo_url) if self.repo_url else None
        if expected:
            for url, rel in sorted(urls.items()):
                if url != expected:
                    self.err(rel, f"git repoURL '{url}' is not this repository ({expected})")
        elif len(urls) > 1:
            listing = ", ".join(f"{u} ({f})" for u, f in sorted(urls.items()))
            self.err("clusters", f"Applications point at more than one git repository: {listing}")

        # Anything in the conventional locations that no Application covers.
        covered = list(self.dirs)
        for pattern in SWEEP:
            for hit in sorted(glob.glob(os.path.join(self.root, pattern))):
                rel = os.path.relpath(hit, self.root)
                if not os.path.isdir(hit) or any(rel == c or rel.startswith(c + os.sep) for c in covered):
                    continue
                if rel in self.excluded:
                    # A written-down decision, not an oversight: keep validating it, do not fail.
                    self.warnings.append(f"{rel}: excluded from ApplicationSet '{self.excluded[rel]}', "
                                         "not deployed (still rendered and validated)")
                else:
                    self.err(rel, "directory is not the source of any Application or ApplicationSet, "
                                  "so Argo CD will never sync it")
                self.add_dir(rel, None)
        used = {f for h in self.helm for f in h["valueFiles"]}
        for pattern in VALUES_SWEEP:
            for hit in sorted(glob.glob(os.path.join(self.root, pattern))):
                rel = os.path.relpath(hit, self.root)
                if rel not in used:
                    self.err(rel, "values file is not referenced by any Application")

    def kustomize_build_options(self):
        """Argo CD passes configs.cm 'kustomize.buildOptions' to every build; do the same."""
        for chart in self.helm:
            for rel in chart["valueFiles"]:
                try:
                    values = load_docs(os.path.join(self.root, rel))
                except (OSError, yaml.YAMLError):
                    continue
                for doc in values:
                    cm = ((doc.get("configs") or {}).get("cm") or {}) if isinstance(doc, dict) else {}
                    if cm.get("kustomize.buildOptions"):
                        return str(cm["kustomize.buildOptions"])
        return ""

    def as_dict(self):
        return {
            "argoFiles": sorted(self.argo_files),
            "applications": self.applications,
            "projects": {name: spec for name, spec in sorted(self.projects.items()) if name},
            "helm": sorted(self.helm, key=lambda h: h["target"] or ""),
            "dirs": [self.dirs[k] for k in sorted(self.dirs)],
            "kustomizeBuildOptions": self.kustomize_build_options(),
            "errors": self.errors,
            "warnings": self.warnings,
        }


def cmd_inventory(args):
    inv = Inventory(os.path.abspath(args.root), args.git_revision, args.repo_url).scan()
    json.dump(inv.as_dict(), sys.stdout, indent=2)
    print()


# --- crd-schemas -------------------------------------------------------------


def strict_schema(node):
    """OpenAPI v3 (as found in a CRD) -> JSON schema that rejects unknown fields,
    the way the API server prunes or rejects them."""
    if isinstance(node, list):
        return [strict_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = dict(node)
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: strict_schema(v) for k, v in out["properties"].items()}
        if "additionalProperties" not in out and not out.get("x-kubernetes-preserve-unknown-fields"):
            out["additionalProperties"] = False
    for key in ("items", "additionalProperties", "not", "allOf", "anyOf", "oneOf"):
        if isinstance(out.get(key), (dict, list)):
            out[key] = strict_schema(out[key])
    if out.get("nullable") is True and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def cmd_crd_schemas(args):
    written = set()
    for path in args.files:
        try:
            docs = load_docs(path)
        except (OSError, yaml.YAMLError):
            continue  # the render check reports unreadable output
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "CustomResourceDefinition":
                continue
            spec = doc.get("spec") or {}
            kind = ((spec.get("names") or {}).get("kind") or "").lower()
            for version in spec.get("versions") or []:
                schema = (version.get("schema") or {}).get("openAPIV3Schema")
                if not (schema and kind and spec.get("group")):
                    continue
                target = os.path.join(args.out, spec["group"], f"{kind}_{version['name']}.json")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(strict_schema(schema), fh, default=str)
                written.add(f"{spec['group']}/{version['name']}/{spec['names']['kind']}")
    print(f"{len(written)} CRD schemas taken from the rendered manifests "
          f"({len({w.split('/')[0] for w in written})} API groups)")


# --- rendered manifests ------------------------------------------------------


def rendered_targets(rendered):
    """Yield (target, file) for every rendered file, e.g. ('helm/cilium', path)."""
    for top in ("manifests", "helm"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(rendered, top)):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith(".yaml"):
                    path = os.path.join(dirpath, name)
                    yield os.path.relpath(path, rendered)[: -len(".yaml")], path


def resource_id(doc, default_namespace=None):
    meta = doc.get("metadata") or {}
    namespace = meta.get("namespace") or default_namespace or ""
    return f"{doc.get('kind')}/{namespace + '/' if namespace else ''}{meta.get('name')}"


def source_yaml_files(root):
    """Tracked and untracked-but-not-ignored YAML files (falls back to a walk)."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-co", "--exclude-standard", "--", "*.yaml", "*.yml"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".cache")]
            out += [os.path.relpath(os.path.join(dirpath, f), root)
                    for f in filenames if f.endswith((".yaml", ".yml"))]
    return sorted(f for f in set(out)
                  if not f.startswith(".cache/") and os.path.isfile(os.path.join(root, f)))


# --- policy ------------------------------------------------------------------

FLUX_API = re.compile(r"^\s*(?:-\s*)?apiVersion:\s*[\"']?([\w.-]+\.toolkit\.fluxcd\.io)/", re.M)
FLUX_KEY = re.compile(r"^\s*(?:-\s*)?[\"']?([\w.-]*toolkit\.fluxcd\.io/[\w.-]+)[\"']?\s*:", re.M)
# A comment naming a GitHub release asset, next to a file of that name, marks the file as vendored.
VENDORED = re.compile(r"^\s*#.*?(https://github\.com/[^/\s]+/[^/\s]+/releases/download/([^/\s]+)/([^/\s]+))", re.M)
BUNDLE_VERSION = re.compile(r"^\s*gateway\.networking\.k8s\.io/bundle-version:\s*[\"']?([^\s\"']+)", re.M)


# Built-in cluster-scoped kinds; CRD scopes are read from the rendered CRDs.
CLUSTER_SCOPED = {
    "APIService", "CSIDriver", "CSINode", "CertificateSigningRequest", "ClusterRole", "ClusterRoleBinding",
    "CustomResourceDefinition", "DeviceClass", "FlowSchema", "IngressClass", "MutatingAdmissionPolicy",
    "MutatingAdmissionPolicyBinding", "MutatingWebhookConfiguration", "Namespace", "Node", "PersistentVolume",
    "PriorityClass", "PriorityLevelConfiguration", "RuntimeClass", "StorageClass", "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding", "ValidatingWebhookConfiguration", "VolumeAttachment",
    "VolumeAttributesClass",
}


def glob_match(pattern, value):
    """Argo CD project glob: '*' wildcards, a leading '!' negates."""
    pattern, value = str(pattern), str(value)
    if pattern.startswith("!"):
        return not fnmatch.fnmatchcase(value, pattern[1:])
    return fnmatch.fnmatchcase(value, pattern)


def project_denies(project, doc, namespace, cluster_scoped):
    """Why an AppProject would refuse this resource at sync time, or None.
    Mirrors AppProject.IsGroupKindPermitted / IsDestinationPermitted for the in-cluster destination."""
    group, kind = group_of(doc), doc.get("kind")

    def listed(entries):
        return any(glob_match(e.get("group", ""), group) and glob_match(e.get("kind", ""), kind)
                   for e in entries or [])

    if cluster_scoped:
        if not listed(project.get("clusterResourceWhitelist")):
            return "cluster-scoped kind is not in clusterResourceWhitelist"
        if listed(project.get("clusterResourceBlacklist")):
            return "kind is in clusterResourceBlacklist"
        return None
    whitelist = project.get("namespaceResourceWhitelist")
    if whitelist is not None and not listed(whitelist):
        return "kind is not in namespaceResourceWhitelist"
    if listed(project.get("namespaceResourceBlacklist")):
        return "kind is in namespaceResourceBlacklist"
    if namespace:
        patterns = [str(d.get("namespace", "")) for d in project.get("destinations") or []]
        if any(p.startswith("!") and not glob_match(p, namespace) for p in patterns):
            return f"namespace '{namespace}' is denied by the project's destinations"
        if not any(glob_match(p, namespace) for p in patterns if not p.startswith("!")):
            return f"namespace '{namespace}' is not in the project's destinations"
    return None


def is_plain_secret(doc):
    return (isinstance(doc, dict) and doc.get("kind") == "Secret" and doc.get("apiVersion") == "v1"
            and bool(doc.get("data") or doc.get("stringData")))


def cmd_policy(args):
    root = os.path.abspath(args.root)
    with open(args.inventory, encoding="utf-8") as fh:
        inventory = json.load(fh)
    default_ns = {f"helm/{h['target']}": h["namespace"] for h in inventory["helm"]}
    default_ns.update({f"manifests/{d['path']}": d["namespace"] for d in inventory["dirs"]})
    project_of = {f"helm/{h['target']}": h.get("project") for h in inventory["helm"]}
    project_of.update({f"manifests/{d['path']}": d.get("project") for d in inventory["dirs"]})
    failures = {"flux": [], "secrets": [], "routes": [], "shared": [], "project": [], "vendored": []}

    # Source tree: catches files no kustomization references any more.
    for rel in source_yaml_files(root):
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for regex, what in ((FLUX_API, "Flux apiVersion"), (FLUX_KEY, "Flux annotation/label")):
            for match in regex.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                failures["flux"].append(f"{rel}:{line}: {what} {match[1]}")
        if re.search(r"^kind:\s*Secret\s*$", text, re.M):
            try:
                for doc in load_docs(os.path.join(root, rel)):
                    if is_plain_secret(doc):
                        failures["secrets"].append(
                            f"{rel}: Secret '{(doc.get('metadata') or {}).get('name')}' carries data in Git; "
                            "use an ExternalSecret backed by Azure Key Vault")
            except yaml.YAMLError:
                pass
        for match in VENDORED.finditer(text):
            url, version, filename = match.groups()
            bundle = os.path.join(os.path.dirname(rel), filename)
            if not os.path.isfile(os.path.join(root, bundle)):
                continue  # just a link in a comment
            with open(os.path.join(root, bundle), encoding="utf-8", errors="replace") as fh:
                found = set(BUNDLE_VERSION.findall(fh.read()))
            if found and found != {version}:
                failures["vendored"].append(
                    f"{rel}: marker says {version} but {bundle} contains {', '.join(sorted(found))}. "
                    f"Re-vendor it:  curl -fsSL {url} -o {bundle}")

    # Rendered manifests: what Argo CD will actually apply.
    owners, gateways, routes, resources, crd_scope = {}, {}, [], [], {}
    for target, path in rendered_targets(args.rendered):
        try:
            docs = load_docs(path)
        except yaml.YAMLError as exc:
            failures["shared"].append(f"{target}: rendered output is not valid YAML: {str(exc).splitlines()[0]}")
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            flux = sorted({m[1] for m in FLUX_API.finditer(fh.read())})
        if flux:
            failures["flux"].append(f"{target} (rendered): Flux apiVersion {', '.join(flux)}")
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            meta = doc.get("metadata") or {}
            namespace = meta.get("namespace") or default_ns.get(target)
            owners.setdefault((group_of(doc), resource_id(doc)), set()).add(target)
            resources.append((target, namespace, doc))
            if doc["kind"] == "CustomResourceDefinition":
                spec = doc.get("spec") or {}
                crd_scope[(spec.get("group"), (spec.get("names") or {}).get("kind"))] = spec.get("scope")
            if target.startswith("manifests/") and is_plain_secret(doc):
                failures["secrets"].append(
                    f"{target} (rendered): Secret '{meta.get('name')}' carries data; use an ExternalSecret")
            if group_of(doc) != GATEWAY_GROUP:
                continue
            if doc["kind"] == "Gateway":
                listeners = (doc.get("spec") or {}).get("listeners") or []
                gateways[(namespace, meta.get("name"))] = {l.get("name") for l in listeners}
            elif doc["kind"].endswith("Route"):
                routes.append((target, namespace, doc))

    for target, namespace, doc in routes:
        name = f"{doc['kind']} {namespace}/{(doc.get('metadata') or {}).get('name')}"
        parents = (doc.get("spec") or {}).get("parentRefs") or []
        if not parents:
            failures["routes"].append(f"{target}: {name} has no parentRefs, so no Gateway will serve it")
        for ref in parents:
            if ref.get("kind", "Gateway") != "Gateway" or ref.get("group", GATEWAY_GROUP) != GATEWAY_GROUP:
                continue
            key = (ref.get("namespace") or namespace, ref.get("name"))
            if key not in gateways:
                known = ", ".join(sorted(f"{n}/{g}" for n, g in gateways)) or "none"
                failures["routes"].append(
                    f"{target}: {name} points at Gateway {key[0]}/{key[1]}, which is not defined in this repo "
                    f"(defined: {known})")
            elif ref.get("sectionName") and ref["sectionName"] not in gateways[key]:
                failures["routes"].append(
                    f"{target}: {name} points at listener '{ref['sectionName']}' of Gateway {key[0]}/{key[1]}, "
                    f"which only has: {', '.join(sorted(str(l) for l in gateways[key]))}")

    # What Argo CD only says after the merge: "resource X is not permitted in project Y".
    for target, namespace, doc in resources:
        project = inventory["projects"].get(project_of.get(target) or "")
        if project is None:
            continue
        group = group_of(doc)
        scope = crd_scope.get((group, doc["kind"]))
        builtin = "." not in group or group.endswith(".k8s.io")
        cluster_scoped = scope == "Cluster" if scope else builtin and doc["kind"] in CLUSTER_SCOPED
        reason = project_denies(project, doc, namespace, cluster_scoped)
        if reason:
            failures["project"].append(
                f"{target}: {resource_id(doc, namespace)} is not permitted in AppProject "
                f"'{project_of[target]}': {reason}")

    for (group, rid), targets in sorted(owners.items()):
        if len(targets) > 1:
            failures["shared"].append(
                f"{rid} ({group or 'core'}) is rendered by {' and '.join(sorted(targets))}; "
                "two Applications would fight over it")

    titles = {
        "flux": "Flux leftovers",
        "routes": "Gateway API routes attach to a Gateway defined in this repo",
        "secrets": "no plain Secrets with data (ExternalSecret only)",
        "shared": "each resource is owned by exactly one Application",
        "project": "every resource is permitted by its Application's AppProject",
        "vendored": "vendored bundles match their version marker",
    }
    failed = False
    for key, title in titles.items():
        items = sorted(set(failures[key]))
        print(f"  {'FAIL' if items else 'ok  '}  {title}")
        for item in items[:40]:
            print(f"          {item}")
        if len(items) > 40:
            print(f"          ... and {len(items) - 40} more")
        failed |= bool(items)
    print(f"  (checked {len(routes)} routes against {len(gateways)} gateways, {len(owners)} rendered resources)")
    sys.exit(1 if failed else 0)


# --- diff --------------------------------------------------------------------

NOISE = re.compile(r"^\s*(helm\.sh/chart|app\.kubernetes\.io/version):.*\n", re.M)
COMMENT_LIMIT = 60000  # GitHub rejects comment bodies over 65536 characters
MARKER = "<!-- gitops-render-diff -->"


def split_resources(path, redact):
    """{resource id: text} for one rendered file. Text stays as rendered, except
    that Secret payloads are replaced (charts generate random ones, and they do
    not belong in a PR comment) and pure version labels are dropped."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        chunks = re.split(r"^---[ \t]*$", fh.read(), flags=re.M)
    resources = {}
    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            doc = yaml.load(chunk, Loader=Loader)
        except yaml.YAMLError:
            doc = None
        if not isinstance(doc, dict) or not doc.get("kind"):
            continue
        text = chunk.strip("\n") + "\n"
        if redact:
            text = NOISE.sub("", text)
            if doc.get("kind") == "Secret":
                for field in ("data", "stringData"):
                    if isinstance(doc.get(field), dict):
                        doc[field] = {k: "<redacted>" for k in doc[field]}
                text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
        key, n = resource_id(doc), 2
        while key in resources:  # same id twice in one file: keep both visible
            key, n = f"{resource_id(doc)}#{n}", n + 1
        resources[key] = text
    return resources


def cmd_diff(args):
    sides = {}
    for side, directory in (("base", args.base), ("head", args.head)):
        sides[side] = {t: split_resources(p, not args.raw) for t, p in rendered_targets(directory)}
    sections, patch = [], []
    totals = {"added": 0, "removed": 0, "changed": 0}
    for target in sorted(set(sides["base"]) | set(sides["head"])):
        base, head = sides["base"].get(target, {}), sides["head"].get(target, {})
        counts, body = {"added": 0, "removed": 0, "changed": 0}, []
        # CRDs last: they are the bulk of a chart diff and the first thing worth truncating.
        for rid in sorted(set(base) | set(head), key=lambda r: (r.startswith("CustomResourceDefinition/"), r)):
            old, new = base.get(rid, ""), head.get(rid, "")
            if old == new:
                continue
            counts["added" if not old else "removed" if not new else "changed"] += 1
            body += difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                f"a/{target}/{rid}" if old else "/dev/null",
                f"b/{target}/{rid}" if new else "/dev/null", n=3)
        if body:
            text = "".join(line if line.endswith("\n") else line + "\n" for line in body)
            sections.append((target, counts, text))
            patch.append(text)
            for key in totals:
                totals[key] += counts[key]

    failed = []
    for side, directory in (("base", args.base), ("head", args.head)):
        failed_file = os.path.join(directory, "FAILED")
        if os.path.isfile(failed_file):
            with open(failed_file, encoding="utf-8") as fh:
                failed += [(side, *line.rstrip("\n").split("\t", 1)) for line in fh if line.strip()]

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "render.diff"), "w", encoding="utf-8") as fh:
        fh.write("".join(patch))

    def describe(counts):
        parts = [f"{counts[k]} {k}" for k in ("changed", "added", "removed") if counts[k]]
        return ", ".join(parts)

    lines = [MARKER, "## Rendered manifest diff", ""]
    if args.title:
        lines += [args.title, ""]
    if failed:
        lines += ["**Some targets failed to render, so this diff is incomplete:**", ""]
        lines += [f"- {side}: `{target}` {' '.join(message)[:300]}" for side, target, *message in failed] + [""]
    if not sections:
        lines += ["No change in what Argo CD will apply: every kustomize directory and Helm chart "
                  "renders to the same manifests as on the base branch.", ""]
    else:
        lines += [f"**{describe(totals)}** resource(s) across {len(sections)} target(s). "
                  "Secret payloads are redacted and `helm.sh/chart` / `app.kubernetes.io/version` "
                  "labels are left out so a chart bump shows only real changes.", ""]
    # Share the comment between targets: small diffs are shown whole, and what
    # they leave unused goes to the large ones.
    budget = COMMENT_LIMIT - sum(len(l) + 1 for l in lines) - 400 - 300 * len(sections)
    room, remaining = {}, len(sections)
    for target, _, text in sorted(sections, key=lambda sec: len(sec[2])):
        room[target] = min(len(text), max(budget // remaining, 0))
        budget -= room[target]
        remaining -= 1
    truncated = False
    for target, counts, text in sections:
        fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))
        block = f"<details><summary><code>{target}</code>: {describe(counts)}</summary>\n\n"
        if room[target] < min(len(text), 400):
            block += "Diff left out to fit the comment.\n"
            truncated = True
        else:
            shown = text if len(text) <= room[target] else text[:room[target]].rsplit("\n", 1)[0] + "\n"
            block += f"{fence}diff\n{shown}{fence}\n"
            if len(shown) < len(text):
                block += f"\nTruncated: {len(text) - len(shown)} more characters in the artifact.\n"
                truncated = True
        lines.append(block + "\n</details>\n")
    if truncated or sections:
        note = "The diff was truncated to fit a comment. " if truncated else ""
        lines += ["", f"{note}The full diff is the `render.diff` file in this run's `render-diff` artifact."]
    with open(os.path.join(args.out, "comment.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"resources": sum(totals.values()), "targets": len(sections), "renderFailures": len(failed)}, fh)
    print(f"{describe(totals) or 'no changes'} across {len(sections)} target(s); "
          f"wrote {args.out}/render.diff and {args.out}/comment.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inventory")
    p.add_argument("--root", default=".")
    p.add_argument("--git-revision", default="main", help="revision every git source must track")
    p.add_argument("--repo-url", default="", help="this repository's URL, if known")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("crd-schemas")
    p.add_argument("--out", required=True)
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_crd_schemas)

    p = sub.add_parser("policy")
    p.add_argument("--root", default=".")
    p.add_argument("--rendered", required=True)
    p.add_argument("--inventory", required=True)
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("diff")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--raw", action="store_true", help="do not redact Secrets or drop version labels")
    p.set_defaults(func=cmd_diff)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
