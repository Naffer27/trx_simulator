# simulator/support_knowledge/service.py
"""
CUSTOMER-SUPPORT-01D — read-only query surface over the static
catalogue (catalogue.py). No database access, no side effects — every
function here is a pure function of CATALOGUE. Disabled
(POLICY_PENDING) items are structurally excluded from every result —
not filtered at the call site, but absent by construction — so a
caller cannot accidentally surface one.
"""
from .catalogue import CATALOGUE, CATEGORY_LABELS, TICKET_CATEGORY_MAP, KnowledgeCategory


def _enabled_items():
    return [item for item in CATALOGUE if item.enabled]


def list_categories():
    """
    Ordered (slug, label) pairs for every category that has at least
    one enabled item. A category with zero enabled items is never
    returned — an empty category would be a confusing dead end for the
    customer.
    """
    present = {item.category for item in _enabled_items()}
    return [
        (category.value, CATEGORY_LABELS[category])
        for category in KnowledgeCategory
        if category in present
    ]


def list_questions(category_slug):
    """Enabled items for one category, in catalogue-definition order.
    An unknown or entirely-disabled category slug returns an empty
    list, never an error."""
    try:
        category = KnowledgeCategory(category_slug)
    except ValueError:
        return []
    return [item for item in _enabled_items() if item.category == category]


def get_item(intent):
    """A single enabled item by intent id, or None. A disabled
    (POLICY_PENDING) item's intent id is never resolvable here, even
    if guessed exactly — this is the enforcement point for the
    withdrawal_minimum safeguard and every other pending item."""
    for item in _enabled_items():
        if item.intent == intent:
            return item
    return None


def ticket_category_for(category_slug):
    """The real SupportTicket.CATEGORY_CHOICES value the human-handoff
    flow should preselect for a given knowledge category."""
    try:
        category = KnowledgeCategory(category_slug)
    except ValueError:
        return "other"
    return TICKET_CATEGORY_MAP.get(category, "other")
