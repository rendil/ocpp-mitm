"""Literal substring rewrites applied to every message that crosses the proxy."""

from dataclasses import dataclass


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
            if not sep or not old:
                raise ValueError(f"rewrite must be OLD=NEW, got {spec!r}")
            pairs.append((old, new))
        return cls(tuple(pairs))

    def to_csms(self, text: str) -> str:
        for charger_side, csms_side in self.pairs:
            text = text.replace(charger_side, csms_side)
        return text

    def to_charger(self, text: str) -> str:
        for charger_side, csms_side in self.pairs:
            text = text.replace(csms_side, charger_side)
        return text
