"""MSeller implementation of `EcfProvider`.

Maps MSeller's HTTP JSON (see mseller/client.py) into the normalized `EcfResult`.

Token caching: MSeller idTokens (Cognito) live ~1 hour, and every operation used
to pay a login round-trip. The provider keeps ONE client per instance and persists
the idToken in Redis per LOGIN EMAIL + environment — with a platform gateway
account, one login serves every company (their per-company API Key does the tenant
scoping). The client's built-in 401 retry refreshes expired tokens; the write-back
after each call captures both first logins and refreshes.
"""

import time

import frappe
from frappe import _

from dgii_ecf.mseller.client import MSellerClient, MSellerError

from .base import EcfProvider, EcfResult

# Just under Cognito's ~60 min so a cached token is never served near expiry.
_TOKEN_TTL_SECONDS = 45 * 60


def _normalize_status(status: str | None) -> str | None:
    """Map gateway spelling/casing to values used by ECF Document Log."""
    if not status:
        return status
    return {
        "error": "ERROR",
        "pendiente": "PROCESANDO",
        "en cola": "PROCESANDO",
        "recibido": "RECIBIDO",
        "procesando": "PROCESANDO",
        "aceptado": "Aceptado",
        "aceptado condicional": "Aceptado Condicional",
        "rechazado": "Rechazado",
    }.get(status.casefold(), status)


class MSellerProvider(EcfProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self._client_inst: MSellerClient | None = None
        self._login_email: str | None = None

    @property
    def _token_key(self) -> str:
        # Keyed by LOGIN identity, not company: with a platform gateway account the
        # same bearer token serves every company (their API Key does the tenant
        # scoping), so one login covers all condos in an environment.
        return f"mseller_token:{self._login_email}:{self.settings.environment}"

    def _client(self) -> MSellerClient:
        if self._client_inst is None:
            s = self.settings
            email, password, base_url = s.login_credentials()
            self._login_email = email
            self._client_inst = MSellerClient(
                environment=s.environment,             # TesteCF / CerteCF / eCF
                email=email,
                password=password,
                api_key=s.active_api_key(),            # per-company, per-environment
                base_url=base_url,
                token=frappe.cache.get_value(self._token_key),
            )
        return self._client_inst

    def _persist_token(self):
        token = self._client_inst._token if self._client_inst else None
        if token and token != frappe.cache.get_value(self._token_key):
            frappe.cache.set_value(
                self._token_key, token, expires_in_sec=_TOKEN_TTL_SECONDS
            )

    def authenticate(self) -> str:
        self._throttle()
        token = self._client().authenticate()
        self._persist_token()
        return token

    def send(self, ecf_json: dict, validate: bool = False) -> EcfResult:
        self._throttle()
        try:
            r = self._client().send_document(ecf_json, validate=validate)
        finally:
            self._persist_token()
        if validate:
            # MSeller documents validate=true as a no-submit dry-run, but the
            # feature can be disabled per account. In that state the gateway may
            # ignore the query flag and return a normal submission receipt. Never
            # report that as an ordinary validation failure: the document was
            # actually signed/queued and needs immediate operator attention.
            if r.get("internalTrackId") or r.get("ecf") or r.get("qr_url"):
                raise MSellerError(
                    _(
                        "MSeller ignored validate=true and processed the document; "
                        "dry-run validation is not enabled for this gateway account."
                    )
                )
            return EcfResult(
                success=bool(r.get("valid")),
                error=None if r.get("valid") else r.get("message"),
                raw=r,
            )
        return EcfResult(
            success=True,
            status="RECIBIDO",
            encf=r.get("ecf"),
            track_id=r.get("internalTrackId"),
            security_code=r.get("securityCode"),
            qr_url=r.get("qr_url"),
            signed_date=r.get("signedDate"),
            signed_xml_path=r.get("signedXml"),
            raw=r,
        )

    def get_status(self, encf: str) -> EcfResult:
        self._throttle()
        try:
            r = self._client().get_document(encf)
        finally:
            self._persist_token()
        return EcfResult(
            success=True,
            status=_normalize_status(r.get("status")),
            encf=r.get("ncf") or encf,
            track_id=r.get("internalTrackId"),
            security_code=r.get("securityCode"),
            qr_url=r.get("qr_url"),
            signed_date=r.get("signedDate"),
            signed_xml_path=r.get("signedXml"),
            raw=r,
        )

    def get_status_batch(self, encfs: list[str]) -> list[EcfResult]:
        self._throttle()
        try:
            resp = self._client().get_status_batch(encfs)
        finally:
            self._persist_token()
        out: list[EcfResult] = []
        for item in resp.get("results", []):
            d = item.get("data") or {}
            out.append(
                EcfResult(
                    success=bool(item.get("found")),
                    status=_normalize_status(item.get("status") or d.get("status")),
                    encf=item.get("ecf") or d.get("ncf"),
                    track_id=d.get("internalTrackId"),
                    security_code=d.get("securityCode"),
                    qr_url=d.get("qr_url"),
                    signed_date=d.get("signedDate"),
                    signed_xml_path=d.get("signedXml"),
                    raw=item,
                )
            )
        return out

    def _throttle(self):
        """Conservative cross-worker pacing for MSeller's smallest plan.

        Provider settings may raise the limit for a contracted plan.  The Redis
        lock makes the spacing apply across every Frappe worker serving the same
        company/environment, not merely inside one Python process.
        """
        configured = self.settings.get("rate_limit_per_second")
        requests_per_second = max(1, int(configured or 5))
        interval = 1.0 / requests_per_second
        identity = self.settings.name or self.settings.company
        key = f"dgii_ecf:mseller_rate:{identity}:{self.settings.environment}"
        with frappe.cache.lock(key + ":lock", timeout=5, blocking_timeout=5):
            now = time.time()
            last = frappe.cache.get_value(key)
            if last is not None:
                time.sleep(max(0.0, interval - (now - float(last))))
            frappe.cache.set_value(key, time.time(), expires_in_sec=5)
