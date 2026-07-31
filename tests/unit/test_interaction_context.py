"""AP2 — the trusted ``X-Yuralume-Interaction`` scope.

The header is what tells the Gateway "this call is already paid for", so the
tests here are mostly about the *absence* cases: outside a scope, and for any
value that could not travel the grammar safely, no header must be emitted at
all rather than a best-effort one.
"""

from __future__ import annotations

from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    INTERACTION_HEADER_NAME,
    InteractionContext,
    confirm_deferred_deliveries,
    covered_call_receipt,
    current_interaction,
    interaction_header_value,
    interaction_headers,
    interaction_scope,
    new_interaction_id,
    response_billing_covered,
)


def test_no_header_outside_a_scope() -> None:
    assert current_interaction() is None
    assert interaction_header_value() is None
    assert interaction_headers() == {}


def test_scope_emits_versioned_header_and_restores_on_exit() -> None:
    context = InteractionContext(interaction_id="abc123", charge_id="chg-1")
    with interaction_scope(context):
        assert interaction_headers() == {
            INTERACTION_HEADER_NAME: "v1;id=abc123;charge=chg-1",
        }
        assert current_interaction() == context
    assert interaction_headers() == {}


def test_nested_scope_restores_the_outer_interaction() -> None:
    outer = InteractionContext(interaction_id="outer", charge_id="chg-outer")
    inner = InteractionContext(interaction_id="inner", charge_id="chg-inner")
    with interaction_scope(outer):
        with interaction_scope(inner):
            assert current_interaction() == inner
        assert current_interaction() == outer


def test_explicit_none_clears_an_outer_interaction() -> None:
    """An uncharged nested action must not inherit its parent's charge."""
    outer = InteractionContext(interaction_id="outer", charge_id="chg-outer")
    with interaction_scope(outer):
        with interaction_scope(None):
            assert interaction_headers() == {}
        assert current_interaction() == outer


def test_delimiter_bearing_values_emit_no_header() -> None:
    forged = InteractionContext(
        interaction_id="abc;charge=stolen", charge_id="chg-1",
    )
    with interaction_scope(forged):
        assert interaction_header_value() is None


def test_overlong_value_is_dropped_not_truncated() -> None:
    with interaction_scope(
        InteractionContext(interaction_id="x" * 101, charge_id="chg-1"),
    ):
        assert interaction_header_value() is None


def test_blank_charge_id_emits_no_header() -> None:
    with interaction_scope(
        InteractionContext(interaction_id="abc", charge_id="  "),
    ):
        assert interaction_header_value() is None


def test_new_interaction_ids_are_unique_and_wire_safe() -> None:
    first, second = new_interaction_id(), new_interaction_id()
    assert first != second
    with interaction_scope(
        InteractionContext(interaction_id=first, charge_id=second),
    ):
        assert interaction_header_value() is not None


# -- covered-call receipts (C2 / C4) -----------------------------------


def test_only_the_covered_header_counts_as_covered() -> None:
    assert response_billing_covered({BILLING_COVERED_HEADER_NAME: "1"}) is True
    assert response_billing_covered({"x-yuralume-billing-covered": "true"}) is True
    assert response_billing_covered({BILLING_COVERED_HEADER_NAME: "0"}) is False
    assert response_billing_covered({}) is False
    assert response_billing_covered(None) is False


def test_a_covered_receipt_counts_the_call_once_on_delivery() -> None:
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        receipt = covered_call_receipt({BILLING_COVERED_HEADER_NAME: "1"})
    assert context.usage.consumed is False  # headers alone buy nothing

    receipt.mark_delivered()
    receipt.mark_delivered()

    assert context.usage.covered_calls == 1


def test_an_uncovered_response_never_consumes_the_charge() -> None:
    """The Gateway billed that call itself; the action charge bought nothing."""
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        receipt = covered_call_receipt({})
    receipt.mark_delivered()

    assert context.usage.consumed is False


def test_a_deferred_receipt_counts_nothing_until_it_is_confirmed() -> None:
    """C4': the deliverable is the persisted result, not the response."""
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        receipt = covered_call_receipt({BILLING_COVERED_HEADER_NAME: "1"})
        receipt.defer_delivery()
        receipt.defer_delivery()  # idempotent — one call, one delivery
        assert context.usage.consumed is False

        assert confirm_deferred_deliveries() == 1
        assert confirm_deferred_deliveries() == 0  # nothing left pending

    assert context.usage.covered_calls == 1


def test_an_unconfirmed_deferral_leaves_the_charge_refundable() -> None:
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        covered_call_receipt(
            {BILLING_COVERED_HEADER_NAME: "1"},
        ).defer_delivery()

    assert context.usage.consumed is False


def test_deferring_an_uncovered_call_is_a_no_op() -> None:
    """The Gateway billed it per call; confirming it would bill it twice."""
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        covered_call_receipt({}).defer_delivery()
        assert confirm_deferred_deliveries() == 0

    assert context.usage.consumed is False


def test_confirming_outside_any_scope_is_harmless() -> None:
    assert confirm_deferred_deliveries() == 0


def test_the_receipt_remembers_the_scope_it_was_opened_in() -> None:
    """A streamed body is consumed by the route, outside the action scope."""
    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        receipt = covered_call_receipt({BILLING_COVERED_HEADER_NAME: "1"})

    receipt.mark_delivered()  # now outside the scope

    assert context.usage.consumed is True
