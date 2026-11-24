# Copyright 2026 sinapah
# See LICENSE file for licensing details.

import copy

import ops
import pytest
from ops import testing

from alerta_webhook import (
    AlertaProvider,
    AlertaRequirer,
    add_alerta_receiver,
)

URL = "http://a.test:8080/api"
WEBHOOK = "http://a.test:8080/api/webhooks/prometheus"
REQUIRER_META = {"name": "am", "requires": {"alerta": {"interface": "alerta_webhook"}}}


class RequirerCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.alerta = AlertaRequirer(self)
        framework.observe(self.alerta.on.alerta_changed, self._on_changed)

    def _on_changed(self, _):
        self.unit.status = ops.ActiveStatus(self.alerta.get_webhook_url() or "none")


@pytest.fixture
def ctx():
    return testing.Context(RequirerCharm, meta=REQUIRER_META)


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"url": URL}, WEBHOOK),
        ({}, "none"),
        ({"url": "ftp://x"}, "none"),
    ],
)
def test_requirer_reads_webhook_url(ctx, data, expected):
    rel = testing.Relation(endpoint="alerta", interface="alerta_webhook", remote_app_data=data)
    state = ctx.run(ctx.on.relation_changed(rel), testing.State(relations=[rel]))
    assert state.unit_status == testing.ActiveStatus(expected)


def test_requirer_ignores_broken_relation(ctx):
    rel = testing.Relation(
        endpoint="alerta", interface="alerta_webhook", remote_app_data={"url": URL}
    )
    state = ctx.run(ctx.on.relation_broken(rel), testing.State(relations=[rel]))
    assert state.unit_status == testing.ActiveStatus("none")


def user_config():
    return {
        "route": {"receiver": "slack", "routes": [{"receiver": "slack", "matchers": ["a=b"]}]},
        "receivers": [{"name": "slack"}],
    }


def test_add_receiver_preserves_existing_routing_and_input():
    config = user_config()
    original = copy.deepcopy(config)
    result = add_alerta_receiver(config, WEBHOOK)

    assert config == original
    assert result["route"]["routes"][0] == {"receiver": "alerta", "continue": True}
    assert result["route"]["routes"][1] == original["route"]["routes"][0]
    assert result["receivers"][0] == {"name": "slack"}
    assert result["receivers"][-1] == {
        "name": "alerta",
        "webhook_configs": [{"url": WEBHOOK, "send_resolved": True}],
    }


def test_add_receiver_is_idempotent():
    once = add_alerta_receiver(user_config(), WEBHOOK)
    assert add_alerta_receiver(once, WEBHOOK) == once


def test_none_url_removes_stale_receiver():
    with_alerta = add_alerta_receiver(user_config(), WEBHOOK)
    assert add_alerta_receiver(with_alerta, None) == user_config()


def test_add_receiver_creates_missing_keys():
    result = add_alerta_receiver({}, WEBHOOK)
    assert [r["name"] for r in result["receivers"]] == ["alerta"]
    assert result["route"]["routes"] == [{"receiver": "alerta", "continue": True}]


@pytest.mark.parametrize(
    "meta",
    [
        {"name": "x", "provides": {"alerta-webhook": {"interface": "other"}}},
        {"name": "x", "requires": {"alerta-webhook": {"interface": "alerta_webhook"}}},
        {"name": "x"},
    ],
)
def test_provider_rejects_bad_endpoint(meta):
    class Charm(ops.CharmBase):
        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            with pytest.raises(ValueError, match="alerta-webhook"):
                AlertaProvider(self, "alerta-webhook")

    ctx = testing.Context(Charm, meta=meta)
    ctx.run(ctx.on.start(), testing.State())
