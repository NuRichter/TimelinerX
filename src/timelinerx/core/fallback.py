"""Explicit fallback decisions (Axiom III.3 — no silent fallback).

Any module that would degrade what the user asked for (encoder, resolution,
map theme, HDR→SDR, map provider) must build a :class:`FallbackDecision` and
pass it through a :class:`FallbackPolicy`. The policy either obtains consent
(GUI dialog, ``--accept-fallback`` flag) or refuses, raising
:class:`FallbackRequiresConfirmationError`. Every applied decision is recorded
so it ends up in render metadata and logs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from .errors import FallbackRequiresConfirmationError


@dataclass(frozen=True)
class FallbackDecision:
    subsystem: str          # e.g. "encoder", "hdr", "map_provider"
    requested: str          # what the user asked for
    proposed: str           # what we would use instead
    reason: str             # why the requested option is unavailable
    impact: str             # what the user will notice
    requires_confirmation: bool = True

    def describe(self) -> str:
        return (f"[{self.subsystem}] '{self.requested}' is unavailable: {self.reason}. "
                f"Proposed fallback: '{self.proposed}'. Impact: {self.impact}")

    def to_dict(self) -> dict:
        return asdict(self)


Confirmer = Callable[[FallbackDecision], bool]


@dataclass
class FallbackPolicy:
    """Decides whether a fallback may be applied.

    ``confirmer`` is called for decisions that require confirmation. With no
    confirmer and ``accept_all`` False, such decisions are refused.
    """

    confirmer: Optional[Confirmer] = None
    accept_all: bool = False
    notify: Optional[Callable[[FallbackDecision], None]] = None
    applied: List[FallbackDecision] = field(default_factory=list)

    def resolve(self, decision: FallbackDecision) -> FallbackDecision:
        if self.notify is not None:
            self.notify(decision)
        if decision.requires_confirmation and not self.accept_all:
            ok = self.confirmer(decision) if self.confirmer is not None else False
            if not ok:
                raise FallbackRequiresConfirmationError(
                    decision.describe(), decision=decision,
                    hint="Re-run with explicit consent (GUI prompt or --accept-fallback) "
                         "or change the requested option.")
        self.applied.append(decision)
        return decision
