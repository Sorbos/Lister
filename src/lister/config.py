"""Settings for the eBay side of Lister.

Secrets (keys) come from environment variables or a `.env` file in the current folder.
Choices made by `lister ebay setup` (policies, location) are stored in the config folder,
by default ~/.config/lister (override with LISTER_CONFIG_DIR).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MARKETPLACES = {
    # marketplace: (currency, Content-Language)
    "EBAY_US": ("USD", "en-US"),
    "EBAY_CA": ("CAD", "en-CA"),
    "EBAY_GB": ("GBP", "en-GB"),
    "EBAY_AU": ("AUD", "en-AU"),
}


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader: KEY=value lines; existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    # utf-8-sig: Notepad may start the file with an invisible marker (BOM) that would hide the first key.
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def config_dir() -> Path:
    return Path(os.environ.get("LISTER_CONFIG_DIR", Path.home() / ".config" / "lister"))


@dataclass
class EbaySettings:
    client_id: str
    client_secret: str
    runame: str
    environment: str = "sandbox"  # "sandbox" | "production"
    marketplace: str = "EBAY_US"
    # Saved by `lister ebay setup`
    fulfillment_policy_id: str | None = None
    payment_policy_id: str | None = None
    return_policy_id: str | None = None
    location_key: str | None = None
    # "ebay": eBay starts scheduled listings (works with your computer off; eBay may charge a small fee).
    # "local": Lister publishes each card at its start time (free; keep `lister list` running).
    schedule_mode: str = "ebay"
    # Extra item specifics per category, e.g. {"261328": {"Type": "Sports Trading Card"}}
    extra_aspects: dict = field(default_factory=dict)

    @property
    def currency(self) -> str:
        return MARKETPLACES[self.marketplace][0]

    @property
    def language(self) -> str:
        return MARKETPLACES[self.marketplace][1]

    @property
    def dir(self) -> Path:
        return config_dir() / self.environment

    @property
    def saved_path(self) -> Path:
        return self.dir / "setup.json"

    @property
    def tokens_path(self) -> Path:
        return self.dir / "tokens.json"

    SAVED_KEYS = ("fulfillment_policy_id", "payment_policy_id", "return_policy_id", "location_key",
                  "schedule_mode", "extra_aspects")

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.saved_path.write_text(json.dumps({k: getattr(self, k) for k in self.SAVED_KEYS}, indent=2), encoding="utf-8")

    KEY_VARS = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME")

    @classmethod
    def has_keys(cls) -> bool:
        load_dotenv()
        return all(os.environ.get(k) for k in cls.KEY_VARS)

    @classmethod
    def offline(cls) -> "EbaySettings":
        """For previews before you have eBay keys: no sign-in, placeholder policies."""
        mp = os.environ.get("EBAY_MARKETPLACE", "EBAY_US").upper()
        return cls("", "", "", "sandbox", mp if mp in MARKETPLACES else "EBAY_US",
                   fulfillment_policy_id="(your shipping policy)", payment_policy_id="(your payment policy)",
                   return_policy_id="(your return policy)", location_key="(your item location)")

    @classmethod
    def load(cls) -> "EbaySettings":
        load_dotenv()
        missing = [k for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME") if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                "Missing " + ", ".join(missing) + ". Add them to a .env file (see README: eBay setup)."
            )
        env = os.environ.get("EBAY_ENV", "sandbox").lower()
        if env not in ("sandbox", "production"):
            raise SystemExit("EBAY_ENV must be 'sandbox' or 'production'")
        mp = os.environ.get("EBAY_MARKETPLACE", "EBAY_US").upper()
        if mp not in MARKETPLACES:
            raise SystemExit(f"EBAY_MARKETPLACE must be one of {', '.join(MARKETPLACES)}")
        s = cls(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"], os.environ["EBAY_RUNAME"], env, mp)
        if s.saved_path.exists():
            for k, v in json.loads(s.saved_path.read_text(encoding="utf-8")).items():
                if k in cls.SAVED_KEYS:
                    setattr(s, k, v)
        return s
