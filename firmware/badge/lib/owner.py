"""Badge owner profile, read from /data/owner.json.

    {"maker_handle": "740Dave", "makes": "robots and blinky things"}

maker_handle is a handle or nickname you are happy to show (and possibly
broadcast) in public. It is NOT meant to hold your real or full name; the
badge is worn in a crowd, so pick something you don't mind strangers seeing.

Any app can use it:

    from lib.owner import owner
    owner.maker_handle   # "" if not set
    owner.makes          # "" if not set
    owner.get("some_other_key", default)
    owner.reload()       # re-read the file after it changed

This module only ever reads the file. A missing, unreadable or invalid file
just means "no profile": the text fields are empty strings, never None, and
nothing raises. Other keys may be present in the file; anything that rewrites
it should keep the keys it doesn't know about.
"""

import json

OWNER_FILE = "/data/owner.json"


def printable(value) -> str:
    """Return value as one line of printable text ("" if it isn't a string).

    Control characters are dropped and runs of whitespace (including
    newlines and tabs) become a single space. Note the built-in LVGL
    Montserrat fonts only have glyphs for printable ASCII.
    """
    if not isinstance(value, str):
        return ""
    out = []
    for ch in value:
        o = ord(ch)
        if ch in "\t\r\n":
            out.append(" ")
        elif o >= 32 and not (127 <= o < 160):
            out.append(ch)
    return " ".join("".join(out).split())


class Owner:
    def __init__(self, filename: str = OWNER_FILE) -> None:
        self.filename = filename
        self.reload()

    def reload(self) -> None:
        """(Re-)read the profile file. Never raises."""
        data = {}
        try:
            with open(self.filename) as f:
                data = json.load(f)
        except Exception:
            # OSError (no file), ValueError (bad JSON), ...
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._data = data
        self.maker_handle = printable(data.get("maker_handle"))
        self.makes = printable(data.get("makes"))

    def get(self, key: str, default=None):
        """Raw value of any key in the file, or default."""
        return self._data.get(key, default)


owner = Owner()
