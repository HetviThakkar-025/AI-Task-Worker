from agent import policy


class FakeSession:
    def __init__(self, elements):
        self._elements = elements

    def get_state_elements(self):
        return self._elements


def form(amount: str, button: str = "Save"):
    return FakeSession([
        {"id": 1, "tag": "a", "label": "Home", "href": "/"},
        {"id": 2, "tag": "input", "type": "number", "label": "Amount", "value": amount},
        {"id": 3, "tag": "button", "label": button},
        {"id": 4, "tag": "button", "label": "Delete record"},
        {"id": 5, "tag": "button", "label": "Mark as paid"},
    ])


def test_delete_button_requires_approval():
    assert "irreversible" in policy.check("browser_click", {"id": 4}, form("10"))


def test_paid_button_requires_approval():
    assert policy.check("browser_click", {"id": 5}, form("10")) is not None


def test_save_with_amount_at_threshold_requires_approval(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD", "100000")
    assert "threshold" in policy.check("browser_click", {"id": 3}, form("100000"))
    assert "threshold" in policy.check("browser_click", {"id": 3}, form("124,050.00"))


def test_ordinary_save_is_allowed(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD", "100000")
    assert policy.check("browser_click", {"id": 3}, form("4700.50")) is None


def test_non_click_tools_and_plain_links_are_allowed():
    s = form("500000")
    assert policy.check("browser_type", {"id": 2, "text": "500000"}, s) is None
    assert policy.check("browser_click", {"id": 1}, s) is None
    assert policy.check("browser_click", {"id": 99}, s) is None
