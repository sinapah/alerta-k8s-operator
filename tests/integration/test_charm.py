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
import re
import time
import urllib.error
import urllib.parse
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


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def test_ingress(juju: jubilant.Juju):
    """Relate to Traefik and check the UI, config.json and API links carry the path prefix."""
    juju.deploy("traefik-k8s", app="traefik", channel="latest/stable", trust=True)
    juju.integrate("alerta:ingress", "traefik:ingress")
    juju.wait(lambda status: jubilant.all_active(status, "traefik", "alerta"), timeout=900)

    task = juju.run("traefik/0", "show-proxied-endpoints")
    endpoints = json.loads(task.results["proxied-endpoints"])
    url = endpoints["alerta"]["url"].rstrip("/")
    prefix = urllib.parse.urlparse(url).path

    deadline = time.monotonic() + 120
    while True:
        try:
            index = _get(f"{url}/")
            break
        except urllib.error.HTTPError as e:
            e.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(5)
    assert f'src="{prefix}/js/' in index
    assert 'src="/js/' not in index

    assert json.loads(_get(f"{url}/config.json")) == {
        "endpoint": f"{prefix}/api",
        "base_path": f"{prefix}/",
    }
    assert f'src="{prefix}/js/' in _get(f"{url}/alerts")
    assert _get(f"{url}/api/management/gtg").strip() == "OK"

    match = re.search(r"/js/(app\.[0-9a-f]+\.js)", index)
    assert match
    assert f'c.p="{prefix}/"' in _get(f"{url}/js/{match.group(1)}")

    alerts = json.loads(_get(f"{url}/api/alerts"))["alerts"]
    if not any(a["resource"] == "it-r1" for a in alerts):
        alert = {
            "resource": "it-r1",
            "event": "it-e1",
            "environment": "Production",
            "service": ["IT"],
            "severity": "major",
        }
        request = urllib.request.Request(
            f"{url}/api/alert",
            data=json.dumps(alert).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert json.load(response)["status"] == "ok"
        alerts = json.loads(_get(f"{url}/api/alerts"))["alerts"]
    assert alerts
    assert all(a["href"].startswith(f"{url}/api/alert/") for a in alerts)

    juju.remove_relation("alerta:ingress", "traefik:ingress")
    juju.wait(lambda status: jubilant.all_active(status, "alerta"), timeout=600)
    address = juju.status().apps["alerta"].units["alerta/0"].address
    assert json.loads(_get(f"http://{address}:8080/config.json")) == {
        "endpoint": "/api",
        "base_path": "/",
    }
