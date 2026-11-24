"""Offline metrics for independently annotated evaluation bundles.

Only candidate-level prediction fields and annotation fields are compared. No
network access or pipeline execution occurs here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation_bundle import BundleValidationError, read_bundle


def _ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _classification(rows: list[tuple[bool, bool]]) -> dict[str, Any]:
    tp = sum(p and y for p, y in rows)
    fp = sum(p and not y for p, y in rows)
    fn = sum(not p and y for p, y in rows)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = _ratio(2 * precision * recall, precision + recall) if precision is not None and recall is not None else None
    status = "unavailable: no assessable labels" if not rows else ("descriptive only: fewer than 10 assessable labels" if len(rows) < 10 else "available")
    return {"status": status,
            "sample_count": len(rows), "denominator": len(rows),
            "precision_denominator": tp + fp, "recall_denominator": tp + fn,
            "true_positive": tp,
            "false_positive": fp, "false_negative": fn, "precision": precision,
            "recall": recall, "f1": f1}


def evaluate_bundle(bundle: dict, *, name: str = "bundle") -> dict[str, Any]:
    """Evaluate predictions against independent assessable annotations only."""
    candidates = bundle["candidates"]
    category_rows: dict[str, list[tuple[bool, bool]]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    prediction_missing: dict[str, int] = {}
    unassessed_predictions = 0
    scoped_categories = bundle["scope"]["selected_categories"]
    for c in candidates:
        anns = c["annotations"]
        preds = c["pipeline_predictions"]
        raw_predicted = preds.get("categories")
        prediction_valid = isinstance(raw_predicted, list) and all(isinstance(x, str) for x in raw_predicted)
        predicted = raw_predicted if prediction_valid else []
        labels = anns["categories"]
        if prediction_valid:
            unassessed_predictions += sum(category not in labels or labels[category] in {"unknown", "not_assessable"}
                                          for category in set(predicted))
        for category in scoped_categories:
            category_counts.setdefault(category, {"relevant": 0, "not_relevant": 0, "unknown": 0, "not_assessable": 0, "unannotated": 0})
            if category not in labels:
                category_counts[category]["unannotated"] += 1
        for category, label in labels.items():
            counts = category_counts.setdefault(category, {"relevant": 0, "not_relevant": 0, "unknown": 0, "not_assessable": 0, "unannotated": 0})
            counts[label] += 1
            if label in {"relevant", "not_relevant"} and prediction_valid:
                category_rows.setdefault(category, []).append((category in predicted, label == "relevant"))
            elif label in {"relevant", "not_relevant"}:
                prediction_missing[category] = prediction_missing.get(category, 0) + 1
    metrics = {k: _classification(category_rows.get(k, [])) for k in sorted(category_counts)}
    metrics["micro"] = _classification([pair for rows in category_rows.values() for pair in rows])
    for category, metric in metrics.items():
        metric["prediction_unavailable_count"] = prediction_missing.get(category, sum(prediction_missing.values()) if category == "micro" else 0)
    total_ann = sum(sum(x[state] for state in ("relevant", "not_relevant", "unknown", "not_assessable")) for x in category_counts.values())
    known = sum(v["relevant"] + v["not_relevant"] for v in category_counts.values())
    unannotated = sum(v["unannotated"] for v in category_counts.values())
    state_counts = {}
    for field in ("geographic_relevance", "primary_source", "evidence_sufficiency", "corroboration", "source_identity", "syndication", "assessability"):
        counts = {"denominator": len(candidates), "unknown": 0, "not_assessable": 0}
        for c in candidates:
            state = c["annotations"][field]
            if state in counts:
                counts[state] += 1
        counts["unknown_rate"] = _ratio(counts["unknown"], counts["denominator"])
        counts["not_assessable_rate"] = _ratio(counts["not_assessable"], counts["denominator"])
        state_counts[field] = counts
    # Duplicate handling is measurable only where multiple candidates share an
    # independently assigned story group and predictions supply the same group.
    group_candidates: dict[str, list[dict]] = {}
    for c in candidates:
        group_candidates.setdefault(c["annotations"]["story_group_id"], []).append(c)
    dup_groups = {k: v for k, v in group_candidates.items() if len(v) > 1}
    pred_dup_supported = all(isinstance(c["pipeline_predictions"].get("story_group_id"), str) for v in dup_groups.values() for c in v)
    duplicate = {"status": "available" if dup_groups and pred_dup_supported else "unavailable: duplicate prediction labels or repeated annotated story groups missing",
                 "annotated_duplicate_groups": len(dup_groups), "candidate_denominator": sum(map(len, dup_groups.values()))}
    if duplicate["status"] == "available":
        duplicate["grouping_consistency"] = sum(len({c["pipeline_predictions"]["story_group_id"] for c in v}) == 1 for v in dup_groups.values()) / len(dup_groups)
    else:
        duplicate["grouping_consistency"] = None
    return {
        "bundle": name, "schema_version": bundle["schema_version"], "candidate_count": len(candidates),
        "category_metrics": metrics,
        "category_annotation_counts": {k: category_counts[k] for k in sorted(category_counts)},
        "annotation_coverage": {"category_label_count": total_ann, "assessable_category_labels": known,
                                "unknown_or_not_assessable_category_labels": total_ann-known,
                                "category_label_coverage": _ratio(known, total_ann),
                                "unannotated_category_labels": unannotated,
                                "candidate_category_slots": total_ann + unannotated,
                                "other_annotation_field_states": state_counts},
        "duplicate_handling": {"status": duplicate["status"],
                               "annotated_duplicate_groups": duplicate["annotated_duplicate_groups"],
                               "candidate_denominator": duplicate["candidate_denominator"],
                               "grouping_consistency": duplicate["grouping_consistency"]},
        "unassessed_predicted_category_assignments": unassessed_predictions,
        "shortlist_relevance": {"status": "unavailable: schema has no independent shortlist relevance grade or explicit selected-item field"},
        "ranking_quality": {"status": "unavailable: schema has no independent graded relevance labels"},
        "evidence_verification": {"status": "unavailable: no validated prediction-to-annotation field mapping; annotation state counts are reported above"},
        "limitations": ["Metrics compare only pipeline_predictions.categories with independent relevant/not_relevant category annotations.",
                        "Unknown, not_assessable, and unannotated category values are excluded from precision/recall denominators and reported separately.",
                        "No metric establishes future virality, audience engagement, monetization, or factual accuracy beyond retained evidence and annotations.",
                        "Per-category precision/recall/F1 with fewer than 10 assessable labels are descriptive only; no population-level claim is justified."],
    }


def evaluate_paths(paths: list[str | Path]) -> dict[str, Any]:
    if not paths:
        raise BundleValidationError("provide at least one --bundle path")
    results = []
    for path in paths:
        try:
            bundle = read_bundle(path)
        except (OSError, UnicodeError) as exc:
            raise BundleValidationError(f"cannot read bundle {path}: {exc}") from exc
        results.append(evaluate_bundle(bundle, name=Path(path).name))
    return {"evaluation": "offline_research_evaluation", "bundle_count": len(results), "bundles": results}


def report(data: dict[str, Any]) -> str:
    lines = ["Offline research evaluation (independent annotations only)", f"Bundles: {data['bundle_count']}"]
    for b in data["bundles"]:
        lines += ["", f"{b['bundle']}: {b['candidate_count']} candidates"]
        for category, m in b["category_metrics"].items():
            if category == "micro" or category in b["category_annotation_counts"]:
                lines.append(f"  {category}: n={m['denominator']} precision={m['precision']} recall={m['recall']} F1={m['f1']} ({m['status']})")
        cov = b["annotation_coverage"]
        lines.append(f"  Category annotation coverage: {cov['assessable_category_labels']}/{cov['category_label_count']}")
        lines.append(f"  Shortlist relevance: {b['shortlist_relevance']['status']}")
        lines.append(f"  Ranking: {b['ranking_quality']['status']}")
        lines.append(f"  Duplicate handling: {b['duplicate_handling']['status']}")
        lines.append(f"  Predicted category assignments without assessable reviewer labels: {b['unassessed_predicted_category_assignments']}")
        lines.extend(f"  Limitation: {item}" for item in b["limitations"])
    return "\n".join(lines)


def dumps_report(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
