# Copyright 2026 sinapah
# See LICENSE file for licensing details.
#
# The integration tests use the Jubilant library and the pytest-jubilant plugin.
# See https://canonical.com/juju/docs/ops/latest/howto/write-integration-tests-for-a-charm/
#
# pytest-jubilant provides a module-scoped `juju` fixture that creates a temporary Juju model.
# The `charm` fixture is defined in conftest.py.

import json
import logging
import pathlib
import urllib.request

import jubilant
import pytest
import yaml

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())


@pytest.mark.juju_setup
def test_deploy(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy PostgreSQL and Alerta, relate them and wait for active."""
    juju.deploy("postgresql-k8s", app="pg", channel="16/stable", trust=True)
    resources = {"alerta-image": METADATA["resources"]["alerta-image"]["upstream-source"]}
    juju.deploy(charm, app="alerta", resources=resources)
    juju.integrate("pg:database", "alerta:database")
    juju.wait(lambda status: jubilant.all_active(status, "pg", "alerta"), timeout=1200)


def test_alerta_api(juju: jubilant.Juju):
    """Check the API health, post an alert and read it back through PostgreSQL."""
    address = juju.status().apps["alerta"].units["alerta/0"].address
    base = f"http://{address}:8080"

    with urllib.request.urlopen(f"{base}/api/management/gtg", timeout=30) as response:
        assert response.read().decode().strip() == "OK"

    alert = {
        "resource": "it-r1",
        "event": "it-e1",
        "environment": "Production",
        "service": ["IT"],
        "severity": "major",
    }
    request = urllib.request.Request(
        f"{base}/api/alert",
        data=json.dumps(alert).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert json.load(response)["status"] == "ok"

    with urllib.request.urlopen(f"{base}/api/alerts", timeout=30) as response:
        assert "it-r1" in response.read().decode()

    with urllib.request.urlopen(f"{base}/", timeout=30) as response:
        assert "<title>Alerta</title>" in response.read().decode()
