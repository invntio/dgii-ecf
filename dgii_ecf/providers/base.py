"""Provider-agnostic e-CF gateway interface.

Everything in the module talks to `EcfProvider`, never to a concrete gateway
directly. Every provider normalizes its response into `EcfResult`, so callers read
`result.status` / `result.encf` and never a provider-specific JSON path. Adding a
future PSFE = one new `EcfProvider` subclass; nothing downstream changes.

Mirrors the Odoo reference (l10n_do_e_cf_tests `ecf_api_provider.send_ecf`), which
returns a normalized tuple regardless of provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EcfResult:
    """Normalized result — same shape for every provider."""

    success: bool
    status: str | None = None          # RECIBIDO/PROCESANDO/Aceptado/Rechazado/...
    encf: str | None = None
    track_id: str | None = None        # MSeller internalTrackId
    security_code: str | None = None
    qr_url: str | None = None
    signed_date: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)   # full provider response, for the log


class EcfProvider(ABC):
    """Abstract e-CF gateway. `settings` is an `ECF Provider Settings` doc."""

    def __init__(self, settings):
        self.settings = settings

    @abstractmethod
    def authenticate(self) -> str: ...

    @abstractmethod
    def send(self, ecf_json: dict, validate: bool = False) -> EcfResult: ...

    @abstractmethod
    def get_status(self, encf: str) -> EcfResult: ...

    @abstractmethod
    def get_status_batch(self, encfs: list[str]) -> list[EcfResult]: ...
