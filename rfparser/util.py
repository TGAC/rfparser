import re
from collections.abc import Iterable
from html.parser import HTMLParser
from io import StringIO
from typing import (
    Any,
    Optional,
    TypeVar,
    Union,
)

NAME_SPLITTER_PATTERN = re.compile(r"[\s-]+")


class MLStripper(HTMLParser):
    """
    HTML parser that finds tags and strips markup.
    """

    # Copied from https://stackoverflow.com/a/925630
    def __init__(self) -> None:
        super().__init__()
        self.text = StringIO()

    def handle_data(self, data: str) -> None:
        self.text.write(data)

    def get_data(self) -> str:
        return self.text.getvalue()


def strip_tags(html: str) -> str:
    """
    Strip HTML tags from a string.
    """
    # Copied from https://stackoverflow.com/a/925630
    s = MLStripper()
    s.feed(html)
    return s.get_data()


def str_if_not_None(s: Any) -> Optional[str]:
    """
    Cast a variable to str if it's not None.
    """
    return str(s) if s is not None else None


T = TypeVar("T")


def unique(l_: Iterable[T]) -> list[T]:
    """
    Return a list with the unique elements of an iterable.

    Similar to using set(), but preserving the order of the elements in the
    iterable.
    """
    return list(dict.fromkeys(l_))


def is_same_person(family_names1: str, given_names1: str, family_names2: str, given_names2: str) -> bool:
    """
    Check whether two persons' family and given names are similar enough to be
    considered the same person.
    """
    assert family_names1
    assert family_names2
    family_names1_list = [name.rstrip(".").lower() for name in NAME_SPLITTER_PATTERN.split(family_names1)]
    family_names2_list = [name.rstrip(".").lower() for name in NAME_SPLITTER_PATTERN.split(family_names2)]
    for name1, name2 in zip(family_names1_list, family_names2_list):
        if name1 != name2:
            return False
    if not given_names1 and not given_names2:
        return True
    if not given_names1 or not given_names2:
        return False
    given_names1_list = [name.rstrip(".").lower() for name in NAME_SPLITTER_PATTERN.split(given_names1)]
    given_names2_list = [name.rstrip(".").lower() for name in NAME_SPLITTER_PATTERN.split(given_names2)]
    for name1, name2 in zip(given_names1_list, given_names2_list):
        if name1 == name2:
            continue
        if name1[0] == name2:
            continue
        if name1 == name2[0]:
            continue
        return False
    return True


def extend_list_to_size(t: list[T], size: int) -> list[Union[None, T]]:
    """
    Extend a list with ``None``s if it is shorter than the requested size.
    """
    return t + [None] * (size - len(t))
