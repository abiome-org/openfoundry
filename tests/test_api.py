import subprocess

import yaml
from fastapi.testclient import TestClient
from openfoundry.api import create_app
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.factory import Factory


def _paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "openfoundry.yaml").write_text(
        """apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata:
  name: api-test
  namespace: local/api-test
spec:
  owners: [local-user]
  extensions: {}
"""
    )
    paths = ProjectPaths(root)
    bootstrap(paths)
    return paths


def test_api_health_auth_schemas_resources_and_doctor(tmp_path):
    paths = _paths(tmp_path)
    (paths.root / "bindings").mkdir()
    (paths.root / "bindings/local.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Binding",
                "metadata": {"name": "local", "namespace": "local/api-test"},
                "spec": {"executor": "local", "resources": {}, "config": {}},
            }
        )
    )
    with Factory(paths) as factory:
        token = factory.secrets.get("local-api-token", "api-authentication").decode()
        operation_id = factory.operations.create("test", {"value": 1})["id"]
    with TestClient(create_app(paths)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/openapi.json").json()["info"]["version"] == "2.0.0"
        assert client.get("/v1/doctor").status_code == 403
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/doctor", headers=headers).json()["ready"]
        providers = client.get("/v1/executors", headers=headers).json()["providers"]
        assert {item["name"] for item in providers} >= {"local"}
        preflight = client.post(
            "/v1/executors/preflight",
            headers=headers,
            json={"binding": "bindings/local.yaml"},
        )
        assert preflight.status_code == 200
        assert preflight.json()["ready"]
        outside = paths.root.parent / "outside-binding.yaml"
        outside.write_text("sensitive-marker: must-not-escape")
        escaped = client.post(
            "/v1/executors/preflight",
            headers=headers,
            json={"binding": "../outside-binding.yaml"},
        )
        assert escaped.status_code == 400
        assert "must-not-escape" not in escaped.text
        assert "Project" in client.get("/v1/schemas", headers=headers).json()["kinds"]
        resource = {
            "apiVersion": "openfoundry.dev/v1alpha1",
            "kind": "ArtifactStore",
            "metadata": {"name": "second", "namespace": "local/api-test"},
            "spec": {"storeType": "filesystem", "location": ".openfoundry/second"},
        }
        response = client.post("/v1/resources", headers=headers, json=resource)
        assert response.status_code == 200
        assert response.json()["kind"] == "ArtifactStore"
        first_uid = response.json()["metadata"]["uid"]
        reapplied = client.post("/v1/resources", headers=headers, json=resource)
        assert reapplied.json()["metadata"]["uid"] == first_uid
        listed = client.get("/v1/resources?kind=ArtifactStore", headers=headers).json()
        assert len(listed) == 1
        assert client.get("/v1/resources?limit=1&offset=1", headers=headers).json() == []
        data_source = paths.root / "data.jsonl"
        data_source.write_text('{"value":1}\n')
        added_data = client.post(
            "/v1/data",
            headers=headers,
            json={
                "source": str(data_source),
                "name": "api-data",
                "mode": "copy",
                "rights": {"license": "CC0-1.0", "trainingAllowed": True},
            },
        )
        assert added_data.status_code == 200
        revoked_data = client.post(
            "/v1/data/api-data/revoke",
            headers=headers,
            json={"reason": "test withdrawal"},
        )
        assert revoked_data.status_code == 200
        assert revoked_data.json()["spec"]["rights"]["revoked"] is True
        operations = client.get("/v1/operations?state=pending", headers=headers).json()
        assert [item["id"] for item in operations] == [operation_id]
        assert client.get(f"/v1/operations/{operation_id}", headers=headers).json()["version"] == 1
        assert (
            client.post(f"/v1/operations/{operation_id}/reconcile", headers=headers).status_code
            == 400
        )

        created = client.post(
            "/v1/tokens",
            headers=headers,
            json={"actor": "alice", "scopes": ["read", "write"]},
        ).json()
        assert created["actor"] == "alice"
        listed_tokens = client.get("/v1/tokens", headers=headers).json()
        assert created["token"] not in repr(listed_tokens)
        bootstrap_token = next(item for item in listed_tokens if item["scopes"] == ["*"])
        assert (
            client.delete(f"/v1/tokens/{bootstrap_token['tokenId']}", headers=headers).status_code
            == 400
        )
        alice_headers = {"Authorization": f"Bearer {created['token']}"}
        assert client.get("/v1/doctor", headers=alice_headers).status_code == 200
        alice_resource = {
            **resource,
            "metadata": {"name": "alice-store", "namespace": "local/api-test"},
        }
        assert (
            client.post("/v1/resources", headers=alice_headers, json=alice_resource).status_code
            == 200
        )
        alice_events = client.get(
            "/v1/events?event_type=SpecValidated", headers=alice_headers
        ).json()
        assert any(event["actor"] == "alice" for event in alice_events)

        read_only = client.post(
            "/v1/tokens",
            headers=headers,
            json={"actor": "reader", "scopes": ["read"]},
        ).json()
        read_headers = {"Authorization": f"Bearer {read_only['token']}"}
        assert client.get("/v1/doctor", headers=read_headers).status_code == 200
        assert (
            client.post(
                "/v1/executors/preflight",
                headers=read_headers,
                json={"binding": "bindings/local.yaml"},
            ).status_code
            == 200
        )
        assert client.post("/v1/resources", headers=read_headers, json=resource).status_code == 403
        assert client.delete(f"/v1/tokens/{read_only['tokenId']}", headers=headers).json()[
            "revoked"
        ]
        assert client.get("/v1/doctor", headers=read_headers).status_code == 403


def test_agent_view_scopes_and_etags(tmp_path):
    paths = _paths(tmp_path)
    with Factory(paths) as factory:
        token = factory.secrets.get("local-api-token", "api-authentication").decode()
        reader_token, _principal = factory.api_tokens.create(actor="reader", scopes={"read"})
    headers = {"Authorization": f"Bearer {token}"}
    reader_headers = {"Authorization": f"Bearer {reader_token}"}

    with TestClient(create_app(paths)) as client:
        capabilities = client.get("/v1/agent/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.headers["etag"]
        assert (
            client.get(
                "/v1/agent/capabilities",
                headers={**headers, "If-None-Match": capabilities.headers["etag"]},
            ).status_code
            == 304
        )

        context = client.get("/v1/agent/context?limit=2&max_bytes=16384", headers=headers)
        assert context.status_code == 200
        assert context.json()["kind"] == "AgentContext"
        assert (
            client.get(
                "/v1/agent/context?limit=2&max_bytes=16384",
                headers={**headers, "If-None-Match": context.headers["etag"]},
            ).status_code
            == 304
        )

        assert client.get("/v1/agent/context", headers=reader_headers).status_code == 200
        assert (
            client.post("/v1/releases/demo/promote", headers=reader_headers, json={}).status_code
            == 403
        )
        invalid = client.post(
            "/v1/releases/demo/promote",
            headers=headers,
            json={"expected_version": "sensitive-input-must-not-echo"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "request_validation_error"
        assert "sensitive-input-must-not-echo" not in invalid.text
