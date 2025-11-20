# vhost/api.inimatic.com

ssl_client_certificate /etc/nginx/certs/adaos_ca.pem;
ssl_verify_client optional;
ssl_verify_depth 2;

# --- эндпоинты bootstrap без mTLS ---
location = /v1/bootstrap_token {
    # не требуем SUCCESS
    proxy_pass https://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/subnets/register {
    # не требуем SUCCESS
    proxy_pass https://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/pair/confirm {
    # не требуем SUCCESS
    proxy_pass https://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location /nats {
  # No client cert required for WS bridge
  proxy_pass http://nats:8080;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- защищённые пути под mTLS ---
location ~ ^/v1/(owner|pki|registry|drafts|devices)/ {
    if ($ssl_client_verify != SUCCESS) { return 400; }
    proxy_pass http://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}
