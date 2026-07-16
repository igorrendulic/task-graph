"""Domain records shared by future schema and presentation work."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Record:
    """A customer record before it is serialized or formatted."""

    identifier: str
    display_name: str
    attributes: dict[str, str] = field(default_factory=dict)


def new_record(identifier: str, display_name: str) -> Record:
    """Create a record with the stable fields used by independent features."""
    if not identifier:
        raise ValueError("identifier is required")
    if not display_name:
        raise ValueError("display_name is required")
    return Record(identifier=identifier, display_name=display_name)
