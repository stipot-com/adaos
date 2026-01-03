# Ensure SmartTV/browser compatibility: allow TLS 1.2 (TLS 1.3 remains enabled).
# Primary switch is `SSL_POLICY=Mozilla-Intermediate` on `reverse-proxy`, this is a per-vhost safety net.
ssl_protocols TLSv1.2 TLSv1.3;

