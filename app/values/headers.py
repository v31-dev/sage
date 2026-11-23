# Sage's strict security headers, the manager's own and every application's default: a permission is granted only by
# the one that sends them, over these. Inline styles are allowed, which charts and frameworks such as Vue insert, and
# so are images and workers from `blob:` URLs, which only the page's own script makes.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
                               "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
                               "upgrade-insecure-requests",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=63072000",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), "
                          "midi=(), display-capture=()",
}

# The security headers an application may send itself instead of Sage: a CSP can hold a nonce, a hash or a
# per-request address, which a static header can't.
APPLICATION_OWN_HEADERS = ("Content-Security-Policy",)
