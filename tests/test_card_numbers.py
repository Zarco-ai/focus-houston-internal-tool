"""Card numbers.

A mother's card number is the only thing that ties a Google Form response to
her. These tests prove the format does what the paper process relies on: a
slip of the thumb is caught, never silently turned into a different mother.

The "every" tests are exhaustive over all 9,000 possible numbers, not
sampled -- the whole space is small enough to check outright.
"""

import random

import pytest

from app.services import card_numbers
from app.services.card_numbers import CardNumberError

ALL_CODES = [
    card_numbers.make_code(base)
    for base in range(1000, 10000)
]


def test_check_digit_matches_the_published_damm_example():
    """Damm's own worked example: 572 -> 4. If this fails the table is wrong."""
    assert card_numbers.check_digit("572") == "4"
    assert card_numbers._interim("5724") == 0


def test_every_generated_code_is_five_digits_and_valid():
    for code in ALL_CODES:
        assert len(code) == 5 and code.isdigit()
        assert card_numbers.is_valid(code), code


def test_a_card_number_never_starts_with_zero():
    """Google Sheets drops leading zeros; "04827" would arrive as "4827"."""
    assert all(code[0] != "0" for code in ALL_CODES)
    assert not card_numbers.is_valid("04827")


def test_every_single_mistyped_digit_is_caught():
    """One wrong digit anywhere, in any of the 9,000 numbers, fails
    validation. So one slip can never land on another real mother."""
    for code in ALL_CODES:
        for position in range(5):
            for wrong in "0123456789":
                if wrong == code[position]:
                    continue
                typo = code[:position] + wrong + code[position + 1:]
                assert not card_numbers.is_valid(typo), (code, typo)


def test_every_swap_of_two_neighbouring_digits_is_caught():
    for code in ALL_CODES:
        for position in range(4):
            a, b = code[position], code[position + 1]
            if a == b:
                continue  # swapping identical digits changes nothing
            swapped = code[:position] + b + a + code[position + 2:]
            assert not card_numbers.is_valid(swapped), (code, swapped)


def test_two_valid_codes_never_differ_in_only_one_digit():
    """The same property stated the other way round: the nearest other valid
    number is always at least two digits away."""
    valid = set(ALL_CODES)
    for code in ALL_CODES:
        for position in range(5):
            for other in "0123456789":
                neighbour = code[:position] + other + code[position + 1:]
                if neighbour != code:
                    assert neighbour not in valid


@pytest.mark.parametrize("typed", ["4827-3", " 48273 ", "48 273", "4827.3"])
def test_spaces_and_dashes_a_mother_might_type_are_ignored(typed):
    code = card_numbers.make_code(4827)
    assert card_numbers.normalize(typed.replace("3", code[-1])) == code


@pytest.mark.parametrize("junk", [None, "", "abcde", "4827", "482730", "4827x", "12345"])
def test_anything_that_is_not_a_card_number_is_rejected(junk):
    """12345 is five digits but its check digit is wrong."""
    assert not card_numbers.is_valid(junk)


def test_generate_returns_exactly_the_count_asked_for_with_no_duplicates():
    codes = card_numbers.generate(200, rng=random.Random(1))
    assert len(codes) == 200
    assert len(set(codes)) == 200
    assert all(card_numbers.is_valid(code) for code in codes)


def test_a_second_batch_never_reuses_a_number_from_the_first():
    first = card_numbers.generate(200, rng=random.Random(1))
    second = card_numbers.generate(200, existing=first, rng=random.Random(1))
    assert not set(first) & set(second)


def test_generate_refuses_when_the_format_runs_out_rather_than_looping_forever():
    with pytest.raises(CardNumberError):
        card_numbers.generate(card_numbers.POSSIBLE_CODES + 1)

    almost_all = ALL_CODES[:-3]
    assert len(card_numbers.generate(3, existing=almost_all)) == 3
    with pytest.raises(CardNumberError):
        card_numbers.generate(4, existing=almost_all)


def test_generate_refuses_a_count_below_one():
    with pytest.raises(CardNumberError):
        card_numbers.generate(0)


def test_numbers_are_not_handed_out_in_order():
    """The order on the list carries no meaning -- a number does not reveal
    how many mothers are enrolled or who came first."""
    codes = card_numbers.generate(200, rng=random.Random(7))
    assert codes != sorted(codes)
