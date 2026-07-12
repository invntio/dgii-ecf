"""ECF Gateway Account — platform-level gateway login (Single, System Manager only).

One provider account represents/serves every invoicing company: the login yields
the bearer token, while each company's own API Key (in ECF Provider Settings)
identifies which registered emitter is being invoiced. Keeping the login here means
managers never see the operator's credentials and rotation happens in one place.
"""

from frappe.model.document import Document


class ECFGatewayAccount(Document):
    pass
