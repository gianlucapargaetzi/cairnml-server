"""The one API version the server serves. Endpoints declare it by default; a future version is added by registering
an implementation with a higher min_version, and the negotiation picks it for requests at or above that version."""

API_VERSION = "2.41"
