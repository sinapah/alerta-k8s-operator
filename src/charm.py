#!/usr/bin/env python3
# Copyright 2026 sinapah
# See LICENSE file for licensing details.

"""Charm the Alerta alert consolidation server."""

import logging
import secrets
from urllib.parse import quote

import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

from nginx_config import NGINX_CONFIG

logger = logging.getLogger(__name__)

CONTAINER_NAME = "alerta"
UWSGI_SERVICE = "uwsgi"
NGINX_SERVICE = "nginx"
SERVICES = (UWSGI_SERVICE, NGINX_SERVICE)
NGINX_CONFIG_PATH = "/etc/alerta/nginx.conf"
HTTP_PORT = 8080
INGRESS_RELATION = "ingress"
DATABASE_RELATION = "database"
METRICS_RELATION = "metrics-endpoint"
METRICS_PATH = "/api/management/metrics"
METRICS_JOB = "alerta"
DATABASE_NAME = "alerta"
SECRET_KEY_LABEL = "secret-key"
LAYER_NAME = "alerta"


class AlertaK8SCharm(ops.CharmBase):
    """Charm the Alerta workload."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container(CONTAINER_NAME)
        self.database = DatabaseRequires(
            self, relation_name=DATABASE_RELATION, database_name=DATABASE_NAME
        )
        self.ingress = IngressPerAppRequirer(
            self, relation_name=INGRESS_RELATION, port=HTTP_PORT, strip_prefix=True
        )
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            relation_name=METRICS_RELATION,
            jobs=[
                {
                    "job_name": METRICS_JOB,
                    "metrics_path": METRICS_PATH,
                    "static_configs": [{"targets": [f"*:{HTTP_PORT}"]}],
                }
            ],
        )

        for event in (
            self.on[CONTAINER_NAME].pebble_ready,
            self.on[CONTAINER_NAME].pebble_check_failed,
            self.on[CONTAINER_NAME].pebble_check_recovered,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.database.on.database_created,
            self.database.on.endpoints_changed,
            self.on[DATABASE_RELATION].relation_broken,
            self.ingress.on.ready,
            self.ingress.on.revoked,
        ):
            framework.observe(event, self._reconcile)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the workload and unit status on the current state."""
        self.unit.set_ports(HTTP_PORT)

        if self.unit.is_leader():
            self._ensure_secret_key()

        if not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for Pebble")
            return

        if self._push_nginx_config():
            self.container.restart(NGINX_SERVICE)

        if not [r for r in self.model.relations[DATABASE_RELATION] if r.active]:
            self.unit.status = ops.BlockedStatus("missing relation: database")
            return

        url = self._database_url()
        if url is None:
            self.unit.status = ops.WaitingStatus("waiting for database credentials")
            return

        key = self._secret_key()
        if key is None:
            self.unit.status = ops.WaitingStatus("waiting for secret key")
            return

        layer: ops.pebble.LayerDict = {
            "summary": "Alerta layer",
            "services": {
                UWSGI_SERVICE: {
                    "override": "merge",
                    "environment": {
                        "DATABASE_URL": url,
                        "SECRET_KEY": key,
                        "BASE_URL": self._api_base_url(),
                    },
                }
            },
        }
        self.container.add_layer(LAYER_NAME, layer, combine=True)
        self.container.replan()

        if self._workload_ready():
            self.unit.status = ops.ActiveStatus()
        else:
            self.unit.status = ops.WaitingStatus("waiting for Alerta to be ready")

    def _push_nginx_config(self) -> bool:
        """Write the nginx config into the workload; return whether it changed."""
        try:
            with self.container.pull(NGINX_CONFIG_PATH) as current:
                if current.read() == NGINX_CONFIG:
                    return False
        except ops.pebble.PathError:
            pass
        self.container.push(NGINX_CONFIG_PATH, NGINX_CONFIG, make_dirs=True, permissions=0o644)
        return True

    def _api_base_url(self) -> str:
        """Return the external API base URL for Alerta, or "" without ingress."""
        if not any(r.active for r in self.model.relations[INGRESS_RELATION]):
            return ""
        url = self.ingress.url
        return f"{url.rstrip('/')}/api" if url else ""

    def _workload_ready(self) -> bool:
        """Return whether all Alerta services run and the ready checks are up."""
        services = self.container.get_services(*SERVICES)
        if len(services) != len(SERVICES) or not all(s.is_running() for s in services.values()):
            return False
        checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
        return all(c.status == ops.pebble.CheckStatus.UP for c in checks.values())

    def _database_url(self) -> str | None:
        """Build the PostgreSQL URL from the relation data, or None if not yet available."""
        data = self.database.fetch_relation_data(fields=["endpoints", "username", "password"])
        if not data:
            return None
        fields = next(iter(data.values()))
        endpoints = fields.get("endpoints")
        username = fields.get("username")
        password = fields.get("password")
        if not (endpoints and username and password):
            return None
        endpoint = endpoints.split(",")[0]
        return (
            f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{endpoint}/{DATABASE_NAME}"
        )

    def _ensure_secret_key(self) -> None:
        """Create the application secret holding Alerta's SECRET_KEY if missing."""
        try:
            self.model.get_secret(label=SECRET_KEY_LABEL)
        except ops.SecretNotFoundError:
            self.app.add_secret({"key": secrets.token_urlsafe(32)}, label=SECRET_KEY_LABEL)

    def _secret_key(self) -> str | None:
        """Return Alerta's SECRET_KEY, or None if the leader has not created it yet."""
        try:
            return self.model.get_secret(label=SECRET_KEY_LABEL).peek_content()["key"]
        except ops.SecretNotFoundError:
            return None


if __name__ == "__main__":  # pragma: nocover
    ops.main(AlertaK8SCharm)
