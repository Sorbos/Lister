"""Phase 4 tests against a simulated eBay (no network)."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from lister.cli import main
from lister.config import EbaySettings
from lister.ebay_api import EbayClient, EbayError, NotSignedIn
from lister.listing import ListingError, build_aspects, build_offer, condition_fields, list_batch
from lister.review import initial_state, normalise

SAMPLES = Path(__file__).parent.parent / "samples"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

CONDITION_POLICY = {"itemConditions": [
    {"conditionId": "4000", "conditionDescriptors": [{"conditionDescriptorId": "40001", "conditionDescriptorValues": [
        {"conditionDescriptorValueId": "400010", "conditionDescriptorValueName": "Near mint or better"},
        {"conditionDescriptorValueId": "400011", "conditionDescriptorValueName": "Excellent"},
        {"conditionDescriptorValueId": "400012", "conditionDescriptorValueName": "Very good"},
        {"conditionDescriptorValueId": "400013", "conditionDescriptorValueName": "Poor"}]}]},
    {"conditionId": "2750", "conditionDescriptors": [
        {"conditionDescriptorId": "27501", "conditionDescriptorValues": [
            {"conditionDescriptorValueId": "275010", "conditionDescriptorValueName": "Professional Sports Authenticator (PSA)"},
            {"conditionDescriptorValueId": "275011", "conditionDescriptorValueName": "Beckett Grading Services (BGS)"}]},
        {"conditionDescriptorId": "27502", "conditionDescriptorValues": [
            {"conditionDescriptorValueId": "275020", "conditionDescriptorValueName": "10"},
            {"conditionDescriptorValueId": "275021", "conditionDescriptorValueName": "9.5"}]}]},
]}
ASPECTS = [
    {"localizedAspectName": "Sport", "aspectConstraint": {"aspectMode": "SELECTION_ONLY", "aspectRequired": True},
     "aspectValues": [{"localizedValue": "Ice Hockey"}, {"localizedValue": "Baseball"}]},
    {"localizedAspectName": "Player/Athlete", "aspectConstraint": {"aspectMode": "FREE_TEXT"}},
    {"localizedAspectName": "Team", "aspectConstraint": {"aspectMode": "FREE_TEXT"}},
    {"localizedAspectName": "Season", "aspectConstraint": {"aspectMode": "FREE_TEXT"}},
    {"localizedAspectName": "Card Number", "aspectConstraint": {"aspectMode": "FREE_TEXT"}},
    {"localizedAspectName": "Features", "aspectConstraint": {"aspectMode": "SELECTION_ONLY"},
     "aspectValues": [{"localizedValue": "Rookie"}, {"localizedValue": "Patch"}, {"localizedValue": "Serial Numbered"}]},
    {"localizedAspectName": "Type", "aspectConstraint": {"aspectMode": "SELECTION_ONLY", "aspectRequired": True},
     "aspectValues": [{"localizedValue": "Sports Trading Card"}]},
]


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class FakeEbay:
    """Minimal stand-in for requests.Session that behaves like the eBay endpoints Lister uses."""

    def __init__(self):
        self.calls, self.offers, self.items, self.images = [], {}, {}, 0
        self.fail_publish = set()

    def post(self, url, data=None, headers=None):
        self.calls.append(("POST", urlparse(url).path, data))
        assert headers["Authorization"].startswith("Basic ")
        if data["grant_type"] == "authorization_code":
            assert data["code"] == "THE-CODE"
            return Resp(200, {"access_token": "AT1", "expires_in": 7200, "refresh_token": "RT",
                              "refresh_token_expires_in": 47304000})
        return Resp(200, {"access_token": "AT2", "expires_in": 7200})

    def request(self, method, url, json=None, params=None, files=None, headers=None):
        u = urlparse(url)
        path = u.path
        self.calls.append((method, path, json if json is not None else params))
        assert headers["Authorization"].startswith("Bearer ")
        if "get_item_condition_policies" in path:
            return Resp(200, {"itemConditionPolicies": [CONDITION_POLICY]})
        if path.endswith("get_default_category_tree_id"):
            return Resp(200, {"categoryTreeId": "0"})
        if path.endswith("get_item_aspects_for_category"):
            return Resp(200, {"aspects": ASPECTS})
        if path.endswith("create_image_from_file"):
            assert "image" in files
            self.images += 1
            return Resp(201, None, {"Location": f"https://apim.ebay.com/commerce/media/v1_beta/image/IMG{self.images}"})
        if "/media/v1_beta/image/" in path:
            return Resp(200, {"imageUrl": "https://i.ebayimg.com/" + path.rsplit("/", 1)[1]})
        if "/inventory_item/" in path:
            self.items[path.rsplit("/", 1)[1]] = json
            return Resp(204)
        if path.endswith("/offer") and method == "GET":
            o = [v for v in self.offers.values() if v["sku"] == params["sku"]]
            return Resp(200, {"offers": o}) if o else Resp(404, {"errors": [{"errorId": 25713}]})
        if path.endswith("/offer") and method == "POST":
            oid = f"OF{len(self.offers) + 1}"
            self.offers[oid] = {**json, "offerId": oid}
            return Resp(201, {"offerId": oid})
        if "/offer/" in path and method == "PUT":
            oid = path.rsplit("/", 1)[1]
            self.offers[oid] = {**json, "offerId": oid}
            return Resp(204)
        if path.endswith("/publish"):
            oid = path.split("/")[-2]
            if oid in self.fail_publish:
                return Resp(400, {"errors": [{"errorId": 25002, "longMessage": "Something is wrong"}]})
            return Resp(200, {"listingId": "11000" + oid[2:]})
        raise AssertionError(f"unexpected {method} {path}")

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method in (None, m)]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LISTER_CONFIG_DIR", str(tmp_path / "cfg"))
    s = EbaySettings("cid", "secret", "My-RuName", "sandbox", "EBAY_US",
                     fulfillment_policy_id="F", payment_policy_id="P", return_policy_id="R", location_key="home",
                     extra_aspects={"261328": {"Type": "Sports Trading Card"}})
    return s


@pytest.fixture
def client(settings):
    fake = FakeEbay()
    c = EbayClient(settings, session=fake, clock=lambda: NOW.timestamp())
    c.sign_in("https://signin.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true&code=THE-CODE&expires_in=299")
    return c


def info(**kw):
    base = dict(category="sports", sport="Ice Hockey", league="NHL", year="2005-06", manufacturer="Upper Deck",
                set_name="Young Guns", subset_insert=None, parallel=None, card_number="201", subject="Sidney Crosby",
                team="Pittsburgh Penguins", rookie=True, autograph=False, memorabilia=False, serial_number=None,
                graded=False, grader=None, grade=None, condition="excellent")
    return {**base, **kw}


# ------------------------------------------------------------------ sign-in
def test_consent_url(settings):
    q = parse_qs(urlparse(EbayClient(settings).consent_url()).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["My-RuName"] and q["response_type"] == ["code"]
    assert "sell.inventory" in q["scope"][0] and "sell.account" in q["scope"][0]
    assert EbayClient(settings).consent_url().startswith("https://auth.sandbox.ebay.com/oauth2/authorize")


def test_sign_in_stores_private_tokens_and_refreshes(settings, client):
    path = settings.tokens_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert client.access_token() == "AT1"
    later = EbayClient(settings, session=client.http, clock=lambda: NOW.timestamp() + 3 * 3600)
    assert later.access_token() == "AT2"  # refreshed with the stored refresh token
    assert json.loads(path.read_text())["refresh_token"] == "RT"


def test_not_signed_in(settings):
    with pytest.raises(NotSignedIn):
        EbayClient(settings, session=FakeEbay()).access_token()


# ------------------------------------------------------------------ payloads
def test_condition_ungraded_and_graded():
    assert condition_fields(info(condition="excellent"), CONDITION_POLICY) == {
        "condition": "USED_VERY_GOOD", "conditionDescriptors": [{"name": "40001", "values": ["400011"]}]}
    assert condition_fields(info(condition="near_mint_or_better"), CONDITION_POLICY)["conditionDescriptors"][0]["values"] == ["400010"]
    g = condition_fields(info(graded=True, grader="PSA", grade="9.5"), CONDITION_POLICY)
    assert g == {"condition": "LIKE_NEW", "conditionDescriptors": [{"name": "27501", "values": ["275010"]},
                                                                  {"name": "27502", "values": ["275021"]}]}
    with pytest.raises(ListingError, match="grader"):
        condition_fields(info(graded=True, grader="XYZ", grade="10"), CONDITION_POLICY)
    with pytest.raises(ListingError, match="needs a grader"):
        condition_fields(info(graded=True), CONDITION_POLICY)


def test_aspects_use_ebay_names_and_choices():
    aspects, warnings = build_aspects(info(sport="Hockey"), ASPECTS, {"Type": "Sports Trading Card"})
    assert aspects["Player/Athlete"] == ["Sidney Crosby"]
    assert aspects["Season"] == ["2005-06"]
    assert aspects["Features"] == ["Rookie"]
    assert aspects["Type"] == ["Sports Trading Card"]
    assert "Sport" not in aspects  # "Hockey" isn't one of eBay's choices
    assert any("Hockey" in w for w in warnings) and any("requires 'Sport'" in w for w in warnings)
    aspects, warnings = build_aspects(info(), ASPECTS, None)
    assert aspects["Sport"] == ["Ice Hockey"]
    assert any("requires 'Type'" in w for w in warnings)


def test_offer_auction_fixed_and_schedule(settings):
    card = {"title": "T", "info": info(), "listing": {"format": "auction", "start_price": 0.99, "buy_it_now": 5,
                                                      "reserve": None, "duration_days": 7, "price": None,
                                                      "start_time": "2026-11-16T00:03:00Z"}}
    o = build_offer(card, "SKU1", settings, send_start_time=True)
    assert o["format"] == "AUCTION" and o["listingDuration"] == "DAYS_7"
    assert o["pricingSummary"] == {"auctionStartPrice": {"value": "0.99", "currency": "USD"},
                                   "price": {"value": "5.00", "currency": "USD"}}
    assert o["listingStartDate"] == "2026-11-16T00:03:00.000Z"
    assert o["categoryId"] == "261328" and o["listingPolicies"]["returnPolicyId"] == "R"
    assert "listingStartDate" not in build_offer(card, "SKU1", settings, send_start_time=False)
    card["listing"].update(format="fixed_price", price=12.5)
    card["info"]["category"] = "non_sport"
    o = build_offer(card, "SKU1", settings, send_start_time=True)
    assert o["format"] == "FIXED_PRICE" and o["listingDuration"] == "GTC" and o["categoryId"] == "183050"
    assert o["pricingSummary"] == {"price": {"value": "12.50", "currency": "USD"}}


# ------------------------------------------------------------------ full runs
@pytest.fixture
def batch(tmp_path):
    folder = tmp_path / "batch"
    main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(folder)])
    state = initial_state(folder)
    for c in state["cards"][:3]:
        c["info"].update(info(card_number=str(c["position"]), subject=f"Player {c['position']}"))
        c["approved"] = True
    state["cards"][1]["listing"]["start_time"] = (NOW + timedelta(hours=2)).isoformat()
    state["cards"][2]["listing"].update(format="fixed_price", price=8)
    state = normalise(state)
    for c in state["cards"][:3]:
        c["approved"] = True  # normalise() validates against the real clock; approval is what matters here
    (folder / "review.json").write_text(json.dumps(state))
    return folder


def run(folder, settings, client, **kw):
    return list_batch(folder, settings, client, clock=lambda: NOW.timestamp(), progress=lambda *_: None, **kw)


def test_list_batch_ebay_scheduling(batch, settings, client):
    fake = client.http
    env = run(batch, settings, client)
    assert sorted(env["cards"]) == ["1", "2", "3"]
    assert all(r["listing_id"].startswith("11000") for r in env["cards"].values())
    assert fake.images == 6  # front + back for 3 cards
    offers = sorted(fake.offers.values(), key=lambda o: o["sku"])
    assert "listingStartDate" not in offers[0]
    assert offers[1]["listingStartDate"] == "2026-11-14T14:00:00.000Z"
    assert offers[2]["format"] == "FIXED_PRICE"
    item = next(iter(fake.items.values()))
    assert item["product"]["imageUrls"][0].startswith("https://i.ebayimg.com/")
    assert item["product"]["aspects"]["Type"] == ["Sports Trading Card"]
    assert item["conditionDescriptors"] == [{"name": "40001", "values": ["400011"]}]
    # Category details fetched once, not per card.
    assert len([p for p in fake.paths("GET") if "get_item_condition_policies" in p]) == 1

    # Running again lists nothing twice.
    before = len(fake.calls)
    run(batch, settings, client)
    assert not [p for p in fake.paths()[before:] if p.endswith("/publish") or "inventory_item" in p]


def test_list_batch_resumes_after_failure(batch, settings, client):
    client.http.fail_publish = {"OF2"}
    with pytest.raises(EbayError, match="Something is wrong"):
        run(batch, settings, client)
    hist = json.loads((batch / "ebay.json").read_text())["environments"]["sandbox"]["cards"]
    assert hist["1"]["listing_id"] and "listing_id" not in hist["2"] and hist["2"]["offer_id"] == "OF2"
    client.http.fail_publish = set()
    uploads = client.http.images
    env = run(batch, settings, client)
    assert all(r.get("listing_id") for r in env["cards"].values())
    assert client.http.images == uploads + 2  # only card 3's photos; card 2's were already uploaded
    assert len(client.http.offers) == 3  # card 2's offer was updated, not duplicated


def test_list_batch_local_scheduling(batch, settings, client):
    settings.schedule_mode = "local"
    slept = []
    run(batch, settings, client, sleep=slept.append)
    assert slept == [7200.0]
    assert all("listingStartDate" not in o for o in client.http.offers.values())
    publishes = [p for p in client.http.paths("POST") if p.endswith("/publish")]
    assert publishes[-1] == "/sell/inventory/v1/offer/OF2/publish"  # the scheduled card went last


def test_dry_run_offline(batch, settings):
    run(batch, settings, None, dry_run=True)
    preview = json.loads((batch / "ebay_preview.json").read_text())
    assert [p["position"] for p in preview] == [1, 2, 3]
    assert preview[0]["offer"]["format"] == "AUCTION"
    assert not (batch / "ebay.json").exists() or "listing_id" not in (batch / "ebay.json").read_text()


def test_refuses_without_setup_or_with_past_start(batch, settings, client):
    settings.location_key = None
    with pytest.raises(ListingError, match="lister ebay setup"):
        run(batch, settings, client)
    settings.location_key = "home"
    state = json.loads((batch / "review.json").read_text())
    state["cards"][1]["listing"]["start_time"] = (NOW - timedelta(minutes=1)).isoformat()
    (batch / "review.json").write_text(json.dumps(state))
    with pytest.raises(ListingError, match="card 2: Start time is in the past"):
        run(batch, settings, client)
    assert not client.http.items


def test_nothing_approved(tmp_path, settings):
    main(["split", str(SAMPLES / "fronts_01.jpg"), "-o", str(tmp_path)])
    (tmp_path / "review.json").write_text(json.dumps(initial_state(tmp_path)))
    with pytest.raises(ListingError, match="No approved"):
        run(tmp_path, settings, None, dry_run=True)


def test_dry_run_without_ebay_keys(batch, monkeypatch, capsys):
    """Showcase before having eBay developer keys: preview works, nothing asks to list for real."""
    for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(batch)  # no .env here
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not ask for confirmation"))
    state = json.loads((batch / "review.json").read_text())
    for c in state["cards"]:  # this test runs on the real clock; the fixture's start time is fixed in the past
        c["listing"]["start_time"] = None
    (batch / "review.json").write_text(json.dumps(state))
    assert main(["list", str(batch), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "previewing offline" in out and "[dry run]" in out
    preview = json.loads((batch / "ebay_preview.json").read_text())
    assert preview[0]["offer"]["listingPolicies"]["fulfillmentPolicyId"] == "(your shipping policy)"
    # Without --dry-run, missing keys is still a clear error.
    with pytest.raises(SystemExit, match="Missing EBAY_CLIENT_ID"):
        main(["list", str(batch)])
