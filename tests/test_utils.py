from rfparser.util import (
    is_same_person,
    unique,
)


def test_is_same_person():
    assert is_same_person("Doe", "John", "Doe", "John")
    assert not is_same_person("Doe", "John", "Doe", "Mary")
    assert is_same_person("Doe", "John-Paul", "Doe", "John Paul")
    assert is_same_person("Doe", "John-Paul", "Doe", "John  Paul")
    assert is_same_person("Doe", "John-Paul", "Doe", "John P")
    assert is_same_person("Doe", "John-Paul", "Doe", "J P")
    assert is_same_person("Doe", "John-Paul", "Doe", "J-P")
    assert is_same_person("Doe", "John-Paul", "Doe", "J")
    assert is_same_person("Doe", "John-Paul", "Doe", "J.")
    assert not is_same_person("Doe", "John", "D", "J")
    assert is_same_person("Doe", "John Jr.", "Doe", "John Jr")
    assert is_same_person("Foo-Bar", "John", "Foo Bar", "John")
    assert not is_same_person("Foo-Bar", "John", "Foo Doe", "John")
    assert not is_same_person("Foo-Bar", "John", "Foo-Bar", "Mary")
    assert is_same_person("Foo-Bar", "John", "Foo", "John")
    assert is_same_person("Foo Bar", "John", "Foo", "John")
    assert is_same_person("McFoo", "John", "Mcfoo", "John")
    assert is_same_person("Doe", "John", "Doe", "john")
    assert is_same_person("Doe", "", "Doe", "")
    assert not is_same_person("Doe", "John", "Doe", "")


def test_unique():
    assert unique([]) == []
    assert unique(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert unique([3, 2, 2, 1, 3]) == [3, 2, 1]
