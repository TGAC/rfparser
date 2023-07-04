from html.parser import HTMLParser
from io import StringIO


class MLStripper(HTMLParser):
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
