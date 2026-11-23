# SPDX-License-Identifier: Apache-2.0
"""A partial, duplicated or mixed-binary nightly must never qualify an installer."""

import runpy

import pytest

from support.paths import ROOT

pytestmark = pytest.mark.pure
runner = runpy.run_path(str(ROOT / "scripts/run-nightly.py"))


def test_groups_preserve_all_cases_and_dependencies(tmp_path):
    plan = runner["plan_for"](tmp_path)
    groups = runner["partition"](plan, 3)
    owners = {name: index for index, names in enumerate(groups) for name in names}
    assert sum(len(names) for names in groups) == len(owners)
    assert {case["test"] for case in plan} == owners.keys()
    for case in runner["read"](ROOT / "tests/test-catalogue.json")["headless"]:
        assert all(owners[producer] == owners[case["test"]] for producer in case["depends_on"])


@pytest.mark.parametrize("fault", ["missing", "duplicate", "different_manifest", "failed"])
def test_bad_partitions_cannot_pass(tmp_path, fault):
    plan = [dict(test=name, device="CPU") for name in ("one.py", "two.py")]
    summary = dict(plan=plan, build_manifest_sha256="a" * 64)
    parts = [
        (
            tmp_path,
            dict(
                result="passed",
                build_manifest_sha256="a" * 64,
                runtime_identity={},
                plan=[case],
                tests=[dict(case, result="passed")],
            ),
        )
        for case in plan
    ]
    if fault == "missing":
        parts.pop()
    elif fault == "duplicate":
        parts[1] = parts[0]
    elif fault == "different_manifest":
        parts[1][1]["build_manifest_sha256"] = "b" * 64
    else:
        parts[1][1]["tests"][0]["result"] = "failed"
        assert not runner["aggregate"](tmp_path, parts, summary)
        return
    with pytest.raises(ValueError):
        runner["aggregate"](tmp_path, parts, summary)


@pytest.mark.parametrize("partition_digest", ["ab12cd" * 10 + "a1b2", "AB12CD" * 10 + "A1B2", "Ab12cD" * 10 + "a1B2"])
def test_manifest_digest_letter_case_does_not_split_the_same_build(tmp_path, partition_digest):
    case = dict(test="one.py", device="CPU")
    part = dict(
        result="passed",
        build_manifest_sha256=partition_digest,
        runtime_identity={},
        plan=[case],
        tests=[dict(case, result="passed")],
    )
    summary = dict(plan=[case], build_manifest_sha256="AB12CD" * 10 + "A1B2")
    assert runner["aggregate"](tmp_path, [(tmp_path, part)], summary)


@pytest.mark.parametrize("bad_digest", [None, 123, "", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n"])
@pytest.mark.parametrize("field", ["partition", "summary"])
def test_malformed_manifest_digest_never_qualifies(tmp_path, bad_digest, field):
    case = dict(test="one.py", device="CPU")
    part = dict(
        result="passed",
        build_manifest_sha256="a" * 64,
        runtime_identity={},
        plan=[case],
        tests=[dict(case, result="passed")],
    )
    summary = dict(plan=[case], build_manifest_sha256="a" * 64)
    (part if field == "partition" else summary)["build_manifest_sha256"] = bad_digest
    with pytest.raises(ValueError, match="Malformed build manifest"):
        runner["aggregate"](tmp_path, [(tmp_path, part)], summary)


def test_aggregate_rebases_private_fixture_root_into_full_run(tmp_path):
    run = tmp_path / "nightly"
    directory = run / "part-1" / "receipt"
    fixture = directory / "private-fixtures/001-consumer-CPU"
    fixture.mkdir(parents=True)
    (fixture / "scene.hiplc").write_bytes(b"verified scene")
    case = dict(test="consumer.py", device="CPU")
    entry = dict(
        case,
        result="passed",
        fixture_root="private-fixtures/001-consumer-CPU",
        producer_inputs=[dict(path="scene.hiplc", sha256="retained")],
    )
    part = dict(result="passed", build_manifest_sha256="a" * 64, runtime_identity={}, plan=[case], tests=[entry])
    summary = dict(plan=[case], build_manifest_sha256="a" * 64)
    assert runner["aggregate"](run, [(directory, part)], summary)
    rebased = summary["tests"][0]
    assert (run / rebased["fixture_root"] / rebased["producer_inputs"][0]["path"]).read_bytes() == b"verified scene"
    assert rebased["fixture_root"] == "part-1/receipt/private-fixtures/001-consumer-CPU"
    assert part["tests"][0]["fixture_root"] == "private-fixtures/001-consumer-CPU"


def test_aggregate_rejects_private_fixture_escape(tmp_path):
    run = tmp_path / "nightly"
    directory = run / "part-1"
    directory.mkdir(parents=True)
    case = dict(test="consumer.py", device="CPU")
    entry = dict(case, result="passed", fixture_root="../outside")
    part = dict(result="passed", build_manifest_sha256="a" * 64, runtime_identity={}, plan=[case], tests=[entry])
    with pytest.raises(ValueError, match="private fixture"):
        runner["aggregate"](run, [(directory, part)], dict(plan=[case], build_manifest_sha256="a" * 64))


@pytest.mark.parametrize("source_kind", ["private", "shared", "escape", "wrong-shared"])
def test_aggregate_rebases_chain_sources_without_mutating_partition_receipts(tmp_path, source_kind):
    run = tmp_path / "nightly"
    directory = run / "part-1" / "receipt"
    fixture = directory / "private-fixtures/001-intermediate-CPU"
    fixture.mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    source = {
        "private": "private-fixtures/001-intermediate-CPU",
        "shared": str(shared),
        "escape": "../outside",
        "wrong-shared": str(tmp_path / "another-shared"),
    }[source_kind]
    case = dict(test="downstream.py", device="CPU")
    entry = dict(case, result="passed", producer_inputs=[dict(path="derived.hiplc", source_root=source)])
    part = dict(
        result="passed",
        build_manifest_sha256="a" * 64,
        runtime_identity={},
        fixture_artifacts=str(shared),
        plan=[case],
        tests=[entry],
    )
    summary = dict(plan=[case], build_manifest_sha256="a" * 64)
    if source_kind in ("escape", "wrong-shared"):
        with pytest.raises(ValueError, match="producer source"):
            runner["aggregate"](run, [(directory, part)], summary)
    else:
        assert runner["aggregate"](run, [(directory, part)], summary)
        rebased = summary["tests"][0]["producer_inputs"][0]["source_root"]
        assert rebased == ("part-1/receipt/" + source if source_kind == "private" else source)
    assert part["tests"][0]["producer_inputs"][0]["source_root"] == source
