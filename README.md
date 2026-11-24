# alerta-k8s-operator

Juju Kubernetes charm for [Alerta](https://alerta.io), the alert consolidation server and web UI. Requires PostgreSQL through the `database` relation (for example `postgresql-k8s` on channel `16/stable`).

## Libraries

`alerta_webhook/` is a Python package implementing the `alerta_webhook` interface (`AlertaProvider`, `AlertaRequirer`). Use it from another charm with:

    uv add "alerta-webhook @ git+https://github.com/sinapah/alerta-k8s-operator#subdirectory=alerta_webhook"

and set `build-packages: [git]` on the charm part in `charmcraft.yaml`.
