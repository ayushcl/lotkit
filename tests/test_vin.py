import pytest

from api.vin import is_valid_vin, normalize_vin


@pytest.mark.parametrize(
    "vin",
    [
        "1HGCM82633A004352",
        "11111111111111111",
        "1FTFW1ET9DFC10312",
    ],
)
def test_known_good_vins(vin: str) -> None:
    assert is_valid_vin(vin)


@pytest.mark.parametrize(
    "vin",
    [
        "1HGCM82633A00435",
        "1HGCM82633A0043520",
        "1HGCM82633AI04352",
        "1HGCM82633AO04352",
        "1HGCM82633AQ04352",
        "1HGCM82643A004352",
        "1FTFW1ET5DFC10312",
        "1hgcm82633a004352",
    ],
)
def test_invalid_vins(vin: str) -> None:
    assert not is_valid_vin(vin)


def test_normalize_vin() -> None:
    assert normalize_vin("  1hgcm82633a004352 \n") == "1HGCM82633A004352"
