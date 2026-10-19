"""Local-only HTTP control plane with host/origin checks and a bootstrap token."""

import secrets

from starlette.responses import JSONResponse


class LocalSecurity:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode()
        hostname = host.split(":")[0]
        origin = headers.get(b"origin", b"").decode()
        fetch_site = headers.get(b"sec-fetch-site", b"").decode()
        authorized = (
            hostname in {"localhost", "127.0.0.1"}
            and (not origin or origin == "http://" + host)
            and fetch_site not in {"cross-site"}
        )
        path = scope.get("path", "")
        if path.startswith("/api"):
            candidate = headers.get(b"x-minicode-token", b"").decode()
            authorized = authorized and secrets.compare_digest(candidate, self.token)
        if not authorized:
            return await JSONResponse({"detail": "仅允许本机页面访问"}, status_code=403)(
                scope, receive, send
            )

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'",
                        ),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        await self.app(scope, receive, secure_send)
