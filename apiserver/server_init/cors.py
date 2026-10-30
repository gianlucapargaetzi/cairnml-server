"""Which browser origins may call the API and read its responses.

The web app is served from the same origin as the API (through the reverse proxy), so it needs no CORS at all. Only
origins named in the configuration are allowed, plus the configured web address. A wildcard is never combined with
credentials: that would let any site read responses made with the session cookie.
"""

from urllib.parse import urlsplit

from apiserver.config_repo import config


def cors_settings() -> dict:
    settings = dict(config.get("apiserver.cors"))
    origins = settings.get("origins") or []
    if isinstance(origins, str):
        origins = [origins]
    origins = [str(origin) for origin in origins]

    if settings.get("supports_credentials") and "*" in origins:
        raise RuntimeError(
            "apiserver.cors: a wildcard origin cannot be combined with supports_credentials; list the origins instead"
        )

    web_url = config.get("apiserver.idp.web_url")
    if web_url:
        address = urlsplit(web_url)
        origins.append(f"{address.scheme}://{address.netloc}")

    settings["origins"] = origins
    return settings
