from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_fully_recovered_500_screening.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_fully_recovered_500_screening", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_derive_sets_is_audited_disjoint_500_plus_142() -> None:
    module = load_module()
    rows, validation, partial = module.derive_sets()
    assert validation["ok"] is True
    assert len(rows) == len({row["template_id"] for row in rows}) == 500
    assert len(partial["phase1"]) == 60
    assert len(partial["phase2"]) == 82
    assert not ({row["template_id"] for row in rows} & set(partial["combined"]))


def test_duration_balancing_produces_ten_fifty_item_batches_under_180_seconds() -> None:
    module = load_module()
    rows, _validation, _partial = module.derive_sets()
    queue = module.assign_batches(rows)
    assert len(queue) == 500
    for batch in range(1, 11):
        selected = [row for row in queue if row["batch"] == batch]
        assert len(selected) == 50
        assert selected[-1]["screening_end_us"] <= 180_000_000
        assert [row["batch_order"] for row in selected] == list(range(1, 51))
        assert all(row["duration_us"] <= module.MAX_TEMPLATE_DURATION_US for row in selected)


def test_screening_split_defers_embedded_media_and_caps_sampling_window() -> None:
    module = load_module()
    rows, validation, _partial = module.derive_sets()
    screenable = [row for row in rows if row["screening_status"] == "screenable"]
    deferred = [row for row in rows if row["screening_status"] == "deferred_embedded_media"]

    assert validation["checks"]["screenable_embedded_media_count"] is True
    assert validation["checks"]["deferred_embedded_media_count"] is True
    assert len(screenable) + len(deferred) == len(rows)
    assert not ({row["template_id"] for row in screenable} & {row["template_id"] for row in deferred})
    order32 = next(row for row in deferred if row["template_id"] == "JIANYING-25-220")
    assert any(
        item.get("missing_kind") == "empty_path_compound_clip"
        or item.get("material_name") == "透明"
        for item in order32["embedded_media_evidence"]
    )
    order592 = next(row for row in deferred if row["template_id"] == "JIANYING-25-592")
    assert any(
        item.get("missing_kind") == "unavailable_imported_fragment_visual_path"
        and item.get("path")
        for item in order592["embedded_media_evidence"]
    )

    queue = module.assign_batches(screenable)
    assert len(queue) == len(screenable)
    expected_batches = max(1, (len(screenable) + module.BATCH_SIZE - 1) // module.BATCH_SIZE)
    assert max(row["batch"] for row in queue) == expected_batches
    expected_sizes = [module.BATCH_SIZE] * (expected_batches - 1)
    expected_sizes.append(len(screenable) - module.BATCH_SIZE * (expected_batches - 1))
    assert [sum(row["batch"] == batch for row in queue) for batch in range(1, expected_batches + 1)] == expected_sizes
    assert all(row["duration_us"] <= module.MAX_TEMPLATE_DURATION_US for row in queue)
    assert all(
        sum(row["batch"] == batch for row in queue) <= module.BATCH_SIZE
        for batch in range(1, expected_batches + 1)
    )


def test_screening_batches_2_to_5_have_independent_selection_and_ids() -> None:
    module = load_module()
    # This tests batching and identities, not the mutable installed-media count.
    # Current inventory partitioning is tested separately above.
    screenable = [{"template_id": f"fixture-{index}", "source_duration_us": 1_000_000}
                  for index in range(242)]
    queue = {"batch_count": 5, "items": module.assign_batches(screenable)}
    batches = {
        batch: [row["template_id"] for row in queue["items"] if row["batch"] == batch]
        for batch in range(1, 6)
    }
    assert [len(batches[batch]) for batch in range(1, 6)] == [50, 50, 50, 50, 42]
    assert not set(batches[1]).intersection(batches[2])
    assert len({tid for ids in batches.values() for tid in ids}) == len(screenable)
    assert module.deterministic_id_factory(2)() != module.deterministic_id_factory(3)()
    assert module.deterministic_id_factory(2)() == module.deterministic_id_factory(2)()
    assert module.screening_draft_name(2, 50) == "字幕预设500款筛选_第02批_50款"
    assert module.screening_draft_name(5, 42) == "字幕预设500款筛选_第05批_42款"
