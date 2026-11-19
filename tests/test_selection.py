import pytest

from wrally.commands.common import letter, parse_shots
from wrally.errors import PreflightError

KEYS = ["00", "01", "02", "03", "05", "07", "99"]


@pytest.mark.parametrize(("spec", "expected"), [
    ("all", KEYS),
    ("02,05", ["02", "05"]),
    ("05, 02", ["02", "05"]),  # 按清单顺序返回
    ("02-07", ["02", "03", "05", "07"]),
    ("2", ["02"]),
    ("00,03-05,99", ["00", "03", "05", "99"]),
])  # fmt: skip
def test_shots_selector(spec, expected):
    assert parse_shots(spec, KEYS) == expected


@pytest.mark.parametrize("spec", ["08", "02,xx", "04-07", "07-02", ""])
def test_unknown_shot_is_a_preflight_error(spec):
    with pytest.raises(PreflightError) as e:
        parse_shots(spec, KEYS)
    assert e.value.code == "unknown_shot" and e.value.exit_code == 2


def test_range_follows_manifest_order_with_subshots_and_large_numbers():
    keys = ["00", "02", "02a", "02b", "12", "03", "98", "99", "100", "101"]
    assert parse_shots("2a-3", keys) == ["02a", "02b", "12", "03"]
    assert parse_shots("02-03", keys) == ["02", "02a", "02b", "12", "03"]
    assert parse_shots("98-101", keys) == ["98", "99", "100", "101"]
    assert parse_shots("2b", keys) == ["02b"]


def test_candidate_letters():
    assert [letter(i) for i in (0, 1, 25, 26, 27)] == ["A", "B", "Z", "AA", "AB"]
