# Copyright 2026 sinapah
# See LICENSE file for licensing details.
# ruff: noqa: E501

"""Nginx configuration for the Alerta workload, aware of a Traefik path prefix."""

# Raw string: the config contains regex backslashes.
NGINX_CONFIG = r"""worker_processes 1;
pid /tmp/nginx.pid;
daemon off;
error_log stderr warn;

events {
    worker_connections 1024;
}

http {
    client_body_temp_path /tmp/client_body;
    fastcgi_temp_path /tmp/fastcgi_temp;
    proxy_temp_path /tmp/proxy_temp;
    scgi_temp_path /tmp/scgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    server_tokens off;
    gzip on;
    client_max_body_size 2M;

    log_format main '$remote_addr - $remote_user [$time_local] '
        '"$request" $status $body_bytes_sent '
        '"$http_referer" "$http_user_agent" - $request_time '
        'x_forwarded_for=$http_x_forwarded_for host=$host '
        '$request_id $sent_http_x_request_id';

    upstream backend {
        server 127.0.0.1:29000;
    }

    # Traefik strips the ingress path prefix and passes it in X-Forwarded-Prefix.
    # Anything that is not a plain path (including repeated headers) maps to "".
    map $http_x_forwarded_prefix $ui_prefix {
        default "";
        "~^(/[A-Za-z0-9._-]+)+$" $http_x_forwarded_prefix;
    }

    # Allows a literal dollar sign inside sub_filter patterns.
    geo $dollar {
        default "$";
    }

    server {
        listen 8080 default_server;
        access_log off;

        sub_filter_once off;
        sub_filter_types application/javascript;

        location ^~ /api {
            include /etc/nginx/uwsgi_params;
            uwsgi_pass backend;

            uwsgi_param Host $host;
            uwsgi_param X-Real-IP $remote_addr;
            uwsgi_param X-Forwarded-For $proxy_add_x_forwarded_for;
            uwsgi_param X-Forwarded-Proto $http_x_forwarded_proto;
        }

        root /srv/alerta/web;
        index index.html;

        # alerta-webui 8.7.1 fetches config.json from "/" and routes under its base_path.
        location = /config.json {
            default_type application/json;
            return 200 '{"endpoint": "$ui_prefix/api", "base_path": "$ui_prefix/"}';
        }

        # Patch the two root-absolute literals in the minified webui bundle (webui 8.7.1).
        location ~ ^/js/app\.[0-9a-f]+\.js$ {
            sub_filter 'c.p="/"' 'c.p="$ui_prefix/"';
            sub_filter 'var e="/";return this.${dollar}http.get("".concat(e,"config.json"))' 'var e="$ui_prefix/";return this.${dollar}http.get("".concat(e,"config.json"))';
        }

        location ~ \.html$|^/$ {
            sub_filter 'href="/' 'href="$ui_prefix/';
            sub_filter 'src="/' 'src="$ui_prefix/';
            sub_filter 'content="/' 'content="$ui_prefix/';
            try_files $uri $uri/ /index.html;
        }

        location / {
            try_files $uri $uri/ /index.html;
        }
    }
}
"""
