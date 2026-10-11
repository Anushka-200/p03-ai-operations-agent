"""Twilio request signature check.

Analogy: a wax seal on a letter. Twilio seals each webhook with a secret only it and we know; anyone
else posting to our URL cannot forge the seal, so a stranger cannot make our agent "receive" a message.

The recipe (from Twilio's docs): take the full URL Twilio called, append each POST field as
name+value sorted by name, HMAC-SHA1 it with the auth token, base64 the result. Compare in constant time.
"""

import base64
import hashlib
import hmac


def compute_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid(url: str, params: dict[str, str], signature: str | None, auth_token: str) -> bool:
    if not auth_token or not signature:
        return False  # no token configured or no header: reject, never "allow by default"
    return hmac.compare_digest(compute_signature(url, params, auth_token), signature)
