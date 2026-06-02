"""Tests for incremental re-discovery: diffing two CoverageInventories."""

from __future__ import annotations

from aitomation.diff import diff_inventories, element_fingerprint, element_key
from aitomation.models import CoverageInventory, InputField, Journey
from aitomation.models import TestableElement as Element  # aliased: avoid pytest "Test*" collection


def _inv(elements, journeys=()):
    return CoverageInventory(
        system_name="S", base_url="https://x", source="openapi",
        elements=list(elements), suggested_journeys=list(journeys),
    )


def _ep(name, path, method="GET", inputs=(), priority="medium", desc="d"):
    return Element(kind="endpoint", name=name, location=path, method=method,
                   description=desc, priority=priority, inputs=list(inputs))


def test_element_key_ignores_llm_name():
    # endpoints: method+path; a renamed endpoint is the same element
    assert element_key(_ep("a", "/things", "get")) == "endpoint GET /things"
    assert element_key(_ep("renamed", "/things", "GET")) == "endpoint GET /things"
    # pages: URL only (the LLM's page name is ignored)
    page_a = Element(kind="page", name="Home", location="/", description="d", priority="high")
    page_b = Element(kind="page", name="Landing Page", location="/", description="d", priority="high")
    assert element_key(page_a) == element_key(page_b) == "page /"
    # forms: URL + input field names (HTML-derived), not the LLM form name
    login = Element(kind="form", name="Login", location="/login", description="d", priority="high",
                    inputs=[InputField(name="email", where="form"), InputField(name="password", where="form")])
    login_renamed = Element(kind="form", name="Sign-in form", location="/login", description="d",
                            priority="high",
                            inputs=[InputField(name="password", where="form"), InputField(name="email", where="form")])
    assert element_key(login) == element_key(login_renamed)


def test_diff_added_and_removed_elements():
    old = _inv([_ep("list", "/things"), _ep("get", "/things/{id}")])
    new = _inv([_ep("list", "/things"), _ep("create", "/things", method="POST")])
    d = diff_inventories(old, new)
    assert [e.name for e in d.added_elements] == ["create"]
    assert [e.name for e in d.removed_elements] == ["get"]
    assert d.changed_elements == [] and not d.is_empty


def test_diff_changed_by_inputs_but_not_by_advisory_fields():
    title = [InputField(name="title", where="body")]
    old = _inv([_ep("create", "/things", method="POST", inputs=title)])

    # a new required input is a real, test-relevant change
    new_changed = _inv([_ep("create", "/things", method="POST",
                            inputs=title + [InputField(name="owner", where="body", required=True)])])
    d = diff_inventories(old, new_changed)
    assert len(d.changed_elements) == 1 and not d.added_elements and not d.removed_elements

    # description/priority differ only → NOT a change (won't make a drafted test stale)
    new_advisory = _inv([_ep("create", "/things", method="POST", desc="new words!",
                             priority="high", inputs=title)])
    d2 = diff_inventories(old, new_advisory)
    assert d2.changed_elements == [] and d2.is_empty


def test_fingerprint_stable_across_input_order():
    a = _ep("x", "/x", inputs=[InputField(name="a", where="query"), InputField(name="b", where="query")])
    b = _ep("x", "/x", inputs=[InputField(name="b", where="query"), InputField(name="a", where="query")])
    assert element_fingerprint(a) == element_fingerprint(b)


def test_diff_journeys_identified_by_element_set_not_name():
    title = [InputField(name="title", where="body")]
    old = _inv(
        [_ep("create", "/things", method="POST", inputs=title), _ep("list", "/things", "GET")],
        [Journey(name="Create thing", description="d", priority="high", elements=["create"]),
         Journey(name="List things", description="d", priority="low", elements=["list"])],
    )
    new = _inv(
        [_ep("create", "/things", method="POST",
             inputs=title + [InputField(name="owner", where="body", required=True)]),
         _ep("delete", "/things/{id}", method="DELETE")],
        # the create flow comes back RENAMED but touches the same element; list is gone; delete is new
        [Journey(name="Create a thing (renamed!)", description="d", priority="high", elements=["create"]),
         Journey(name="Delete thing", description="d", priority="high", elements=["delete"])],
    )
    d = diff_inventories(old, new)
    # the renamed create flow is NOT a new/removed flow — identity is its element set
    assert [j.name for j in d.added_journeys] == ["Delete thing"]
    assert [j.name for j in d.removed_journeys] == ["List things"]
    # but it touches the changed 'create' element → flagged as possibly stale
    assert [j.name for j in d.affected_journeys] == ["Create a thing (renamed!)"]


def test_diff_no_new_flows_when_only_journey_names_change():
    # same surface, journeys renamed every discover
    els = [_ep("list", "/things"), _ep("get", "/things/{id}")]
    old = _inv(els, [Journey(name="Browse things", description="d", priority="high",
                             elements=["list", "get"])])
    new = _inv(els, [Journey(name="Thing Discovery Path", description="d", priority="high",
                             elements=["list", "get"])])
    d = diff_inventories(old, new)
    assert d.added_journeys == [] and d.removed_journeys == [] and d.is_empty


def test_diff_no_new_flows_when_journeys_regrouped():
    # the actual bug: identical pages, but the LLM regroups them into differently-composed
    # journeys each crawl. No element changed → nothing new to draft, despite the churn.
    els = [_ep("a", "/a"), _ep("b", "/b"), _ep("c", "/c")]
    old = _inv(els, [Journey(name="AB flow", description="d", priority="high", elements=["a", "b"]),
                     Journey(name="C flow", description="d", priority="low", elements=["c"])])
    new = _inv(els, [Journey(name="A solo Path", description="d", priority="high", elements=["a"]),
                     Journey(name="BC Combo Journey", description="d", priority="high", elements=["b", "c"])])
    d = diff_inventories(old, new)
    assert d.added_journeys == [] and d.affected_journeys == [] and d.is_empty


def test_diff_new_flow_only_when_surface_actually_grows():
    old = _inv([_ep("a", "/a")], [Journey(name="A", description="d", priority="high", elements=["a"])])
    # a genuinely new endpoint shows up; the flow touching it is the one to draft
    new = _inv(
        [_ep("a", "/a"), _ep("b", "/b")],
        [Journey(name="A regrouped", description="d", priority="high", elements=["a"]),
         Journey(name="New B flow", description="d", priority="high", elements=["b"])],
    )
    d = diff_inventories(old, new)
    assert [j.name for j in d.added_journeys] == ["New B flow"]
    assert not d.is_empty


def test_diff_empty_when_identical():
    def build():
        return _inv([_ep("list", "/things")],
                    [Journey(name="L", description="d", priority="low", elements=["list"])])
    d = diff_inventories(build(), build())
    assert d.is_empty
    assert "elements +0 ~0 -0" in d.summary()
