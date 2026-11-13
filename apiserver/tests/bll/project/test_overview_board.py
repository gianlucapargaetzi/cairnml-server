"""The overview board of a project: what a stored card may be."""

import pytest

from apiserver.apierrors import errors
from apiserver.bll.project import overview_board
from apiserver.bll.project.overview_board import validate_board


def card(id_="a", type_="plot", x=0, y=0, w=3, h=12, settings=None):
    return {"id": id_, "type": type_, "x": x, "y": y, "w": w, "h": h, "settings": settings or {}}


def test_a_valid_board_is_accepted_and_returned_with_the_known_fields_only():
    cards = [card("a"), card("b", "divider", x=0, y=12, w=6, h=1), dict(card("c", "agents", x=3), extra="dropped")]
    cleaned = validate_board(cards)
    assert [c["id"] for c in cleaned] == ["a", "b", "c"]
    assert set(cleaned[2]) == {"id", "type", "x", "y", "w", "h", "settings"}


@pytest.mark.parametrize("bad", [
    card(type_="nonsense"),
    card(x=4, w=3),          # x + w > 6
    card(w=0),
    card(h=49),              # taller than 12 tiles
    card(h=6),               # a card is a whole number of tiles high
    card(x=-1),
    card(type_="divider", h=4),   # a divider is one row
    "not an object",
])
def test_a_card_that_does_not_fit_the_grid_or_the_rules_is_refused(bad):
    with pytest.raises(errors.bad_request.ValidationError):
        validate_board([bad])


def test_duplicate_ids_are_refused():
    with pytest.raises(errors.bad_request.ValidationError):
        validate_board([card("a"), card("a", x=3)])


def test_more_than_sixty_cards_are_refused():
    with pytest.raises(errors.bad_request.ValidationError):
        validate_board([card(str(n), y=n * 12) for n in range(61)])


def test_settings_over_four_kilobytes_are_refused():
    with pytest.raises(errors.bad_request.ValidationError):
        validate_board([card(settings={"text": "x" * 5000})])


def test_stored_text_that_is_corrupt_reads_as_no_board():
    assert overview_board.parse_stored("{not json") is None
    assert overview_board.parse_stored('{"cards": 5}') is None
    assert overview_board.parse_stored(None) is None
    assert overview_board.parse_stored(overview_board.dump([card()])) == [card()]
