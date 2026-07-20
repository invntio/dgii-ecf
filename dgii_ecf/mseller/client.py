"""Pure-Python MSeller e-CF client — NO frappe imports.

Transport layer for the MSeller e-CF gateway (https://ecf.api.mseller.app).
Copy this into `dgii_ecf/mseller/client.py`. Because it has no framework
dependencies it unit-tests without a Frappe site and could be extracted to a pip
package unchanged.

Contract reference: references/mseller-api.md. This mirrors the Odoo reference
`_mseller_login` / `_send_mseller` in l10n_do_e_cf_tests but as a standalone class.

Only dependency: `requests`.
"""

from __future__ import annotations

import requests

from dgii_ecf.providers.errors import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    safe_provider_payload_text,
)

# Environment is a PATH SEGMENT, not a header.
VALID_ENVIRONMENTS = ("TesteCF", "CerteCF", "eCF")
DEFAULT_BASE_URL = "https://ecf.api.mseller.app"


class MSellerError(ProviderError):
    """Base error for MSeller transport failures."""


class MSellerConnectionError(ProviderConnectionError, MSellerError):
    """The request outcome is unknown because transport failed."""


class MSellerAuthError(ProviderAuthError, MSellerError):
    """401/403 — bad credentials, expired token, or invalid API key."""


class MSellerHTTPError(ProviderHTTPError, MSellerError):
    def __init__(self, status_code: int, payload):
        ProviderHTTPError.__init__(self, status_code, payload, provider="MSeller")


class MSellerClient:
    def __init__(
        self,
        environment: str,
        email: str,
        password: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 60,
        token: str | None = None,
    ):
        """`token` seeds a previously-obtained idToken (e.g. from a cache) so the
        client skips the login round-trip; the built-in 401 retry replaces it
        transparently when it expires."""
        if environment not in VALID_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {VALID_ENVIRONMENTS}, got {environment!r}"
            )
        self.environment = environment
        self.email = email
        self.password = password
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = token

    # ── URLs ────────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        """Build /{env}/{path}. e.g. _url('documentos-ecf')."""
        return f"{self.base_url}/{self.environment}/{path.lstrip('/')}"

    # ── auth ────────────────────────────────────────────────────────────────
    def authenticate(self, force: bool = False) -> str:
        """POST /{env}/customer/authentication -> idToken. Cached until force."""
        if self._token and not force:
            return self._token
        url = self._url("customer/authentication")
        try:
            r = requests.post(
                url,
                json={"email": self.email, "password": self.password},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise MSellerConnectionError(f"Connection error to MSeller: {e}") from e

        data = _safe_json(r)
        if r.status_code == 401:
            raise MSellerAuthError(
                "Invalid credentials or expired token: "
                f"{safe_provider_payload_text(data)}",
                status_code=401,
            )
        if r.status_code == 403:
            raise MSellerAuthError(
                "Invalid credentials or permissions: "
                f"{safe_provider_payload_text(data)}",
                status_code=403,
            )
        if r.status_code >= 400:
            raise MSellerHTTPError(r.status_code, data)

        # Prefer idToken; tolerate token/accessToken like the Odoo client does.
        token = _pick(data, "idToken", "token", "accessToken")
        if not token:
            raise MSellerAuthError(
                "Login OK but no token in response: "
                f"{safe_provider_payload_text(data)}"
            )
        self._token = token
        return token

    def _headers(self, with_content_type: bool = True) -> dict:
        token = self.authenticate()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-API-KEY": self.api_key,
        }
        if with_content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, url: str, **kw):
        """Send a request, re-authenticating once on a 401."""
        for attempt in (1, 2):
            headers = self._headers(with_content_type=(method != "GET"))
            try:
                r = requests.request(method, url, headers=headers,
                                     timeout=self.timeout, **kw)
            except requests.RequestException as e:
                raise MSellerConnectionError(
                    f"Connection error to MSeller: {e}"
                ) from e
            if r.status_code == 401 and attempt == 1:
                self.authenticate(force=True)   # token expired -> refresh, retry once
                continue
            if r.status_code == 401:
                raise MSellerAuthError(
                    safe_provider_payload_text(_safe_json(r)), status_code=401
                )
            if r.status_code == 403:
                raise MSellerAuthError(
                    "Invalid API key or permissions: "
                    f"{safe_provider_payload_text(_safe_json(r))}",
                    status_code=403,
                )
            return r
        return r  # pragma: no cover

    # ── documents ───────────────────────────────────────────────────────────
    def send_document(self, ecf_json: dict, validate: bool = False) -> dict:
        """POST /{env}/documentos-ecf  (append ?validate=true to dry-run).

        On submit, returns {rnc, ecf, internalTrackId, securityCode, qr_url,
        signedDate}. On validate, returns {valid, message} or a validation-error
        payload. Raises for auth/HTTP errors other than validation failures.
        """
        url = self._url("documentos-ecf")
        if validate:
            url += "?validate=true"
        r = self._request("POST", url, json=ecf_json)
        data = _safe_json(r)

        if r.status_code == 401:
            raise MSellerAuthError(safe_provider_payload_text(data), status_code=401)
        if r.status_code == 403:
            raise MSellerAuthError(
                "Invalid API key or permissions: "
                f"{safe_provider_payload_text(data)}",
                status_code=403,
            )
        # A validation failure comes back as 4xx with ECF_VALIDATION_FAILED — let the
        # caller inspect it rather than raising, since validate=true expects errors.
        if validate:
            return data
        if r.status_code >= 400:
            raise MSellerHTTPError(r.status_code, data)
        return data

    def get_document(self, encf: str) -> dict:
        """GET /{env}/documentos-ecf?ecf={encf} — single status query."""
        url = self._url("documentos-ecf") + f"?ecf={encf}"
        r = self._request("GET", url)
        data = _safe_json(r)
        if r.status_code >= 400:
            raise MSellerHTTPError(r.status_code, data)
        return data

    def get_status_batch(self, encfs: list[str]) -> dict:
        """POST /{env}/documentos-ecf/status/batch — up to 100 e-CF per call.

        Returns {total, results:[{ecf, status, found, data:{...}}]}.
        """
        if len(encfs) > 100:
            raise ValueError("MSeller batch limit is 100 e-CF per request")
        url = self._url("documentos-ecf/status/batch")
        r = self._request("POST", url, json={"ecfs": encfs})
        data = _safe_json(r)
        if r.status_code >= 400:
            raise MSellerHTTPError(r.status_code, data)
        return data


# ── helpers ──────────────────────────────────────────────────────────────────
def _safe_json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"raw_response": r.text}


def _pick(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return None


if __name__ == "__main__":
    # Smoke test against TesteCF. Fill in real test credentials.
    import os

    client = MSellerClient(
        environment="TesteCF",
        email=os.environ["MSELLER_EMAIL"],
        password=os.environ["MSELLER_PASSWORD"],
        api_key=os.environ["MSELLER_API_KEY"],
    )
    client.authenticate()
    print("authentication: ok")
    # A validate round-trip consumes no eNCF — safe first call.
    sample = {"ECF": {"Encabezado": {"Version": "1.0"}}}
    print("validate:", client.send_document(sample, validate=True))
