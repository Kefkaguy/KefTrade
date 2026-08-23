"""The token that says attribution was already established for this order.

``GovernedOrderSubmitter`` enforces "attribution before POST" by doing the two
in order. That is only a guarantee if the second step cannot be reached without
the first -- and until now it could be, by constructing the portfolio adapter
and calling it directly. This module closes that.

The adapter's mutating entry point requires a ``SubmissionCapability``, and a
capability can only be produced from a *verified* attribution record. So the
enforcement is not "the submitter remembers to attribute first"; it is that the
adapter has nothing to act on until attribution has happened.

**Why this module sits here, importing nothing.** The capability has to be named
by both the adapter and the submission service. If it lived with the service,
the adapter would import a module that reaches the database; if it lived with
the adapter, the service's dependencies would invert. A leaf module with no
imports of its own belongs to neither and can be named by both.

**What this does and does not prevent.** It makes a plain direct call to the
adapter fail, and makes minting require an attribution record that says it was
verified. It cannot stop code that deliberately reaches for a private name and
fabricates a record -- no in-process Python mechanism can, and pretending
otherwise would be worse than stating the limit. The boundary this enforces is
against *accident and convenience*, which is what a bypass in practice is.
"""

from __future__ import annotations

from typing import Any

# Module-private. Holding this object is what distinguishes a capability minted
# by the sanctioned path from one someone constructed because it was convenient.
_CAPABILITY_MINT = object()


class SubmissionCapabilityError(RuntimeError):
    """No capability, or one that does not authorise this particular order."""


class SubmissionCapability:
    """Proof that a specific order's attribution is durable and verified.

    Bound to the order's identity rather than granting blanket permission: a
    capability for one order cannot be replayed onto another, so a bug that
    reused one would fail loudly instead of sending the wrong thing.

    Carries no quantity. It authorises *which* order may be sent, never *how
    much* -- the same separation the attribution table keeps, for the same
    reason.
    """

    __slots__ = (
        "broker_account_id",
        "client_order_id",
        "intended_side",
        "strategy",
        "strategy_version",
        "symbol",
    )

    def __init__(
        self,
        mint: Any,
        *,
        broker_account_id: int,
        client_order_id: str,
        strategy: str,
        strategy_version: str,
        symbol: str,
        intended_side: str,
    ) -> None:
        if mint is not _CAPABILITY_MINT:
            raise SubmissionCapabilityError(
                "a submission capability cannot be constructed directly; it is "
                "issued only by GovernedOrderSubmitter, after the order's "
                "attribution has been persisted and read back. Route the order "
                "through that submitter rather than building a token for it."
            )
        self.broker_account_id = broker_account_id
        self.client_order_id = client_order_id
        self.strategy = strategy
        self.strategy_version = strategy_version
        self.symbol = symbol
        self.intended_side = intended_side

    @classmethod
    def after_verified_attribution(
        cls, *, attribution: dict[str, Any], verified: bool
    ) -> SubmissionCapability:
        """Issue a capability from a verified attribution record.

        ``verified`` is the flag ``persist_and_verify_attribution`` sets only
        after re-reading the row and finding it describes this exact order. A
        record that does not claim verification cannot mint anything, so the
        capability and the verification cannot come apart.
        """
        if not verified:
            raise SubmissionCapabilityError(
                "attribution was not verified, so no capability may be issued; "
                "an unverified attribution is indistinguishable from none"
            )
        missing = [
            field
            for field in (
                "broker_account_id",
                "client_order_id",
                "strategy",
                "strategy_version",
                "symbol",
                "intended_side",
            )
            if attribution.get(field) in (None, "")
        ]
        if missing:
            raise SubmissionCapabilityError(
                f"the attribution record is missing {missing}; a capability must "
                "name the order it authorises completely, or it authorises "
                "whatever is passed with it"
            )
        return cls(
            _CAPABILITY_MINT,
            broker_account_id=int(attribution["broker_account_id"]),
            client_order_id=str(attribution["client_order_id"]),
            strategy=str(attribution["strategy"]),
            strategy_version=str(attribution["strategy_version"]),
            symbol=str(attribution["symbol"]).upper(),
            intended_side=str(attribution["intended_side"]).lower(),
        )

    def authorises(self, payload: dict[str, Any]) -> bool:
        """Does this capability name the order in hand?"""
        return (
            str(payload.get("client_order_id") or "") == self.client_order_id
            and str(payload.get("symbol") or "").upper() == self.symbol
            and str(payload.get("side") or "").lower() == self.intended_side
        )

    def assert_authorises(self, payload: dict[str, Any]) -> None:
        if not self.authorises(payload):
            raise SubmissionCapabilityError(
                f"the capability authorises {self.intended_side} {self.symbol} "
                f"as {self.client_order_id!r}, which is not the order being sent "
                f"({payload.get('side')} {payload.get('symbol')} as "
                f"{payload.get('client_order_id')!r}); a capability is bound to "
                "one order and cannot be replayed onto another"
            )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"SubmissionCapability({self.intended_side} {self.symbol} "
            f"for {self.strategy}@{self.strategy_version})"
        )


def require_submission_capability(
    capability: Any, payload: dict[str, Any]
) -> SubmissionCapability:
    """The adapter's gate: a real capability, naming this order.

    ``None`` is the ordinary case -- someone called the adapter directly -- and
    it is refused with the route they should have taken rather than a bare type
    error.
    """
    if capability is None:
        raise SubmissionCapabilityError(
            "a portfolio order requires a submission capability, which is issued "
            "only after its attribution is persisted and verified; submit through "
            "GovernedOrderSubmitter instead of calling the adapter directly"
        )
    if not isinstance(capability, SubmissionCapability):
        raise SubmissionCapabilityError(
            f"{type(capability).__name__} is not a submission capability"
        )
    capability.assert_authorises(payload)
    return capability
