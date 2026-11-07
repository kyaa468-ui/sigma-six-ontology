"""Read-only size accounting and comparable-before/after rehearsal checks."""
import json
from pathlib import Path


def summarize_storage(workroot):
    """Measure physical bytes and verify projection dependencies read-only."""
    root = Path(workroot)
    files, errors = [], []
    for path in root.rglob("*"):
        try:
            if path.is_file():
                files.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size})
        except OSError as exc:
            errors.append({"path": str(path.relative_to(root)), "error": str(exc)})
    files.sort(key=lambda row: (-row["bytes"], row["path"]))
    projections = [row for row in files if row["path"].startswith("ontology/trace_view/snapshots/")
                   and row["path"].endswith("/projection.json")]
    blobs = [row for row in files if row["path"].startswith("ontology/snapshots/blobs/")]
    closure, closure_errors = {}, []
    for row in projections:
        path = root / row['path']
        try:
            import snapshot_store
            dependencies = {}
            # This function runs after fixture patches have been restored. An
            # explicit base prevents accidentally reading the operating store.
            snapshot_store.load(path, base=root / 'ontology', dependencies=dependencies)
            for dependency in dependencies:
                value = Path(dependency)
                closure[str(value)] = value.stat().st_size
        except (OSError, ValueError, ImportError) as exc:
            closure_errors.append({'path': row['path'], 'error': str(exc)})
    projection_documents = sum(row['bytes'] for row in projections)
    ontology_bytes = sum(row['bytes'] for row in files if row['path'].startswith('ontology/'))
    return {
        "status": "partial" if errors or closure_errors else "measured",
        "work_root": str(root),
        "total_bytes": sum(row["bytes"] for row in files),
        "file_count": len(files),
        "largest_files": files[:10],
        "ontology_bytes": ontology_bytes,
        "ancillary_bytes": sum(row['bytes'] for row in files) - ontology_bytes,
        "projection_bytes": projection_documents,
        "projection_reference_document_bytes": projection_documents,
        "projection_dependency_bytes": sum(closure.values()) if not closure_errors else None,
        "projection_dependency_count": len(closure) if not closure_errors else None,
        "projection_closure_bytes": projection_documents + sum(closure.values()) if not closure_errors else None,
        "projection_measurement_scope": "Projection documents plus the union of all transitively referenced blobs; shared blobs counted once. This excludes manifest/commit metadata.",
        "projection_dependency_errors": closure_errors,
        "projections": projections,
        "shared_blob_bytes": sum(row["bytes"] for row in blobs),
        "shared_blob_count": len(blobs),
        "measurement_scope": "Current stored file lengths; excludes filesystem allocation and in-memory JSON expansion",
        "storage_finding": {
            "status": "content_addressed_storage_observed" if blobs else "plain_snapshots_observed",
            "summary": "공용 원본은 내용 해시로 저장하고 논리 기록은 읽을 때 복원합니다." if blobs else "공용 스냅샷 파일이 없는 저장 형식입니다.",
            "mechanism": "Physical bytes include unique shared blobs and record references; logical replay values remain complete.",
            "risk": "읽을 때 복원한 논리 JSON의 메모리 크기와 물리 저장 용량은 다릅니다.",
            "interpretation": "기록 연결 성공과 운영 규모의 성능 검증은 별개입니다. 이 측정은 운영 규모 통과 판정이 아닙니다.",
            "original_records_migrated_or_deleted": False,
        },
        "errors": errors,
    }


def compare_rehearsals(report, baseline_path):
    """Compare only deliberately stable outputs, not regenerated IDs/timestamps.

    New collection records and the now-complete portfolio are expected additions.
    They must not silently change candidate order, actual requested/fill quantities,
    judgment coverage, retry sends or stop-monitor call timing.
    """
    path = Path(baseline_path)
    if not path.is_file():
        return {"status": "unavailable", "reason": "baseline_report_missing", "path": str(path)}
    before = json.loads(path.read_text(encoding="utf-8"))
    old, new = before.get("steps", {}).get("1-3", {}), report.get("steps", {}).get("1-3", {})
    checks = {}
    for key in ("prep_candidates", "intraday_candidates", "main_price", "ma20", "partial_fill", "coverage"):
        checks[key] = {"before": old.get(key), "after": new.get(key),
                       "equal": key in old and key in new and old[key] == new[key]}
    for key in ("response_loss", "receipt_loss"):
        a, b = old.get(key, {}).get("dispatches"), new.get(key, {}).get("dispatches")
        checks[key + "_dispatches"] = {"before": a, "after": b, "equal": a is not None and a == b}
    old_monitor = before.get("fault_scenarios", {}).get("monitor", {}).get("current", {})
    new_monitor = report.get("fault_scenarios", {}).get("monitor", {}).get("current", {})
    # Keep the monitor's actual current-output field compatible with older runs.
    if not old_monitor:
        old_monitor = before.get("fault_scenarios", {}).get("monitor", {}).get("after", {})
    if not new_monitor:
        new_monitor = report.get("fault_scenarios", {}).get("monitor", {}).get("after", {})
    if old_monitor and new_monitor:
        a = [x["at"] for x in old_monitor.get("exit_calls", [])]
        b = [x["at"] for x in new_monitor.get("exit_calls", [])]
        checks["monitor_exit_times"] = {"before": a, "after": b, "equal": bool(a) and a == b}
        checks["monitor_retry_state"] = {"before": old_monitor.get("retry_state"),
            "after": new_monitor.get("retry_state"),
            "equal": old_monitor.get("retry_state") == new_monitor.get("retry_state")}
    old_storage = dict(before.get('storage', {}))
    old_root = Path(before.get('work', '')) if before.get('work') else None
    if old_root and old_root.is_dir() and 'ontology_bytes' not in old_storage:
        old_ontology = sum(p.stat().st_size for p in (old_root / 'ontology').rglob('*') if p.is_file())
        old_storage['ontology_bytes'] = old_ontology
        old_storage['ancillary_bytes'] = old_storage['total_bytes'] - old_ontology
    # Older plain snapshots had no blob dependencies; use their full projection
    # document length only when there was no shared store in that saved run.
    if 'projection_closure_bytes' not in old_storage and old_root and not (old_root / 'ontology/snapshots/blobs').exists():
        old_storage['projection_closure_bytes'] = old_storage.get('projection_bytes')
    storage = {}
    for key in ('total_bytes', 'ontology_bytes', 'ancillary_bytes', 'projection_closure_bytes'):
        a, b = old_storage.get(key), report.get('storage', {}).get(key)
        storage[key] = {"before": a, "after": b,
            "reduction_fraction": 1 - b / a if isinstance(a, int) and a > 0 and isinstance(b, int) else None}
    return {"status": "matched" if all(x["equal"] for x in checks.values()) else "mismatch",
            "baseline_report": str(path), "stable_behavior": checks, "storage": storage,
            "baseline_portfolio_status": before.get("steps", {}).get("4-7", {}).get("7", {}).get("portfolio", {}).get("status"),
            "current_portfolio_status": report.get("steps", {}).get("4-7", {}).get("7", {}).get("portfolio", {}).get("status"),
            "timing_before": before.get("phase_timings"), "timing_after": report.get("phase_timings"),
            "scope": "Same fixed original decision behavior; added capture and hypothetical portfolio output are separate."}
