# vhost/api.inimatic.com

# Allow TLS 1.2 for SmartTV/legacy clients (TLS 1.3 remains enabled).
ssl_protocols TLSv1.2 TLSv1.3;

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

# --- Browser -> Hub proxy over Root (WS + HTTP) ---
location ^~ /hubs/ {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_connect_timeout 60s;

  proxy_pass https://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location ~ ^/v1/(owner|pki|registry|drafts|devices)/ {
    if ($ssl_client_verify != SUCCESS) { return 400; }
    proxy_pass http://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}
