from values.headers import SECURITY_HEADERS


def test_the_security_headers_grant_no_permission_but_inline_styles_and_the_pages_own_blobs():
  policy = {part.split()[0]: part.split()[1:] for part in SECURITY_HEADERS["Content-Security-Policy"].split(";")}
  assert policy["style-src"] == ["'self'", "'unsafe-inline'"]
  assert policy["img-src"] == ["'self'", "data:", "blob:"] and policy["worker-src"] == ["'self'", "blob:"]
  assert all("'unsafe-inline'" not in sources and "*" not in sources
             for directive, sources in policy.items() if directive != "style-src")
  assert policy["frame-ancestors"] == ["'none'"] and SECURITY_HEADERS["X-Frame-Options"] == "DENY"
  assert all(feature.endswith("=()") for feature in SECURITY_HEADERS["Permissions-Policy"].split(", "))
