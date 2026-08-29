import unicodedata

_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def is_safe_local_component_name(value: str) -> bool:
    """Return whether value is safe to use as one local project directory name."""
    if not value or value in {".", ".."}:
        return False
    if any(
        character in _INVALID_COMPONENT_CHARACTERS or unicodedata.category(character) == "Cc"
        for character in value
    ):
        return False
    if value.endswith((" ", ".")):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES
