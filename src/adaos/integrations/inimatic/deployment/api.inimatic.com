# vhost/api.inimatic.com

ssl_client_certificate /etc/nginx/certs/adaos_ca.pem;
ssl_verify_client optional;
ssl_verify_depth 2;

# для пары endpoint — выключаем mTLS (хабу будет достаточно обычного TLS)
location /v1/pair/confirm {
  # отключить только здесь
  ssl_verify_client off;

  # стандартный прокси-набор nginx-proxy уже добавляет proxy_pass,
  # мы лишь подстрахуемся заголовками
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

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

# телеграм-вебхуки — небольшой лимит и базовая защита от мусора
location ^~ /io/tg/webhook/ {
  # тело входящего JSON крошечное
  client_max_body_size 1m;

  # только POST
  if ($request_method !~ ^(POST)$) { return 405; }

  # (если используете глобальный mTLS) вебхуку обычно не нужен mTLS
  ssl_verify_client off;

  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location = /healthz { ssl_verify_client off; }

# --- NATS WebSocket passthrough ---
location /nats {
  # No client cert required for WS bridge
  ssl_verify_client off;

  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_read_timeout  60s;
  proxy_send_timeout  60s;
  proxy_connect_timeout 5s;
  proxy_pass http://nats:8080;
}

# --- защищённые пути под mTLS ---
location ~ ^/v1/(owner|pki|registry|drafts|devices)/ {
    if ($ssl_client_verify != SUCCESS) { return 400; }
    proxy_pass http://api.inimatic.com;
    include /etc/nginx/vhost.d/api.inimatic.com_location;
}
