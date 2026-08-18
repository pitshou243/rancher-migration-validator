#!/usr/bin/env python3
"""
rancher-migration-validator

Compare Rancher metadata between:
  * two live Rancher local clusters
  * a rancher-backup .tar.gz and a live target cluster
  * a captured baseline and a live target cluster

The tool intentionally compares Kubernetes/Rancher metadata, not downstream
cluster workloads or etcd snapshots.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tarfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install PyYAML", file=sys.stderr)
    sys.exit(2)


# Curated MVP set: high-value Rancher/Fleet objects that are useful after migration.
DEFAULT_RESOURCES = [
    "clusters.management.cattle.io",
    "projects.management.cattle.io",
    "users.management.cattle.io",
    "globalroles.management.cattle.io",
    "globalrolebindings.management.cattle.io",
    "roletemplates.management.cattle.io",
    "clusterroletemplatebindings.management.cattle.io",
    "projectroletemplatebindings.management.cattle.io",
    "settings.management.cattle.io",
    "features.management.cattle.io",
    "authconfigs.management.cattle.io",
    "clusters.provisioning.cattle.io",
    "machines.cluster.x-k8s.io",
    "gitrepos.fleet.cattle.io",
    "bundles.fleet.cattle.io",
    "bundledeployments.fleet.cattle.io",
    "clusters.fleet.cattle.io",
    "clustergroups.fleet.cattle.io",
]

# Kinds where a missing object is usually significant for migration equivalence.
CRITICAL_KINDS = {
    "Cluster",
    "Project",
    "User",
    "GlobalRole",
    "GlobalRoleBinding",
    "RoleTemplate",
    "ClusterRoleTemplateBinding",
    "ProjectRoleTemplateBinding",
    "GitRepo",
    "ClusterGroup",
}

# Metadata that is expected to change when objects are restored into a new cluster.
VOLATILE_METADATA_FIELDS = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}

# Annotations commonly mutated by apiserver/controllers and not useful for equivalence.
VOLATILE_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "objectset.rio.cattle.io/applied",
    "objectset.rio.cattle.io/id",
    "lifecycle.cattle.io/create.cluster-agent-controller-cleanup",
}

RANCHER_GROUP_MARKERS = (
    ".cattle.io",
    ".fleet.cattle.io",
    "fleet.cattle.io",
    "management.cattle.io",
    "provisioning.cattle.io",
    "resources.cattle.io",
    "catalog.cattle.io",
)


@dataclass(frozen=True, order=True)
class ObjectKey:
    api_version: str
    kind: str
    namespace: str
    name: str

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> "ObjectKey":
        md = obj.get("metadata") or {}
        return cls(
            str(obj.get("apiVersion", "")),
            str(obj.get("kind", "")),
            str(md.get("namespace", "")),
            str(md.get("name", "")),
        )

    def short(self) -> str:
        ns = f"{self.namespace}/" if self.namespace else ""
        return f"{self.kind} {ns}{self.name}"


@dataclass
class DiffEntry:
    key: ObjectKey
    severity: str
    summary: str
    paths: List[str]


@dataclass
class Comparison:
    source_count: int
    target_count: int
    matched: int
    missing: List[DiffEntry]
    extra: List[DiffEntry]
    changed: List[DiffEntry]

    @property
    def critical_count(self) -> int:
        return sum(1 for x in self.missing + self.changed if x.severity == "CRITICAL")

    @property
    def warning_count(self) -> int:
        return sum(1 for x in self.missing + self.changed + self.extra if x.severity == "WARNING")

    @property
    def result(self) -> str:
        if self.critical_count:
            return "FAIL"
        if self.warning_count:
            return "WARN"
        return "PASS"


class KubectlError(RuntimeError):
    pass


def run_kubectl(kubeconfig: str, args: Sequence[str], context: Optional[str] = None) -> str:
    cmd = ["kubectl", "--kubeconfig", kubeconfig]
    if context:
        cmd += ["--context", context]
    cmd += list(args)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise KubectlError(
            f"kubectl failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.stdout


def get_api_resources(kubeconfig: str, context: Optional[str]) -> List[str]:
    out = run_kubectl(kubeconfig, ["api-resources", "-o", "name"], context)
    return [line.strip() for line in out.splitlines() if line.strip()]


def rancher_api_resources(kubeconfig: str, context: Optional[str]) -> List[str]:
    resources = get_api_resources(kubeconfig, context)
    selected = []
    for r in resources:
        lower = r.lower()
        if any(marker in lower for marker in RANCHER_GROUP_MARKERS):
            selected.append(r)
    return sorted(set(selected))


def get_resource_objects(
    kubeconfig: str, resource: str, context: Optional[str], ignore_forbidden: bool
) -> List[Dict[str, Any]]:
    # -A works for both namespaced resources and many cluster-scoped resources,
    # but some kubectl versions reject it for cluster-scoped resources.
    attempts = [
        ["get", resource, "-A", "-o", "json"],
        ["get", resource, "-o", "json"],
    ]
    last_error = None
    for args in attempts:
        try:
            payload = json.loads(run_kubectl(kubeconfig, args, context))
            if payload.get("kind", "").endswith("List"):
                return list(payload.get("items") or [])
            return [payload]
        except (KubectlError, json.JSONDecodeError) as exc:
            last_error = exc
    if ignore_forbidden:
        print(f"WARN: skipping {resource}: {last_error}", file=sys.stderr)
        return []
    raise KubectlError(str(last_error))


def collect_live(
    kubeconfig: str,
    resources: Sequence[str],
    context: Optional[str] = None,
    ignore_forbidden: bool = True,
) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    available = set(get_api_resources(kubeconfig, context))
    for resource in resources:
        if resource not in available:
            print(f"WARN: resource not available on cluster: {resource}", file=sys.stderr)
            continue
        objects.extend(get_resource_objects(kubeconfig, resource, context, ignore_forbidden))
    return objects


def iter_yaml_documents(raw: bytes, name: str) -> Iterable[Dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace")
    try:
        docs = yaml.safe_load_all(text)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("kind", "")).endswith("List") and isinstance(doc.get("items"), list):
                for item in doc["items"]:
                    if isinstance(item, dict):
                        yield item
            elif doc.get("apiVersion") and doc.get("kind") and doc.get("metadata") is not None:
                yield doc
    except yaml.YAMLError as exc:
        print(f"WARN: unable to parse {name}: {exc}", file=sys.stderr)


def load_backup(path: str) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    with tarfile.open(p, mode="r:*") as tf:
        for member in tf:
            if not member.isfile():
                continue
            lower = member.name.lower()
            if not (lower.endswith(".yaml") or lower.endswith(".yml") or lower.endswith(".json")):
                continue
            f = tf.extractfile(member)
            if not f:
                continue
            raw = f.read()
            objects.extend(iter_yaml_documents(raw, member.name))
    return objects


def secret_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_object(
    obj: Mapping[str, Any],
    include_status: bool = False,
    compare_secret_values: bool = False,
) -> Dict[str, Any]:
    o = copy.deepcopy(dict(obj))
    md = o.get("metadata")
    if isinstance(md, dict):
        for field in VOLATILE_METADATA_FIELDS:
            md.pop(field, None)
        anns = md.get("annotations")
        if isinstance(anns, dict):
            for key in list(anns):
                if key in VOLATILE_ANNOTATIONS:
                    anns.pop(key, None)
            if not anns:
                md.pop("annotations", None)

    if not include_status:
        o.pop("status", None)

    # Never emit secret material. If comparison is requested, compare a stable hash.
    if o.get("kind") == "Secret":
        for field in ("data", "stringData"):
            if field in o:
                if compare_secret_values:
                    o[field] = {"__valueHash": secret_hash(o[field])}
                else:
                    o.pop(field, None)

    return prune_empty(o)


def prune_empty(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            pv = prune_empty(v)
            if pv is None:
                continue
            if pv == {} or pv == []:
                continue
            out[k] = pv
        return out
    if isinstance(value, list):
        return [prune_empty(v) for v in value]
    return value


def build_index(
    objects: Iterable[Mapping[str, Any]],
    include_status: bool,
    compare_secret_values: bool,
) -> Dict[ObjectKey, Dict[str, Any]]:
    idx: Dict[ObjectKey, Dict[str, Any]] = {}
    duplicates: Dict[ObjectKey, int] = {}
    for obj in objects:
        key = ObjectKey.from_obj(obj)
        if not all([key.api_version, key.kind, key.name]):
            continue
        norm = normalize_object(obj, include_status, compare_secret_values)
        if key in idx:
            duplicates[key] = duplicates.get(key, 1) + 1
        idx[key] = norm
    if duplicates:
        print(
            "WARN: duplicate object identities found; last object wins: "
            + ", ".join(f"{k.short()} x{n}" for k, n in list(duplicates.items())[:10]),
            file=sys.stderr,
        )
    return idx


def leaf_diff_paths(a: Any, b: Any, prefix: str = "") -> List[str]:
    paths: List[str] = []
    if type(a) != type(b):
        return [prefix or "$"]
    if isinstance(a, dict):
        keys = sorted(set(a) | set(b))
        for k in keys:
            p = f"{prefix}.{k}" if prefix else k
            if k not in a or k not in b:
                paths.append(p)
            else:
                paths.extend(leaf_diff_paths(a[k], b[k], p))
    elif isinstance(a, list):
        if a != b:
            paths.append(prefix or "$")
    elif a != b:
        paths.append(prefix or "$")
    return paths


def severity_for(key: ObjectKey, missing: bool = False, changed: bool = False, extra: bool = False) -> str:
    if key.kind in CRITICAL_KINDS and (missing or changed):
        return "CRITICAL"
    if missing or changed:
        return "WARNING"
    return "INFO"


def compare_indexes(
    source: Mapping[ObjectKey, Dict[str, Any]],
    target: Mapping[ObjectKey, Dict[str, Any]],
) -> Comparison:
    sk = set(source)
    tk = set(target)

    missing = [
        DiffEntry(k, severity_for(k, missing=True), "missing from target", [])
        for k in sorted(sk - tk)
    ]
    extra = [
        DiffEntry(k, severity_for(k, extra=True), "exists only on target", [])
        for k in sorted(tk - sk)
    ]

    changed: List[DiffEntry] = []
    matched = 0
    for k in sorted(sk & tk):
        if source[k] == target[k]:
            matched += 1
            continue
        paths = leaf_diff_paths(source[k], target[k])
        changed.append(
            DiffEntry(
                k,
                severity_for(k, changed=True),
                "configuration differs",
                paths[:50],
            )
        )
    return Comparison(
        source_count=len(source),
        target_count=len(target),
        matched=matched,
        missing=missing,
        extra=extra,
        changed=changed,
    )


def inventory_by_kind(index: Mapping[ObjectKey, Any]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for k in index:
        result[k.kind] = result.get(k.kind, 0) + 1
    return dict(sorted(result.items()))


def report_text(comp: Comparison, source_idx: Mapping[ObjectKey, Any], target_idx: Mapping[ObjectKey, Any]) -> str:
    src_inv = inventory_by_kind(source_idx)
    tgt_inv = inventory_by_kind(target_idx)
    kinds = sorted(set(src_inv) | set(tgt_inv))

    lines = [
        "Rancher Migration Validation",
        "============================",
        "",
        f"RESULT: {comp.result}",
        f"Source objects: {comp.source_count}",
        f"Target objects: {comp.target_count}",
        f"Identical objects: {comp.matched}",
        f"Missing from target: {len(comp.missing)}",
        f"Changed: {len(comp.changed)}",
        f"Extra on target: {len(comp.extra)}",
        "",
        "Inventory",
        "---------",
        f"{'Kind':42} {'Source':>8} {'Target':>8}",
    ]
    for kind in kinds:
        lines.append(f"{kind[:42]:42} {src_inv.get(kind, 0):8d} {tgt_inv.get(kind, 0):8d}")

    entries = comp.missing + comp.changed + comp.extra
    if entries:
        lines += ["", "Differences", "-----------"]
        rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
        for item in sorted(entries, key=lambda x: (rank.get(x.severity, 9), x.key)):
            lines.append(f"[{item.severity}] {item.key.short()}: {item.summary}")
            if item.paths:
                lines.append("  paths: " + ", ".join(item.paths[:12]))
                if len(item.paths) > 12:
                    lines.append(f"  ... {len(item.paths) - 12} more differing path(s)")
    else:
        lines += ["", "No differences found after normalization."]

    lines += [
        "",
        "Notes",
        "-----",
        "* status is excluded unless --include-status is used.",
        "* volatile Kubernetes metadata is excluded.",
        "* Secret values are never printed; use --compare-secret-values to compare hashes.",
        "* A PASS means equivalence for the collected objects, not full application functionality.",
    ]
    return "\n".join(lines)


def comparison_json(comp: Comparison) -> Dict[str, Any]:
    def enc(entries: List[DiffEntry]) -> List[Dict[str, Any]]:
        return [
            {
                "key": asdict(e.key),
                "severity": e.severity,
                "summary": e.summary,
                "paths": e.paths,
            }
            for e in entries
        ]

    return {
        "result": comp.result,
        "sourceCount": comp.source_count,
        "targetCount": comp.target_count,
        "matched": comp.matched,
        "criticalCount": comp.critical_count,
        "warningCount": comp.warning_count,
        "missing": enc(comp.missing),
        "changed": enc(comp.changed),
        "extra": enc(comp.extra),
    }


def save_baseline(
    path: str,
    index: Mapping[ObjectKey, Dict[str, Any]],
    include_status: bool,
    compare_secret_values: bool,
) -> None:
    payload = {
        "schemaVersion": 1,
        "includeStatus": include_status,
        "compareSecretValues": compare_secret_values,
        "objects": [
            {"key": asdict(k), "object": index[k]}
            for k in sorted(index)
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_baseline(path: str) -> Tuple[Dict[ObjectKey, Dict[str, Any]], Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("Unsupported baseline schemaVersion")
    idx: Dict[ObjectKey, Dict[str, Any]] = {}
    for entry in payload.get("objects", []):
        k = entry["key"]
        key = ObjectKey(k["api_version"], k["kind"], k["namespace"], k["name"])
        idx[key] = entry["object"]
    return idx, payload


def resource_list(args: argparse.Namespace, kubeconfig: str, context: Optional[str]) -> List[str]:
    if args.resource:
        return list(dict.fromkeys(args.resource))
    if getattr(args, "all_rancher_resources", False):
        return rancher_api_resources(kubeconfig, context)
    return DEFAULT_RESOURCES


def compare_command(args: argparse.Namespace) -> int:
    if bool(args.source_kubeconfig) == bool(args.backup):
        raise ValueError("Specify exactly one of --source-kubeconfig or --backup")

    if args.source_kubeconfig:
        resources = resource_list(args, args.source_kubeconfig, args.source_context)
        source_objs = collect_live(
            args.source_kubeconfig, resources, args.source_context, args.ignore_forbidden
        )
    else:
        source_objs = load_backup(args.backup)
        # For backup comparisons, target resources are derived from object kinds/resources
        # by using kubectl names where possible. The curated list is safer and predictable,
        # while --all-rancher-resources discovers all Rancher API resources on target.
        resources = resource_list(args, args.target_kubeconfig, args.target_context)

    target_objs = collect_live(
        args.target_kubeconfig, resources, args.target_context, args.ignore_forbidden
    )

    source_idx = build_index(source_objs, args.include_status, args.compare_secret_values)
    target_idx = build_index(target_objs, args.include_status, args.compare_secret_values)

    # When comparing a backup, restrict source to identities whose kinds exist in the
    # target collection unless --backup-all-objects is explicitly requested.
    if args.backup and not args.backup_all_objects:
        target_kinds = {k.kind for k in target_idx}
        source_idx = {k: v for k, v in source_idx.items() if k.kind in target_kinds}

    comp = compare_indexes(source_idx, target_idx)
    print(report_text(comp, source_idx, target_idx))

    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(comparison_json(comp), indent=2) + "\n", encoding="utf-8"
        )

    return 2 if comp.result == "FAIL" else 1 if comp.result == "WARN" else 0


def capture_command(args: argparse.Namespace) -> int:
    resources = resource_list(args, args.kubeconfig, args.context)
    objs = collect_live(args.kubeconfig, resources, args.context, args.ignore_forbidden)
    idx = build_index(objs, args.include_status, args.compare_secret_values)
    save_baseline(args.output, idx, args.include_status, args.compare_secret_values)
    print(f"Captured {len(idx)} normalized objects to {args.output}")
    return 0


def validate_command(args: argparse.Namespace) -> int:
    source_idx, meta = load_baseline(args.baseline)
    include_status = bool(meta.get("includeStatus", False))
    compare_secret_values = bool(meta.get("compareSecretValues", False))

    resources = resource_list(args, args.target_kubeconfig, args.target_context)
    target_objs = collect_live(
        args.target_kubeconfig, resources, args.target_context, args.ignore_forbidden
    )
    target_idx = build_index(target_objs, include_status, compare_secret_values)
    comp = compare_indexes(source_idx, target_idx)
    print(report_text(comp, source_idx, target_idx))
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(comparison_json(comp), indent=2) + "\n", encoding="utf-8"
        )
    return 2 if comp.result == "FAIL" else 1 if comp.result == "WARN" else 0


def inspect_backup_command(args: argparse.Namespace) -> int:
    objs = load_backup(args.backup)
    idx = build_index(objs, args.include_status, args.compare_secret_values)
    inv = inventory_by_kind(idx)
    print(f"Backup objects parsed: {len(idx)}")
    print("")
    print(f"{'Kind':42} {'Count':>8}")
    print("-" * 52)
    for kind, count in inv.items():
        print(f"{kind[:42]:42} {count:8d}")
    return 0


def add_collection_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--resource",
        action="append",
        help="Kubernetes resource name to compare. Repeatable. Overrides default list.",
    )
    p.add_argument(
        "--all-rancher-resources",
        action="store_true",
        help="Discover and collect all API resources with Rancher/Fleet cattle.io groups.",
    )
    p.add_argument(
        "--ignore-forbidden",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip resources that cannot be listed (default: true).",
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rancher-migration-validator",
        description="Validate Rancher metadata equivalence after backup/restore migration.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("compare", help="Compare live source or backup against live target.")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--source-kubeconfig")
    src.add_argument("--backup", help="rancher-backup .tar.gz file")
    c.add_argument("--source-context")
    c.add_argument("--target-kubeconfig", required=True)
    c.add_argument("--target-context")
    c.add_argument("--include-status", action="store_true")
    c.add_argument("--compare-secret-values", action="store_true")
    c.add_argument(
        "--backup-all-objects",
        action="store_true",
        help="Compare every Kubernetes object found in backup rather than filtering to target-collected kinds.",
    )
    c.add_argument("--json-output", help="Write machine-readable comparison JSON.")
    add_collection_flags(c)
    c.set_defaults(func=compare_command)

    cap = sub.add_parser("capture", help="Capture normalized pre-migration baseline.")
    cap.add_argument("--kubeconfig", required=True)
    cap.add_argument("--context")
    cap.add_argument("--output", required=True)
    cap.add_argument("--include-status", action="store_true")
    cap.add_argument("--compare-secret-values", action="store_true")
    add_collection_flags(cap)
    cap.set_defaults(func=capture_command)

    v = sub.add_parser("validate", help="Validate live target against captured baseline.")
    v.add_argument("--baseline", required=True)
    v.add_argument("--target-kubeconfig", required=True)
    v.add_argument("--target-context")
    v.add_argument("--json-output")
    add_collection_flags(v)
    v.set_defaults(func=validate_command)

    i = sub.add_parser("inspect-backup", help="Show object inventory contained in backup.")
    i.add_argument("--backup", required=True)
    i.add_argument("--include-status", action="store_true")
    i.add_argument("--compare-secret-values", action="store_true")
    i.set_defaults(func=inspect_backup_command)

    return p


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.func(args))
    except (KubectlError, FileNotFoundError, ValueError, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
