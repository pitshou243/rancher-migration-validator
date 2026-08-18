# Rancher Migration Validator

`rancher-migration-validator` is an **unofficial support/development utility** for validating Rancher metadata after a `rancher-backup` backup/restore migration.

It answers a practical question:

> After the restore completed, does the target Rancher instance contain the same important Rancher/Fleet objects and configuration as the source?

## Status

MVP / experimental.

This tool is **not** a SUSE/Rancher supported product and does not replace the Rancher Backup Restore Operator, an etcd backup, or functional application testing.

## What it does

The MVP supports four workflows:

1. **Live source → live target comparison**
2. **Rancher backup `.tar.gz` → live target comparison**
3. **Pre-migration baseline capture → post-migration validation**
4. **Backup inventory inspection**

It normalizes objects before comparison so expected Kubernetes metadata differences do not create false positives.

Ignored by default:

- `metadata.uid`
- `metadata.resourceVersion`
- `metadata.creationTimestamp`
- `metadata.generation`
- `metadata.managedFields`
- `status`
- selected controller-generated annotations

Secret values are **never printed**. With `--compare-secret-values`, the tool compares SHA-256 hashes of secret payloads.

## Requirements

- Python 3.9+
- `kubectl`
- PyYAML
- kubeconfigs with permission to list the objects being validated

Install dependency:

```bash
python3 -m pip install -r requirements.txt
```

## Quick start

### 1. Compare two live Rancher instances

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml
```

### 2. Compare a rancher-backup file with the restored target

```bash
./rancher-migration-validator.py compare \
  --backup ./rancher-backup-2026-08-17.tar.gz \
  --target-kubeconfig ./target.yaml
```

### 3. Recommended migration workflow: capture a baseline

Before migration:

```bash
./rancher-migration-validator.py capture \
  --kubeconfig ./source.yaml \
  --output ./source-baseline.json
```

After restore:

```bash
./rancher-migration-validator.py validate \
  --baseline ./source-baseline.json \
  --target-kubeconfig ./target.yaml
```

### 4. Inspect backup contents

```bash
./rancher-migration-validator.py inspect-backup \
  --backup ./rancher-backup.tar.gz
```

## Compare all Rancher/Fleet API resources

The default profile intentionally uses a curated, high-value set.

For broader discovery:

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml \
  --all-rancher-resources
```

This discovers API resources whose names include Rancher/Fleet `cattle.io` groups.

## Compare selected resources only

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml \
  --resource clusters.management.cattle.io \
  --resource projects.management.cattle.io \
  --resource gitrepos.fleet.cattle.io
```

## Compare secret values without exposing them

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml \
  --compare-secret-values
```

The report contains only object identity/difference paths. Secret data is converted to a one-way SHA-256 value before comparison.

## Include status

Configuration equivalence and operational health are different questions. For that reason `status` is excluded by default.

To include it:

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml \
  --include-status
```

For a migration validation workflow, configuration comparison should normally run first. Operational checks can be run separately after Rancher controllers and agents have reconciled.

## JSON output

```bash
./rancher-migration-validator.py compare \
  --source-kubeconfig ./source.yaml \
  --target-kubeconfig ./target.yaml \
  --json-output ./migration-report.json
```

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | PASS |
| `1` | WARN |
| `2` | FAIL |
| `3` | Tool/input/kubectl error |

This makes the tool suitable for CI or migration runbooks.

## Example output

```text
Rancher Migration Validation
============================

RESULT: FAIL
Source objects: 1482
Target objects: 1480
Identical objects: 1477
Missing from target: 2
Changed: 3
Extra on target: 0

Differences
-----------
[CRITICAL] GitRepo fleet-default/apps: configuration differs
  paths: spec.clientSecretName

[CRITICAL] Project c-m-abc:p-12345: missing from target

[WARNING] Setting server-url: configuration differs
  paths: value
```

## Default resource profile

The initial profile includes high-value resources such as:

- management clusters and projects
- users
- global roles and bindings
- role templates
- cluster/project role template bindings
- Rancher settings/features/auth configuration
- provisioning clusters
- CAPI machines
- Fleet GitRepos, Bundles, BundleDeployments, Fleet Clusters and ClusterGroups

The list is intentionally conservative and should evolve as the project is tested against real migrations.

## Backup comparison behavior

A Rancher backup may contain many resource kinds. By default, backup comparison filters the parsed source objects to kinds represented by the selected target collection profile.

Use:

```bash
--backup-all-objects
```

to compare every parsed Kubernetes object in the backup. This is stricter and may produce expected differences for operator/runtime objects.

## Security

The tool:

- does not extract the backup archive to disk
- does not print Secret contents
- can hash Secret values for equivalence comparison
- only uses the supplied kubeconfig through `kubectl`

Treat generated baseline and JSON report files as potentially sensitive metadata.

## Important limitations

The MVP validates **metadata/configuration equivalence**, not complete functionality.

A successful comparison does not prove:

- downstream cluster agents are connected
- authentication works end to end
- Fleet workloads successfully deploy
- webhooks/controllers are healthy
- external secrets or identity-provider state is available
- the target local cluster infrastructure is equivalent
- user workloads are backed up

A later phase should add dedicated functional checks for Rancher availability, downstream cluster connectivity, Fleet readiness, authentication configuration sanity, and restore-job analysis.

## Suggested next phases

### Phase 2 — Rancher-aware health checks

- downstream cluster `Ready`/connected status
- Fleet GitRepo readiness
- BundleDeployment failure summary
- Rancher deployment health
- webhook health
- cattle-cluster-agent/cattle-node-agent connectivity indicators
- authentication provider presence/configuration checks

### Phase 3 — ResourceSet coverage

- read `Backup.spec.resourceSetName`
- parse the selected ResourceSet
- build an explicit expected-resource coverage matrix
- distinguish:
  - expected but absent from backup
  - present in backup but absent from target
  - present in both but configuration differs

### Phase 4 — Support bundle/report packaging

Produce a support-friendly report containing:

- Rancher version
- Kubernetes version
- backup metadata
- ResourceSet
- inventory counts
- normalized differences
- functional checks
- migration confidence/result

## License

Apache-2.0 is a sensible choice if this is contributed to Rancher support tooling. A LICENSE file is intentionally not asserted by this prototype until the target repository's licensing requirements are confirmed.
