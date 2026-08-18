import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "rancher-migration-validator.py"
spec = importlib.util.spec_from_file_location("rmv", MODULE_PATH)
rmv = importlib.util.module_from_spec(spec)
import sys
sys.modules["rmv"] = rmv
spec.loader.exec_module(rmv)


def obj(kind="Project", name="p-1", namespace="", spec_data=None):
    return {
        "apiVersion": "management.cattle.io/v3",
        "kind": kind,
        "metadata": {
            "name": name,
            **({"namespace": namespace} if namespace else {}),
            "uid": "abc",
            "resourceVersion": "1",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "managedFields": [{"x": 1}],
        },
        "spec": spec_data or {"displayName": "test"},
        "status": {"state": "active"},
    }


def test_normalization_removes_volatile_metadata_and_status():
    n = rmv.normalize_object(obj())
    assert "uid" not in n["metadata"]
    assert "resourceVersion" not in n["metadata"]
    assert "creationTimestamp" not in n["metadata"]
    assert "managedFields" not in n["metadata"]
    assert "status" not in n


def test_normalization_can_include_status():
    n = rmv.normalize_object(obj(), include_status=True)
    assert n["status"]["state"] == "active"


def test_secret_values_are_removed_by_default():
    s = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "x", "namespace": "n"},
        "data": {"password": "c2VjcmV0"},
    }
    n = rmv.normalize_object(s)
    assert "data" not in n


def test_secret_hash_comparison_does_not_expose_value():
    s = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "x", "namespace": "n"},
        "data": {"password": "c2VjcmV0"},
    }
    n = rmv.normalize_object(s, compare_secret_values=True)
    assert n["data"]["__valueHash"].startswith("sha256:")
    assert "c2VjcmV0" not in str(n)


def test_compare_detects_missing_critical_object():
    source = rmv.build_index([obj()], False, False)
    target = {}
    result = rmv.compare_indexes(source, target)
    assert result.result == "FAIL"
    assert len(result.missing) == 1
    assert result.missing[0].severity == "CRITICAL"


def test_compare_ignores_status_by_default():
    a = obj()
    b = obj()
    b["status"]["state"] = "unavailable"
    source = rmv.build_index([a], False, False)
    target = rmv.build_index([b], False, False)
    result = rmv.compare_indexes(source, target)
    assert result.result == "PASS"


def test_compare_reports_configuration_change_path():
    a = obj()
    b = obj(spec_data={"displayName": "different"})
    source = rmv.build_index([a], False, False)
    target = rmv.build_index([b], False, False)
    result = rmv.compare_indexes(source, target)
    assert result.result == "FAIL"
    assert "spec.displayName" in result.changed[0].paths
