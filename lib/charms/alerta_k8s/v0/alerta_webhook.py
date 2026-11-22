# Copyright 2026 sinapah
# See LICENSE file for licensing details.

"""Relation interface for sending Alertmanager alerts to Alerta (`alerta_webhook`).

This file is self-contained: it imports only the standard library, `ops` and `pydantic`.

Databag layout
==============

The provider (Alerta) writes one key in its application databag:

    url: "http://alerta-k8s.test.svc.cluster.local:8080/api"

`url` is the Alerta API base URL, without a trailing slash. It is the ingress URL (plus `/api`)
when ingress is active, otherwise the in-cluster service address.

Provider usage (Alerta charm)
=============================

    from charms.alerta_k8s.v0.alerta_webhook import AlertaProvider

    class AlertaCharm(ops.CharmBase):
        def __init__(self, framework):
            super().__init__(framework)
            self.alertmanagers = AlertaProvider(self, relation_name="alerta-webhook")

        def _reconcile(self, _):
            self.alertmanagers.publish("http://alerta.example/api")

`publish` is a no-op on non-leader units. The provider observes no events; call `publish` from
the charm's reconcile loop.

Requirer usage (Alertmanager charm)
===================================

    from charms.alerta_k8s.v0.alerta_webhook import AlertaRequirer

    class AlertmanagerCharm(ops.CharmBase):
        def __init__(self, framework):
            super().__init__(framework)
            self.alerta = AlertaRequirer(self)
            framework.observe(self.alerta.on.alerta_changed, self._update_config)

        def _build_config(self) -> str:
            config = {...}
            config = self.alerta.apply_to_config(config)
            return yaml.safe_dump(config)

The requirer relation should be declared with `limit: 1`. `apply_to_config` adds a receiver named
`alerta` (reserved for this relation) that POSTs every alert, firing and resolved, to Alerta's
Prometheus webhook, and a first child route with `continue: true` so existing routing is
untouched. When the relation is gone, the receiver and route are removed.
"""

import copy
import logging
from typing import Any

import ops
import pydantic

logger = logging.getLogger(__name__)

INTERFACE_NAME = "alerta_webhook"
DEFAULT_PROVIDER_RELATION_NAME = "alerta-webhook"
DEFAULT_REQUIRER_RELATION_NAME = "alerta"
WEBHOOK_PATH = "/webhooks/prometheus"
RECEIVER_NAME = "alerta"


class AlertaProviderAppData(pydantic.BaseModel):
    """Application databag published by the provider."""

    url: str = pydantic.Field(pattern=r"^https?://\S+$")


def _validate_endpoint(
    charm: ops.CharmBase, relation_name: str, role: ops.RelationRole
) -> None:
    """Raise ValueError unless the charm declares the endpoint with the given role/interface."""
    meta = charm.meta.relations.get(relation_name)
    if meta is None or meta.role != role or meta.interface_name != INTERFACE_NAME:
        raise ValueError(
            f"relation {relation_name!r} must be declared in charm metadata with role "
            f"{role.name!r} and interface {INTERFACE_NAME!r}"
        )


class AlertaProvider(ops.Object):
    """Publishes the Alerta API URL to related Alertmanagers."""

    def __init__(
        self, charm: ops.CharmBase, relation_name: str = DEFAULT_PROVIDER_RELATION_NAME
    ):
        super().__init__(charm, relation_name)
        _validate_endpoint(charm, relation_name, ops.RelationRole.provides)
        self._charm = charm
        self._relation_name = relation_name

    def publish(self, url: str) -> None:
        """Write the Alerta API base URL to every active relation (leader only)."""
        if not self._charm.unit.is_leader():
            return
        data = AlertaProviderAppData(url=url.rstrip("/")).model_dump()
        for relation in self._charm.model.relations[self._relation_name]:
            if relation.active:
                relation.data[self._charm.app].update(data)


class AlertaChangedEvent(ops.EventBase):
    """The Alerta URL, or its presence, may have changed."""


class AlertaRequirerEvents(ops.ObjectEvents):
    """Events emitted by `AlertaRequirer`."""

    alerta_changed = ops.EventSource(AlertaChangedEvent)


class AlertaRequirer(ops.Object):
    """Reads the Alerta URL published by the related Alerta charm."""

    on = AlertaRequirerEvents()  # pyright: ignore

    def __init__(
        self, charm: ops.CharmBase, relation_name: str = DEFAULT_REQUIRER_RELATION_NAME
    ):
        super().__init__(charm, relation_name)
        _validate_endpoint(charm, relation_name, ops.RelationRole.requires)
        self._charm = charm
        self._relation_name = relation_name
        self.framework.observe(charm.on[relation_name].relation_changed, self._on_relation_event)
        self.framework.observe(charm.on[relation_name].relation_broken, self._on_relation_event)

    def _on_relation_event(self, _: ops.EventBase) -> None:
        self.on.alerta_changed.emit()

    def get_url(self) -> str | None:
        """Return the Alerta API base URL, or None if unavailable."""
        for relation in self._charm.model.relations[self._relation_name]:
            if not relation.active or relation.app is None:
                continue
            data = dict(relation.data[relation.app])
            if not data:
                continue
            try:
                return AlertaProviderAppData(**data).url
            except pydantic.ValidationError as err:
                logger.warning("invalid or missing alerta relation data: %s", err)
        return None

    def get_webhook_url(self) -> str | None:
        """Return Alerta's Prometheus webhook URL, or None if unavailable."""
        url = self.get_url()
        return url + WEBHOOK_PATH if url else None

    def apply_to_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of an Alertmanager config with the Alerta receiver applied."""
        return add_alerta_receiver(config, self.get_webhook_url())


def add_alerta_receiver(config: dict[str, Any], webhook_url: str | None) -> dict[str, Any]:
    """Return a copy of an Alertmanager config routing all alerts to Alerta.

    Any existing receiver/route named `alerta` is removed first (idempotent). With
    `webhook_url=None` the result has no Alerta receiver. The input is never mutated.
    """
    result = copy.deepcopy(config)

    if "receivers" in result:
        result["receivers"] = [r for r in result["receivers"] if r.get("name") != RECEIVER_NAME]
    route = result.get("route")
    if route and "routes" in route:
        route["routes"] = [r for r in route["routes"] if r.get("receiver") != RECEIVER_NAME]

    if webhook_url is None:
        return result

    result.setdefault("receivers", []).append(
        {
            "name": RECEIVER_NAME,
            "webhook_configs": [{"url": webhook_url, "send_resolved": True}],
        }
    )
    route = result.setdefault("route", {})
    route.setdefault("routes", []).insert(0, {"receiver": RECEIVER_NAME, "continue": True})
    return result
