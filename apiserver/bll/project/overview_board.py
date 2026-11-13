"""The overview board of a project: one shared layout of cards that the owner arranges and everyone on the project sees.

A board is a list of cards on a grid of 6 columns whose rows are quarter tiles (see the web app's board model): ``x``, ``y``, ``w``
and ``h`` are whole numbers of grid cells, ``h`` counted in sub-rows. A card is a whole number of tiles high (``h`` a multiple of
``SUB_ROWS``); a divider is one sub-row, a thin line with a title. The server keeps the board as JSON text and checks only what
makes a board safe to store and to draw: the type, the place on the grid and the size of the data. What a card shows is the
business of the card.
"""

import json
from typing import List, Optional

from apiserver.apierrors import errors

GRID_COLUMNS = 6
SUB_ROWS = 4
MAX_CARDS = 60
MAX_CARD_HEIGHT = 48
MAX_SETTINGS_BYTES = 4096
MAX_BOARD_BYTES = 131072
DIVIDER = "divider"

CARD_TYPES = frozenset({
    "project-state", "recent-tasks", "metric-snapshot", "contributions", "description", "plot", "dataset-content",
    "agents", "repositories", "progress", DIVIDER,
})


def _fail(reason: str):
    raise errors.bad_request.ValidationError(f"the overview board is not valid: {reason}")


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_board(cards) -> List[dict]:
    """The cards as they are stored (known fields only), or a ValidationError that says what is wrong."""
    if not isinstance(cards, list):
        _fail("the cards are not a list")
    if len(cards) > MAX_CARDS:
        _fail(f"more than {MAX_CARDS} cards")
    cleaned, seen = [], set()
    for number, card in enumerate(cards, 1):
        if not isinstance(card, dict):
            _fail(f"card {number} is not an object")
        card_id, card_type = card.get("id"), card.get("type")
        if not isinstance(card_id, str) or not card_id or card_id in seen:
            _fail(f"card {number} has no id, or its id is used twice")
        seen.add(card_id)
        if card_type not in CARD_TYPES:
            _fail(f"card {card_id} has an unknown type")
        x, y, w, h = (card.get(key) for key in ("x", "y", "w", "h"))
        if not all(_whole(value) for value in (x, y, w, h)):
            _fail(f"card {card_id} has a place or size that is not a whole number")
        if x < 0 or y < 0 or w < 1 or x + w > GRID_COLUMNS:
            _fail(f"card {card_id} does not fit the {GRID_COLUMNS} columns")
        if card_type == DIVIDER:
            if h != 1:
                _fail(f"the divider {card_id} must be one row high")
        elif h < SUB_ROWS or h > MAX_CARD_HEIGHT or h % SUB_ROWS:
            _fail(f"card {card_id} must be a whole number of tiles high, at most {MAX_CARD_HEIGHT // SUB_ROWS}")
        settings = card.get("settings", {})
        if not isinstance(settings, dict):
            _fail(f"the settings of card {card_id} are not an object")
        if len(json.dumps(settings).encode()) > MAX_SETTINGS_BYTES:
            _fail(f"the settings of card {card_id} are over {MAX_SETTINGS_BYTES} bytes")
        cleaned.append({"id": card_id, "type": card_type, "x": x, "y": y, "w": w, "h": h, "settings": settings})
    if len(dump(cleaned).encode()) > MAX_BOARD_BYTES:
        _fail(f"the board is over {MAX_BOARD_BYTES} bytes")
    return cleaned


def dump(cards: List[dict]) -> str:
    return json.dumps({"cards": cards}, separators=(",", ":"))


def parse_stored(text: Optional[str]) -> Optional[List[dict]]:
    """The cards of a stored board, or None when there is none or what is stored is not a valid board any more."""
    if not text:
        return None
    try:
        cards = json.loads(text).get("cards")
        return validate_board(cards)
    except (ValueError, AttributeError, errors.bad_request.ValidationError):
        return None
