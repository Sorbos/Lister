"""Thin client for the eBay REST APIs Lister needs.

Sign-in uses eBay's OAuth "authorization code" flow: you approve Lister once in the browser,
eBay redirects to a page whose address contains a code, you paste that address into the
terminal, and Lister swaps it for tokens. The long-lived refresh token is stored in the
config folder (readable only by you) and used to get fresh 2-hour access tokens.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from .config import EbaySettings

HOSTS = {
    "production": {"api": "https://api.ebay.com", "apim": "https://apim.ebay.com", "auth": "https://auth.ebay.com"},
    "sandbox": {"api": "https://api.sandbox.ebay.com", "apim": "https://apim.sandbox.ebay.com",
                "auth": "https://auth.sandbox.ebay.com"},
}
SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
]


class EbayError(RuntimeError):
    def __init__(self, status: int, method: str, url: str, body):
        self.status, self.body = status, body
        errors = body.get("errors", []) if isinstance(body, dict) else []
        self.error_ids = [e.get("errorId") for e in errors]
        detail = "; ".join(e.get("longMessage") or e.get("message", "") for e in errors) or str(body)[:300]
        super().__init__(f"eBay {status} on {method} {urlparse(url).path}: {detail}")


class NotSignedIn(RuntimeError):
    pass


class EbayClient:
    def __init__(self, settings: EbaySettings, session: requests.Session | None = None, clock=time.time):
        self.s = settings
        self.http = session or requests.Session()
        self.clock = clock
        self.hosts = HOSTS[settings.environment]
        self._tokens: dict | None = None

    # ---------- sign-in ----------
    def consent_url(self) -> str:
        q = {"client_id": self.s.client_id, "redirect_uri": self.s.runame, "response_type": "code",
             "scope": " ".join(SCOPES)}
        return f"{self.hosts['auth']}/oauth2/authorize?" + urlencode(q, quote_via=quote)

    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.s.client_id}:{self.s.client_secret}".encode()).decode()

    def _token_request(self, data: dict) -> dict:
        url = self.hosts["api"] + "/identity/v1/oauth2/token"
        r = self.http.post(url, data=data, headers={"Authorization": self._basic(),
                                                    "Content-Type": "application/x-www-form-urlencoded"})
        body = _json(r)
        if r.status_code != 200:
            raise EbayError(r.status_code, "POST", url, body)
        return body

    def sign_in(self, code_or_url: str) -> None:
        """Accept the code itself or the whole address eBay redirected to."""
        code = code_or_url.strip()
        if "code=" in code:
            code = parse_qs(urlparse(code).query).get("code", [""])[0]
        if not code:
            raise ValueError("No code found. Paste the full address from the browser after approving.")
        body = self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": self.s.runame})
        now = self.clock()
        self._save_tokens({
            "access_token": body["access_token"],
            "access_expires_at": now + body.get("expires_in", 7200) - 60,
            "refresh_token": body["refresh_token"],
            "refresh_expires_at": now + body.get("refresh_token_expires_in", 0),
        })

    def _save_tokens(self, tokens: dict) -> None:
        path: Path = self.s.tokens_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # only you can read it
        with os.fdopen(fd, "w") as f:
            json.dump(tokens, f)
        self._tokens = tokens

    def _load_tokens(self) -> dict:
        if self._tokens is None:
            if not self.s.tokens_path.exists():
                raise NotSignedIn("Not signed in to eBay. Run: lister ebay login")
            self._tokens = json.loads(self.s.tokens_path.read_text())
        return self._tokens

    def access_token(self) -> str:
        t = self._load_tokens()
        if self.clock() < t["access_expires_at"]:
            return t["access_token"]
        if t.get("refresh_expires_at") and self.clock() >= t["refresh_expires_at"]:
            raise NotSignedIn("Your eBay sign-in has expired. Run: lister ebay login")
        body = self._token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                    "scope": " ".join(SCOPES)})
        t = {**t, "access_token": body["access_token"],
             "access_expires_at": self.clock() + body.get("expires_in", 7200) - 60}
        self._save_tokens(t)
        return t["access_token"]

    # ---------- requests ----------
    def request(self, method: str, path: str, *, host: str = "api", json_body=None, params=None, files=None,
                ok=(200, 201, 204)) -> requests.Response:
        url = self.hosts[host] + path
        headers = {"Authorization": "Bearer " + self.access_token(),
                   "Accept-Language": self.s.language, "X-EBAY-C-MARKETPLACE-ID": self.s.marketplace}
        if json_body is not None:
            headers.update({"Content-Type": "application/json", "Content-Language": self.s.language})
        for attempt in range(3):
            r = self.http.request(method, url, json=json_body, params=params, files=files, headers=headers)
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                break
            time.sleep(2 ** attempt)
        if r.status_code not in ok:
            raise EbayError(r.status_code, method, url, _json(r))
        return r

    def get(self, path: str, **kw) -> dict:
        return _json(self.request("GET", path, **kw))

    # ---------- account setup ----------
    def policies(self, kind: str) -> list[dict]:
        """kind: fulfillment | payment | return"""
        body = self.get(f"/sell/account/v1/{kind}_policy", params={"marketplace_id": self.s.marketplace})
        return body.get(f"{kind}Policies", [])

    def locations(self) -> list[dict]:
        return self.get("/sell/inventory/v1/location", params={"limit": 100}).get("locations", [])

    def create_location(self, key: str, name: str, postal_code: str, country: str) -> None:
        self.request("POST", f"/sell/inventory/v1/location/{quote(key)}", json_body={
            "name": name,
            "location": {"address": {"postalCode": postal_code, "country": country}},
            "locationTypes": ["WAREHOUSE"],
            "merchantLocationStatus": "ENABLED",
        })

    # ---------- category metadata ----------
    def condition_policy(self, category_id: str) -> dict:
        body = self.get(f"/sell/metadata/v1/marketplace/{self.s.marketplace}/get_item_condition_policies",
                        params={"filter": f"categoryIds:{{{category_id}}}"})
        policies = body.get("itemConditionPolicies", [])
        return policies[0] if policies else {}

    def item_aspects(self, category_id: str) -> list[dict]:
        tree = self.get("/commerce/taxonomy/v1/get_default_category_tree_id",
                        params={"marketplace_id": self.s.marketplace})["categoryTreeId"]
        return self.get(f"/commerce/taxonomy/v1/category_tree/{tree}/get_item_aspects_for_category",
                        params={"category_id": category_id}).get("aspects", [])

    # ---------- listing ----------
    def upload_image(self, path: Path) -> str:
        """Upload a picture to eBay's picture hosting and return its URL for the listing."""
        with open(path, "rb") as f:
            r = self.request("POST", "/commerce/media/v1_beta/image/create_image_from_file", host="apim",
                             files={"image": (Path(path).name, f, "image/jpeg")}, ok=(201,))
        image_id = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        info = self.get(f"/commerce/media/v1_beta/image/{image_id}", host="apim")
        url = info.get("imageUrl")
        if not url:
            raise EbayError(200, "GET", f"image/{image_id}", {"errors": [{"message": f"no imageUrl in {info}"}]})
        return url

    def put_inventory_item(self, sku: str, body: dict) -> None:
        self.request("PUT", f"/sell/inventory/v1/inventory_item/{quote(sku)}", json_body=body)

    def find_offer(self, sku: str) -> dict | None:
        try:
            offers = self.get("/sell/inventory/v1/offer", params={"sku": sku}).get("offers", [])
        except EbayError as e:
            if e.status == 404:
                return None
            raise
        return offers[0] if offers else None

    def create_or_update_offer(self, sku: str, body: dict) -> str:
        existing = self.find_offer(sku)
        if existing:
            self.request("PUT", f"/sell/inventory/v1/offer/{existing['offerId']}", json_body=body)
            return existing["offerId"]
        return _json(self.request("POST", "/sell/inventory/v1/offer", json_body=body))["offerId"]

    def publish_offer(self, offer_id: str) -> str:
        return _json(self.request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish"))["listingId"]


def _json(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:500]}
