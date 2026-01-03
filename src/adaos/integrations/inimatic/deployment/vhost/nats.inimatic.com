# ssl_protocols TLSv1.2 TLSv1.3;

proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_connect_timeout 60s;
client_max_body_size 10m;

# NATS WebSocket passthrough (no mTLS)
location ^~ /nats {
  ssl_verify_client off;

  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
  proxy_read_timeout  60s;
  proxy_send_timeout  60s;
  proxy_connect_timeout 5s;

  # Keep trailing slash so `/nats` maps to `/` on upstream.
  proxy_pass http://nats:8080/;
}
