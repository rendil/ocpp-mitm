"""Whole-token rewrites applied to every message that crosses the proxy."""

import re
from dataclasses import dataclass
from functools import cached_property


def _token(text: str) -> re.Pattern:
    # Whole tokens only: 192.168.1.5 must not match inside 192.168.1.50 or 10.192.168.1.5.
    return re.compile(rf"(?<![\w.]){re.escape(text)}(?!\w|\.\d)")


@dataclass(frozen=True)
class Rewriter:
    """Pairs of (charger-side text, controller-side text).

    Text going to the controller has the charger-side value swapped for the
    controller-side one; text going to the charger gets the reverse swap, so
    the charger never sees the value it did not send.
    """

    pairs: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, specs: list[str]) -> "Rewriter":
        pairs = []
        for spec in specs:
            old, sep, new = spec.partition("=")
            if not sep or not old or not new:
                raise ValueError(f"rewrite must be OLD=NEW, got {spec!r}")
            pairs.append((old, new))
        return cls(tuple(pairs))

    def with_pair(self, charger_side: str, csms_side: str) -> "Rewriter":
        if charger_side == csms_side or (charger_side, csms_side) in self.pairs:
            return self
        return Rewriter(self.pairs + ((charger_side, csms_side),))

    @cached_property
    def _compiled(self) -> tuple[tuple[re.Pattern, str, re.Pattern, str], ...]:
        return tuple((_token(c), m, _token(m), c) for c, m in self.pairs)

    def to_csms(self, text: str) -> str:
        for charger_side, csms_value, _, _ in self._compiled:
            text = charger_side.sub(lambda _: csms_value, text)
        return text

    def to_charger(self, text: str) -> str:
        for _, _, csms_side, charger_value in self._compiled:
            text = csms_side.sub(lambda _: charger_value, text)
        return text
