"""Find failure-injection patches whose mock is never called.

A test that patches something with an exception `side_effect` and never reaches
it asserts nothing: it passes because the failure never happened, not because
the code survived it. Two such tests were found this way — one claiming to cover
the #136 post-write cleanup regression, one claiming to cover a Fakturownia
auto-download that the module under test never performs.

Run as a pytest plugin over the suite:

    .venv/bin/python -m pytest tests/ -q -s -p no:cacheprovider \
        -p scripts.quality.find_vacuous_patches

Every hit is one of two things, and only a person can tell them apart:

* a vacuous test — the failure never reached the code, so nothing was verified;
* a deliberate tripwire — `side_effect=AssertionError` meaning "being called at
  all is the regression". Those are correct and should say so in a comment.

Not wired into the quality gate: it is a periodic audit, and turning it into a
blocking check would fail on the legitimate tripwires.
"""

from __future__ import annotations

import unittest.mock as mock

_current = ["?"]
_live: list = []
_orig_enter = mock._patch.__enter__
_orig_exit = mock._patch.__exit__


def _is_failure(value: object) -> bool:
    return isinstance(value, BaseException) or (
        isinstance(value, type) and issubclass(value, BaseException)
    )


def _enter(self):
    result = _orig_enter(self)
    if _is_failure(getattr(self, "kwargs", {}).get("side_effect")):
        _live.append((self, result, _current[0], f"{self.target}.{self.attribute}"))
    return result


def _exit(self, *exc):
    for entry in list(_live):
        patcher, mock_obj, node, label = entry
        if patcher is self:
            if getattr(mock_obj, "called", True) is False:
                print(f"VACUOUS::{node}::{label}")
            _live.remove(entry)
    return _orig_exit(self, *exc)


mock._patch.__enter__ = _enter
mock._patch.__exit__ = _exit


def pytest_runtest_logstart(nodeid, location):
    del location
    _current[0] = nodeid
