"""
Keep credentials out of messages shown to the user, logged, or put in a helpdesk ticket.

Error messages from ``requests`` quote the full request URL, and some SRCNet
calls carry tokens in the query string, so an exception message can contain a
live access or refresh token. :func:`redact_secrets` removes them.
"""
import re

REDACTED = "[REDACTED]"

# These run on untrusted text (server responses), so matching must stay linear in its length (no ReDoS): once a
# pattern has started matching it cannot fail, so each match consumes a whole run and no position is rescanned from
# many starts.

# A JWT (header.payload.signature, each base64url; the header always starts with "eyJ", i.e. '{"'). A truncated one
# is redacted too.
_JWT = re.compile(r"eyJ[\w-]+(?:\.[\w-]*){0,2}")
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"',;]+")
# A URL and its query string, if any: where tokens end up when sent as parameters.
_URL = re.compile(r"(https?://[^\s?#\"'<>]+)(\?[^\s#\"'<>]*)?")
# token=..., "access_token": "...", 'refresh_token': '...' (opaque tokens are not JWT-shaped).
_TOKEN_FIELD = re.compile(r"(?i)(\b(?:access_|refresh_|id_)?token[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;}]+")


def _redact_url_query(match):
    url, query = match.groups()
    return "{}?{}".format(url, REDACTED) if query is not None else url


def redact_secrets(text: str) -> str:
    """Return *text* with tokens and URL query strings replaced by ``[REDACTED]``.

    The rest of the text is kept, so the message still says what failed.
    """
    text = _URL.sub(_redact_url_query, text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _TOKEN_FIELD.sub(rf"\1{REDACTED}", text)
    return _JWT.sub(REDACTED, text)
