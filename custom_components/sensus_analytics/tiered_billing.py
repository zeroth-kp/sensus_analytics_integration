"""Shared progressive tiered-billing calculation for the Sensus Analytics Integration."""

from .const import GALLONS_PER_PRICING_UNIT


def calculate_tiered_cost(usage_gallons, tiers, free_gallons=0):
    """Calculate the variable (tier-priced) cost for a usage amount.

    ``tiers`` is an ordered list of ``(cutoff_gallons, price_per_1000_gal)``
    tuples. Each cutoff is the *absolute* cumulative usage at which that
    tier's bracket ends and the next tier begins; the final tier's cutoff may
    be ``None`` for an unbounded top bracket. Tiers with no price configured
    (``None``) are skipped, which also stops any tier after them from being
    considered, since a later tier can't be reached without pricing the one
    before it.

    ``free_gallons`` is a usage allowance - e.g. already covered by a flat
    base/service fee - billed at $0 before the first paid tier starts.

    Each gallon is billed at exactly one tier's rate: every bracket only
    charges for the portion of usage that actually falls within it.
    """
    if usage_gallons is None:
        return 0.0

    cost = 0.0
    previous_cutoff = free_gallons
    for cutoff, price in tiers:
        if price is None:
            break
        if usage_gallons <= previous_cutoff:
            break
        tier_top = usage_gallons if cutoff is None else min(usage_gallons, cutoff)
        bracket_gallons = tier_top - previous_cutoff
        cost += (bracket_gallons / GALLONS_PER_PRICING_UNIT) * price
        if cutoff is None or usage_gallons <= cutoff:
            break
        previous_cutoff = cutoff

    return cost
