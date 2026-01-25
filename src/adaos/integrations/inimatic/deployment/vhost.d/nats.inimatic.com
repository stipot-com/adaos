# vhost.d/nats.inimatic.com
client_max_body_size 10m;

# NATS WebSocket passthrough (no mTLS)
location ^~ /nats {

  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_read_timeout  3600s;
  proxy_send_timeout  3600s;
  proxy_connect_timeout 10s;

  proxy_buffering off;
  proxy_request_buffering off;

  # Forward to the backend ws-nats-proxy so hub tokens work on this host too.
  proxy_pass https://api.inimatic.com;
}
