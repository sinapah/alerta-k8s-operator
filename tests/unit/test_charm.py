# Copyright 2026 sinapah
# See LICENSE file for licensing details.

import json

import ops
import pytest
from ops import testing

from charm import AlertaK8SCharm

EXPECTED_URL = (
    "postgresql://relation-1:p%40ss%2Fw%3Ard@pg-primary.test.svc.cluster.local:5432/alerta"
)

ROCK_LAYER = ops.pebble.Layer(
    {
        "services": {
            "uwsgi": {"override": "replace", "command": "uwsgi", "startup": "enabled"},
            "nginx": {"override": "replace", "command": "nginx", "startup": "enabled"},
        },
        "checks": {
            "alerta-ready": {
                "override": "replace",
                "level": "ready",
                "http": {"url": "http://localhost:8080/api/management/gtg"},
            }
        },
    }
)


def container(check_status: ops.pebble.CheckStatus = ops.pebble.CheckStatus.UP):
    return testing.Container(
        "alerta",
        can_connect=True,
        layers={"rock": ROCK_LAYER},
        service_statuses={
            "uwsgi": ops.pebble.ServiceStatus.ACTIVE,
            "nginx": ops.pebble.ServiceStatus.ACTIVE,
        },
        check_infos={
            testing.CheckInfo(
                "alerta-ready", level=ops.pebble.CheckLevel.READY, status=check_status
            )
        },
    )


def relation(with_data: bool = True):
    data = {
        "endpoints": "pg-primary.test.svc.cluster.local:5432",
        "username": "relation-1",
        "password": "p@ss/w:rd",
        "database": "alerta",
    }
    return testing.Relation(
        endpoint="database",
        interface="postgresql_client",
        remote_app_data=data if with_data else {},
    )


def ingress_relation():
    return testing.Relation(
        endpoint="ingress",
        interface="ingress",
        remote_app_data={"ingress": json.dumps({"url": "http://10.0.0.1/test-alerta-k8s"})},
    )


def secret():
    return testing.Secret(tracked_content={"key": "k"}, label="secret-key", owner="app")


@pytest.fixture
def ctx():
    return testing.Context(AlertaK8SCharm)


def test_blocked_without_database_relation(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers=[container()], secrets=[secret()]),
    )
    assert state.unit_status == testing.BlockedStatus("missing relation: database")


def test_waiting_for_database_credentials(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(
            leader=True,
            containers=[container()],
            relations=[relation(with_data=False)],
            secrets=[secret()],
        ),
    )
    assert state.unit_status == testing.WaitingStatus("waiting for database credentials")


def test_active_and_layer_environment(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(
            leader=True,
            containers=[container()],
            relations=[relation()],
            secrets=[secret()],
        ),
    )
    assert state.unit_status == testing.ActiveStatus()
    layer = state.get_container("alerta").layers["alerta"]
    assert layer.services["uwsgi"].environment == {
        "DATABASE_URL": EXPECTED_URL,
        "SECRET_KEY": "k",
        "BASE_URL": "",
    }


def test_base_url_from_ingress(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(
            leader=True,
            containers=[container()],
            relations=[relation(), ingress_relation()],
            secrets=[secret()],
        ),
    )
    layer = state.get_container("alerta").layers["alerta"]
    assert layer.services["uwsgi"].environment["BASE_URL"] == (
        "http://10.0.0.1/test-alerta-k8s/api"
    )


def test_base_url_cleared_when_ingress_removed(ctx):
    ingress = ingress_relation()
    state = ctx.run(
        ctx.on.relation_broken(ingress),
        testing.State(
            leader=True,
            containers=[container()],
            relations=[relation(), ingress],
            secrets=[secret()],
        ),
    )
    layer = state.get_container("alerta").layers["alerta"]
    assert layer.services["uwsgi"].environment["BASE_URL"] == ""


def test_leader_creates_secret_key(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers=[container()], relations=[relation()]),
    )
    created = [s for s in state.secrets if s.label == "secret-key"]
    assert len(created) == 1
    assert created[0].latest_content["key"]
    assert state.unit_status == testing.ActiveStatus()


def test_non_leader_waits_for_secret_key(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(leader=False, containers=[container()], relations=[relation()]),
    )
    assert state.unit_status == testing.WaitingStatus("waiting for secret key")


def test_waiting_when_ready_check_down(ctx):
    state = ctx.run(
        ctx.on.update_status(),
        testing.State(
            leader=True,
            containers=[container(ops.pebble.CheckStatus.DOWN)],
            relations=[relation()],
            secrets=[secret()],
        ),
    )
    assert state.unit_status == testing.WaitingStatus("waiting for Alerta to be ready")
