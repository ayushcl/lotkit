import re

VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
POSITION_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
LETTER_VALUES = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
}


def normalize_vin(vin: str) -> str:
    return vin.strip().upper()


def is_valid_vin(vin: str) -> bool:
    if not VIN_PATTERN.fullmatch(vin):
        return False

    total = 0
    for character, weight in zip(vin, POSITION_WEIGHTS, strict=True):
        value = int(character) if character.isdigit() else LETTER_VALUES[character]
        total += value * weight

    remainder = total % 11
    expected_check_digit = "X" if remainder == 10 else str(remainder)
    return vin[8] == expected_check_digit
