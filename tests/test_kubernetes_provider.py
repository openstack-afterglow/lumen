"""Deterministic HTTP contract tests; no Kubernetes writes or cluster required."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import ssl
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from lumen.services.infrastructure import kubernetes as k
from lumen.services.infrastructure.config import (
    FixedWorkerIdentity,
    KubernetesClusterProfile,
    KubernetesProfile,
    PoolConfig,
    RuntimeConfig,
    TlsMaterial,
)
from lumen.services.infrastructure.providers import (
    OWNER_FINGERPRINT,
    OWNER_POLICY,
    OWNER_RESOURCE,
    ResourceIntent,
    ResourceRef,
    ownership,
)


class API:
    """Small API-server-shaped fixture, including GET404 and UID CAS deletion."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.sequence = 10
        self.fail_post = None
        self.delay_delete = False
        self.delay_delete_kind = None
        self.canonicalize_pod = False
        self.namespace = {
            "metadata": {"uid": str(UUID(int=1)), "labels": {
                k._KEYS["lumen_deployment"]: "lumen", k._KEYS["lumen_pool"]: "mcp",
                "pod-security.kubernetes.io/enforce": "restricted",
            }, "annotations": {k._KEYS["lumen_deployment"]: "lumen", k._KEYS["lumen_pool"]: "mcp"}},
        }
        self.runtime = {"apiVersion": "node.k8s.io/v1", "kind": "RuntimeClass",
                        "metadata": {"uid": str(UUID(int=2)), "name": "sandbox"}, "handler": "runsc"}
        self.rules = [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "create", "delete"]},
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create", "delete"]},
            {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": ["get", "list", "create", "delete"]},
            {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list", "create", "delete"]},
        ]
        self.objects[("networkpolicies", "lumen-default-deny")] = {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "lumen-default-deny", "namespace": "runs", "uid": str(UUID(int=3))},
            # The API omits empty ingress/egress arrays in real JSON responses.
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        }

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.url.params)))
        path = request.url.path
        if path == "/api/v1/namespaces/runs":
            return httpx.Response(200, json=copy.deepcopy(self.namespace))
        if path == "/apis/node.k8s.io/v1/runtimeclasses/sandbox":
            return httpx.Response(200, json=copy.deepcopy(self.runtime))
        if path == "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
            return httpx.Response(200, json={"status": {"resourceRules": self.rules, "incomplete": False}})
        parts = path.split("/")
        if parts[-1] in {"pods", "secrets", "networkpolicies", "services"}:
            kind, name = parts[-1], None
        else:
            kind, name = parts[-2:]
        if request.method == "GET":
            if name is None:
                items = [copy.deepcopy(value) for (item_kind, _), value in self.objects.items() if item_kind == kind]
                return httpx.Response(200, json={"items": items, "metadata": {}})
            item = self.objects.get((kind, name))
            return httpx.Response(200, json=copy.deepcopy(item)) if item else httpx.Response(404, json={})
        if request.method == "POST":
            if self.fail_post == kind:
                raise httpx.ReadTimeout("unknown create", request=request)
            self.sequence += 1
            item = copy.deepcopy(body)
            item["metadata"]["uid"] = str(UUID(int=self.sequence))
            name = item["metadata"]["name"]
            if (kind, name) in self.objects:
                return httpx.Response(409, json={"message": "private upstream message"})
            if kind == "pods":
                item["status"] = {"phase": "Running", "podIP": "10.42.0.5", "hostIP": "192.168.50.20"}
                if self.canonicalize_pod:
                    item["spec"]["nodeName"] = "worker-1"
                    for key in ("hostNetwork", "hostPID", "hostIPC"):
                        item["spec"].pop(key)
                    for limits in item["spec"]["containers"][0]["resources"].values():
                        limits["cpu"] = "1"
            elif kind == "services":
                item["spec"]["clusterIP"] = "10.43.0.5"
                item["spec"]["ports"][0]["nodePort"] = 30000 + self.sequence
            self.objects[(kind, name)] = item
            return httpx.Response(201, json=copy.deepcopy(item))
        if request.method == "DELETE":
            item = self.objects.get((kind, name))
            if item is None:
                return httpx.Response(404, json={})
            if body["preconditions"]["uid"] != item["metadata"]["uid"]:
                return httpx.Response(409, json={})
            if not self.delay_delete and self.delay_delete_kind != kind:
                del self.objects[(kind, name)]
            return httpx.Response(200, json={"apiVersion": "v1", "kind": "Status", "status": "Success"})
        raise AssertionError((request.method, path))


def _pool(manifest_file="/private/manifest.json", manifest_sha256="b" * 64):
    return {
        "name": "mcp", "role": "sandbox", "backend": "kubernetes", "enabled": True,
        "image": "registry.example/sandbox@sha256:" + "a" * 64, "architecture": "x86_64",
        "min_replicas": 0, "max_replicas": 5,
        "profile": {"backend": "kubernetes", "cluster_profile_id": "cluster",
                    "tool_manifest_file": manifest_file, "tool_manifest_sha256": manifest_sha256,
                    "controller_ingress_cidrs": ["192.168.50.10/32"],
                    "bootstrap_egress_cidrs": ["192.168.50.11/32"]},
        "sandbox": {"workspace_bytes": 16 * 1024**2, "memory_bytes": 256 * 1024**2,
                    "cpu_millis": 1000, "pids_limit": 128},
    }


def _cluster(qualification_file="/private/qualification.json", qualification_sha256="c" * 64):
    return {
        "id": "cluster", "server": "https://kubernetes.example:6443", "ca_file": "/private/kubernetes-ca.pem",
        "bearer_token": {"env": "KUBERNETES_OPERATOR_TOKEN"}, "namespace": "runs",
        "runtime_class_name": "sandbox", "runtime_class_handler": "runsc",
        "cluster_cidrs": ["10.42.0.0/16", "10.43.0.0/16", "2606:4700:1234::/48"],
        "qualification_file": qualification_file, "qualification_sha256": qualification_sha256,
    }


@pytest.fixture
def provider(tmp_path, monkeypatch):
    api = API()
    manifest = b'{"version":1,"profiles":[]}'
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_bytes(manifest)
    pool = PoolConfig.model_validate(_pool(str(manifest_file), hashlib.sha256(manifest).hexdigest()))
    qualification = {
        "version": 1, "server": "https://kubernetes.example:6443", "namespace": "runs",
        "namespace_uid": api.namespace["metadata"]["uid"], "runtime_class_name": "sandbox",
        "runtime_class_handler": "runsc", "runtime_class_uid": api.runtime["metadata"]["uid"],
        "runtime_class_spec_sha256": k._digest({"handler": "runsc"}),
        "cluster_cidrs": _cluster()["cluster_cidrs"], "pool_digest": pool.digest(),
        "runtime_probe_sha256": "d" * 64, "cni_probe_sha256": "e" * 64, "measured_pids_max": 128,
    }
    qualification_file = tmp_path / "qualification.json"
    raw = json.dumps(qualification).encode()
    qualification_file.write_bytes(raw)
    cluster = KubernetesClusterProfile.model_validate(_cluster(str(qualification_file), hashlib.sha256(raw).hexdigest()))
    monkeypatch.setenv("KUBERNETES_OPERATOR_TOKEN", "controller-only-token")
    monkeypatch.setattr(k.ssl, "create_default_context", lambda **kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    result = k.KubernetesProvider(cluster, pool, "lumen", controller_url="https://192.168.50.11:8013",
                                  controller_ca_pem="-----BEGIN CERTIFICATE-----\ncontroller-ca\n")
    result.client.close()
    result.client = httpx.Client(base_url=cluster.server, transport=httpx.MockTransport(api),
                                 follow_redirects=False, trust_env=False)
    yield result, api
    result.client.close()


def _intent(provider):
    return ResourceIntent(resource_id="00000000-0000-0000-0000-000000000001", generation=1,
                          pool_id="mcp", role="sandbox", image_ref=provider.pool.image,
                          policy_digest=provider.pool.digest(), request_fingerprint="f" * 64,
                          bootstrap_token="one-use-bootstrap-token")


def _create(provider):
    result, api = provider
    intent = _intent(result)
    observation = result.create(intent)
    ref = ResourceRef(intent.resource_id, intent.generation, observation.provider_id, intent.pool_id)
    return result, api, intent, observation, ref


def test_kubernetes_config_strict_json_coherence_and_secret_redaction():
    runtime = RuntimeConfig.model_validate_json(json.dumps({"kubernetes_clusters": [_cluster()], "pools": [_pool()]}), strict=True)
    assert isinstance(runtime.pool("mcp").profile, KubernetesProfile)
    assert runtime.kubernetes_cluster("cluster").namespace == "runs"
    public = json.dumps(runtime.public_view())
    for private in ("KUBERNETES_OPERATOR_TOKEN", "/private/kubernetes-ca.pem", "/private/qualification.json", "/private/manifest.json", "bearer_token"):
        assert private not in public
    with pytest.raises(KeyError):
        runtime.kubernetes_cluster("missing")
    with pytest.raises(ValidationError, match="duplicate Kubernetes"):
        RuntimeConfig.model_validate({"kubernetes_clusters": [_cluster(), _cluster()]})
    with pytest.raises(ValidationError, match="unknown Kubernetes"):
        RuntimeConfig.model_validate({"pools": [_pool()]})


@pytest.mark.parametrize("changed", [
    {"role": "worker"}, {"min_replicas": 1}, {"max_replicas": 0}, {"image": "sandbox:latest"},
    {"cloud_profile_id": "dummy"}, {"network_id": "dummy"}, {"security_group_ids": ["dummy"]},
    {"guest_profile_id": "trusted"},
])
def test_kubernetes_rejects_trusted_warm_mutable_or_openstack_pools(changed):
    with pytest.raises(ValidationError):
        PoolConfig.model_validate(_pool() | changed)


@pytest.mark.parametrize("changed", [
    {"server": "http://kubernetes.example"}, {"server": "https://user:secret@kubernetes.example"},
    {"server": "https://kubernetes.example/path"}, {"runtime_class_handler": "runc"},
    {"cluster_cidrs": ["10.42.0.1/16"]},
])
def test_cluster_rejects_ambient_insecure_or_unqualified_endpoints(changed):
    with pytest.raises(ValidationError):
        KubernetesClusterProfile.model_validate(_cluster() | changed)


def test_nova_still_requires_openstack_network_fields():
    values = _pool() | {"backend": "nova", "profile": {"backend": "nova", "flavor_id": "f",
        "guest_image_id": "i", "guest_image_hash": "a" * 64}}
    with pytest.raises(ValidationError, match="Nova/Zun pools require"):
        PoolConfig.model_validate(values)


def test_fixed_worker_configuration_has_unique_dedicated_identities():
    worker = FixedWorkerIdentity(id="worker-1", certificate_fingerprint="a" * 64)
    with pytest.raises(ValidationError, match="duplicate fixed worker identity"):
        RuntimeConfig(fixed_workers=(worker, worker))
    with pytest.raises(ValidationError, match="duplicate fixed worker certificate"):
        RuntimeConfig(fixed_workers=(worker, worker.model_copy(update={"id": "worker-2"})))
    tls = {"ca_file": "/ca", "ca_key_file": "/ca-key", "cert_file": "/server", "key_file": "/server-key",
           "operator_client_cert_file": "/probe", "operator_client_key_file": "/probe-key"}
    with pytest.raises(ValidationError, match="configured together"):
        TlsMaterial(**tls, worker_client_cert_file="/worker")
    for cert, key in (("/server", "/worker-key"), ("/worker", "/probe-key"), ("/worker", "/worker")):
        with pytest.raises(ValidationError, match="must be separate"):
            TlsMaterial(**tls, worker_client_cert_file=cert, worker_client_key_file=key)


def test_preflight_only_reads_and_nonpersisted_authorization_review(provider):
    result, api = provider
    result.preflight()
    assert all(method == "GET" or path.endswith("/selfsubjectrulesreviews") for method, path, _, _ in api.calls)
    assert not any("secrets" in path for _, path, _, _ in api.calls)
    services = [(path, params) for method, path, _, params in api.calls
                if method == "GET" and path.endswith("/services")]
    assert len(services) == 1
    assert services[0][0] == "/api/v1/namespaces/runs/services"
    assert "labelSelector" not in services[0][1]


def test_preflight_accepts_default_selfsubjectreview_create_grants(provider):
    result, api = provider
    api.rules.extend([
        {"apiGroups": ["authentication.k8s.io"], "resources": ["selfsubjectreviews"], "verbs": ["create"]},
        {"apiGroups": ["authorization.k8s.io"],
         "resources": ["selfsubjectaccessreviews", "selfsubjectrulesreviews"], "verbs": ["create"]},
    ])
    result.preflight()
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("rule", [
    {"apiGroups": ["authentication.k8s.io"], "resources": ["selfsubjectreviews"], "verbs": ["create", "get"]},
    {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]},
    {"apiGroups": ["authentication.k8s.io"], "resources": ["*"], "verbs": ["create"]},
    {"apiGroups": ["*"], "resources": ["selfsubjectreviews"], "verbs": ["create"]},
    {"apiGroups": ["authentication.k8s.io"], "resources": ["selfsubjectreviews"], "verbs": ["*"]},
    {"apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["services"], "verbs": ["update"]},
])
def test_preflight_rejects_broader_authentication_and_unrelated_grants_before_writes(provider, rule):
    result, api = provider
    api.rules.append(rule)
    with pytest.raises(ValueError):
        result.create(_intent(result))
    assert not any(method == "POST" and not path.endswith("/selfsubjectrulesreviews")
                   for method, path, _, _ in api.calls)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("verb", ["get", "list", "create", "delete"])
def test_preflight_requires_every_service_rbac_operation(provider, verb):
    result, api = provider
    rule = next(rule for rule in api.rules if rule["resources"] == ["services"])
    rule["verbs"].remove(verb)
    with pytest.raises(ValueError, match="RBAC insufficient"):
        result.create(_intent(result))
    assert not any(method == "POST" and not path.endswith("/selfsubjectrulesreviews")
                   for method, path, _, _ in api.calls)


@pytest.mark.parametrize("failure", ["namespace", "pod-security", "runtime", "qualification", "default-deny", "rbac", "secret-list", "pod-exec"])
def test_preflight_fails_closed_before_resource_writes(provider, failure):
    result, api = provider
    if failure == "namespace":
        api.namespace["metadata"]["annotations"][k._KEYS["lumen_deployment"]] = "foreign"
    elif failure == "pod-security":
        api.namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "baseline"
    elif failure == "runtime":
        api.runtime["handler"] = "runc"
    elif failure == "qualification":
        api.runtime["metadata"]["uid"] = str(UUID(int=123))
    elif failure == "default-deny":
        api.objects[("networkpolicies", "lumen-default-deny")]["spec"]["egress"] = [{}]
    elif failure == "rbac":
        api.rules[0]["verbs"].remove("create")
    elif failure == "secret-list":
        api.rules[1]["verbs"].append("list")
    else:
        api.rules.append({"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]})
    with pytest.raises((ValueError, RuntimeError)):
        result.create(_intent(result))
    assert not any(method == "POST" and not path.endswith("/selfsubjectrulesreviews") for method, path, _, _ in api.calls)


def test_create_secret_policy_and_service_precede_nonroot_bounded_pod_and_no_cloud_credential(provider):
    result, api, intent, observation, ref = _create(provider)
    posts = [(path.split("/")[-1], body) for method, path, body, _ in api.calls
             if method == "POST" and not path.endswith("/selfsubjectrulesreviews")]
    assert [kind for kind, _ in posts] == ["secrets", "networkpolicies", "services", "pods"]
    pod = posts[-1][1]
    service = posts[2][1]
    assert service["metadata"]["name"] == pod["metadata"]["name"]
    assert service["metadata"]["labels"] == pod["metadata"]["labels"]
    assert service["metadata"]["annotations"] == posts[1][1]["metadata"]["annotations"]
    assert service["spec"] == {
        "type": "NodePort", "externalTrafficPolicy": "Local",
        "selector": posts[1][1]["spec"]["podSelector"]["matchLabels"],
        "ports": [{"protocol": "TCP", "port": 8013, "targetPort": 8013}],
    }
    uids = json.loads(pod["metadata"]["annotations"][k._AUX_UIDS])
    assert uids == {kind: api.objects[(kind, pod["metadata"]["name"])]["metadata"]["uid"]
                    for kind in ("secrets", "networkpolicies", "services")}
    spec, container = pod["spec"], pod["spec"]["containers"][0]
    assert spec["runtimeClassName"] == "sandbox" and spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    assert not any(spec[key] for key in ("hostNetwork", "hostPID", "hostIPC"))
    assert spec["securityContext"]["runAsUser"] == 4300
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["requests"] == container["resources"]["limits"]
    assert "pids" not in container["resources"]["limits"]
    assert sum(int(volume["emptyDir"]["sizeLimit"]) for volume in spec["volumes"] if "emptyDir" in volume) <= result.pool.sandbox.workspace_bytes
    assert len(observation.provider_id) <= 190
    assert observation.state == "booting"  # Running is never mTLS-ready.
    assert result.observe(ref).provider_id == observation.provider_id
    allocated = api.objects[("services", pod["metadata"]["name"])]["spec"]["ports"][0]["nodePort"]
    assert (observation.address, observation.port) == ("192.168.50.20", allocated)
    assert result.list_owned("mcp")[0] == observation
    assert all(len(value) <= 63 for value in pod["metadata"]["labels"].values())
    assert pod["metadata"]["annotations"][k._KEYS[OWNER_FINGERPRINT]] == intent.request_fingerprint
    assert "controller-only-token" not in json.dumps(posts)
    assert "one-use-bootstrap-token" not in json.dumps(pod)
    assert "one-use-bootstrap-token" not in json.dumps(posts[1][1])


def test_api_canonical_quantities_and_assigned_node_are_not_false_drift(provider):
    result, api = provider
    api.canonicalize_pod = True
    _, _, _, observation, ref = _create(provider)
    assert result.observe(ref).provider_id == observation.provider_id


@pytest.mark.parametrize("kind", ["secrets", "networkpolicies", "services", "pods"])
def test_unknown_post_is_single_attempt_without_blind_retry_or_cleanup(provider, kind):
    result, api = provider
    api.fail_post = kind
    with pytest.raises(httpx.ReadTimeout):
        result.create(_intent(result))
    assert len([1 for method, path, _, _ in api.calls if method == "POST" and path.endswith("/" + kind)]) == 1
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("field", ["uid", "fingerprint", "policy", "image", "privileged", "extra-container", "deadline", "aux-uid"])
def test_observe_and_delete_never_adopt_drift_or_same_name_replacement(provider, field):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    pod = api.objects[("pods", name)]
    if field == "uid":
        pod["metadata"]["uid"] = str(UUID(int=999))
    elif field in {"fingerprint", "policy"}:
        key = OWNER_FINGERPRINT if field == "fingerprint" else OWNER_POLICY
        pod["metadata"]["annotations"][k._KEYS[key]] = "9" * 64
        pod["metadata"]["labels"][k._KEYS[key]] = k._label("9" * 64)
    elif field == "image":
        pod["spec"]["containers"][0]["image"] = "registry.example/foreign@sha256:" + "9" * 64
    elif field == "privileged":
        pod["spec"]["containers"][0]["securityContext"]["privileged"] = True
    elif field == "extra-container":
        pod["spec"]["containers"].append(copy.deepcopy(pod["spec"]["containers"][0]))
    elif field == "deadline":
        pod["metadata"]["annotations"][k._DEADLINE] = "99999999"
    else:
        api.objects[("secrets", name)]["metadata"]["uid"] = str(UUID(int=999))
    for operation in (result.observe, result.delete):
        with pytest.raises(RuntimeError):
            operation(ref)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("kind", ["secrets", "networkpolicies", "services"])
def test_foreign_or_drifted_auxiliary_never_overwritten_or_deleted(provider, kind):
    result, api = provider
    intent = _intent(result)
    owner = ownership("lumen", intent)
    name = result._name(intent.resource_id, intent.generation)
    if kind == "secrets":
        item = result._secret(name, owner, "foreign-token")
    elif kind == "networkpolicies":
        item = result._policy(name, owner)
    else:
        item = result._service(name, owner)
    item["metadata"]["uid"] = str(UUID(int=999))
    item["metadata"]["annotations"][k._KEYS[OWNER_RESOURCE]] = "foreign"
    api.objects[(kind, name)] = item
    with pytest.raises(RuntimeError):
        result.create(intent)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)
    assert not any(method == "POST" and path.endswith("/" + kind) for method, path, _, _ in api.calls)


def test_delete_uses_uid_precondition_and_requires_positive_pod_and_aux_absence(provider):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    assert result.delete(ref).absent is False
    assert ("pods", name) not in api.objects
    assert result.delete(ref).absent is True
    assert all((kind, name) not in api.objects for kind in ("secrets", "networkpolicies", "services"))
    deletes = [body for method, _, body, _ in api.calls if method == "DELETE"]
    assert len(deletes) == 4
    assert all(set(body["preconditions"]) == {"uid"} for body in deletes)
    assert result.observe(ref).state == "absent"


def test_successful_delete_is_not_positive_absence_while_server_retains_pod(provider):
    result, api, _, _, ref = _create(provider)
    api.delay_delete = True
    assert result.delete(ref).absent is False
    assert result.observe(ref).state == "booting"


@pytest.mark.parametrize("kind", ["secrets", "networkpolicies", "services"])
def test_post_pod_absence_auxiliary_uid_replacement_is_preserved(provider, kind):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    del api.objects[("pods", name)]
    api.objects[(kind, name)]["metadata"]["uid"] = str(UUID(int=999))
    with pytest.raises(RuntimeError, match="UID mismatch"):
        result.delete(ref)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_explicit_orphan_cleanup_is_exact_and_does_not_list_secrets(provider):
    result, api = provider
    intent = _intent(result)
    api.fail_post = "pods"
    with pytest.raises(httpx.ReadTimeout):
        result.create(intent)
    # Test caller declares the failed create resolved. Provider never invokes this
    # automatically merely because a POST timed out.
    api.fail_post = None
    assert result.cleanup_orphans(intent).absent is True
    assert list(api.objects) == [("networkpolicies", "lumen-default-deny")]
    assert not any(method == "GET" and path.endswith("/secrets") for method, path, _, _ in api.calls)


def test_public_policy_excludes_private_linklocal_reserved_and_cluster_ranges(provider):
    result, _ = provider
    profile = result.profile.model_copy(update={"egress": "public", "dns_server_cidrs": ("192.168.50.53/32",)})
    result.profile = profile
    owner = ownership("lumen", _intent(result))
    policy = result._policy("example", owner)["spec"]
    public = policy["egress"][-1]
    assert public["ports"] == [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}]
    for address in ("127.0.0.1", "169.254.169.254", "10.42.0.5", "192.168.1.1", "224.0.0.1", "240.0.0.1", "2001:db8::1", "2606:4700:1234::1"):
        ip = ipaddress.ip_address(address)
        assert not any(ip.version == ipaddress.ip_network(peer["ipBlock"]["cidr"]).version
                       and ip in ipaddress.ip_network(peer["ipBlock"]["cidr"])
                       and not any(ip in ipaddress.ip_network(excluded) for excluded in peer["ipBlock"]["except"])
                       for peer in public["to"])


def test_redirect_and_body_limits_fail_closed_without_following_or_secret_errors(provider):
    result, _ = provider
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://foreign.example/token"}, json={"secret": "private"})
    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(redirect), follow_redirects=False)
    with pytest.raises(k.KubernetesAPIError, match="HTTP 302") as error:
        result._request("GET", "/api/v1/namespaces/runs")
    assert "private" not in str(error.value) and len(calls) == 1
    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (k._MAX_BODY + 1))))
    with pytest.raises(RuntimeError, match="exceeded bounds"):
        result._request("GET", "/api/v1/namespaces/runs")


def test_invalid_intent_never_reaches_api(provider):
    result, api = provider
    for intent in (replace(_intent(result), image_ref="sandbox:latest"),
                   replace(_intent(result), policy_digest="a" * 64),
                   replace(_intent(result), resource_id="../../foreign"),
                   replace(_intent(result), bootstrap_token=None)):
        with pytest.raises(ValueError):
            result.create(intent)
    assert api.calls == []


@pytest.mark.parametrize("authority", ["missing", "wrong", "durable-hash"])
def test_orphan_secret_cleanup_requires_original_token_or_durable_hash(provider, authority):
    result, api = provider
    intent = _intent(result)
    api.fail_post = "pods"
    with pytest.raises(httpx.ReadTimeout):
        result.create(intent)
    without_token = replace(intent, bootstrap_token=None)
    expected_hash = hashlib.sha256(intent.bootstrap_token.encode()).hexdigest()
    if authority == "durable-hash":
        assert result.cleanup_orphans(without_token, bootstrap_token_hash=expected_hash).absent is True
    else:
        with pytest.raises(RuntimeError, match="content authority unavailable|token hash mismatch"):
            result.cleanup_orphans(without_token, bootstrap_token_hash="0" * 64 if authority == "wrong" else None)
        assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_exact_preexisting_auxiliaries_can_be_compared_but_are_never_overwritten(provider):
    result, api = provider
    intent = _intent(result)
    owner = ownership("lumen", intent)
    name = result._name(intent.resource_id, intent.generation)
    for number, (kind, item) in enumerate((
        ("secrets", result._secret(name, owner, intent.bootstrap_token)),
        ("networkpolicies", result._policy(name, owner)),
        ("services", result._service(name, owner)),
    ), 100):
        item["metadata"]["uid"] = str(UUID(int=number))
        api.objects[(kind, name)] = item
    result.create(intent)
    writes = [path.split("/")[-1] for method, path, _, _ in api.calls
              if method == "POST" and not path.endswith("/selfsubjectrulesreviews")]
    assert writes == ["pods"]


def test_service_api_defaults_are_tolerated_without_false_drift(provider):
    result, api, _, observation, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    spec = api.objects[("services", name)]["spec"]
    spec.update({"clusterIPs": [spec["clusterIP"]], "ipFamilies": ["IPv4"],
                 "ipFamilyPolicy": "SingleStack", "internalTrafficPolicy": "Cluster",
                 "sessionAffinity": "None"})
    spec["ports"][0].pop("protocol")  # TCP is the API default.
    assert result.observe(ref) == observation
    assert result.list_owned("mcp") == [observation]
    assert result.delete(ref).absent is False
    assert result.delete(ref).absent is True


@pytest.mark.parametrize("changed", [
    {"type": "LoadBalancer"}, {"externalIPs": ["192.168.50.99"]},
    {"ports": [{"protocol": "TCP", "port": 8013, "targetPort": 8013, "nodePort": 30013},
               {"protocol": "TCP", "port": 8080, "targetPort": 8080, "nodePort": 30014}]},
    {"selector": {"app": "foreign"}}, {"externalTrafficPolicy": "Cluster"},
    {"loadBalancerIP": "192.168.50.99"}, {"loadBalancerSourceRanges": ["0.0.0.0/0"]},
    {"allocateLoadBalancerNodePorts": True}, {"healthCheckNodePort": 30100},
    {"sessionAffinity": "ClientIP"}, {"sessionAffinityConfig": {}},
    {"internalTrafficPolicy": "Local"}, {"publishNotReadyAddresses": True},
    {"unrequestedField": "untrusted"},
])
def test_service_specification_drift_refuses_observe_list_and_delete_without_mutation(provider, changed):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    api.objects[("services", name)]["spec"].update(changed)
    for operation in (lambda: result.observe(ref), lambda: result.list_owned("mcp"), lambda: result.delete(ref)):
        with pytest.raises(RuntimeError):
            operation()
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)
    assert all((kind, name) in api.objects for kind in ("pods", "secrets", "networkpolicies", "services"))


@pytest.mark.parametrize("missing", ["hostIP", "nodePort", "both"])
def test_observation_has_no_address_or_port_until_host_ip_and_node_port_exist(provider, missing):
    result, api, _, observation, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    pod = api.objects[("pods", name)]
    port = api.objects[("services", name)]["spec"]["ports"][0]
    node_port = port["nodePort"]
    if missing in {"hostIP", "both"}:
        pod["status"].pop("hostIP")
    if missing in {"nodePort", "both"}:
        port.pop("nodePort")
    pending = result.observe(ref)
    assert pending.provider_id == observation.provider_id
    assert (pending.address, pending.port) == (None, None)
    assert result.list_owned("mcp") == [pending]
    pod["status"]["hostIP"] = "192.168.50.20"
    port["nodePort"] = node_port
    assert result.observe(ref) == observation


@pytest.mark.parametrize("host_ip", ["192.168.50.21", "2606:4700:1234::20"])
def test_observation_uses_node_host_ip_and_allocated_port_not_pod_ip(provider, host_ip):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    api.objects[("pods", name)]["status"]["hostIP"] = host_ip
    api.objects[("services", name)]["spec"]["ports"][0]["nodePort"] = 32013
    observation = result.observe(ref)
    assert (observation.address, observation.port) == (host_ip, 32013)


@pytest.mark.parametrize("field,value", [("hostIP", "not-an-address"), ("hostIP", "192.168.50.20:8013"),
                                         ("nodePort", 0), ("nodePort", 65536), ("nodePort", True),
                                         ("nodePort", "32013"), ("nodePort", 32013.0)])
def test_observation_rejects_invalid_host_ip_or_allocated_node_port(provider, field, value):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    if field == "hostIP":
        api.objects[("pods", name)]["status"][field] = value
    else:
        api.objects[("services", name)]["spec"]["ports"][0][field] = value
    with pytest.raises(RuntimeError):
        result.observe(ref)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_service_uid_delete_follows_positive_pod_absence(provider):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    service_uid = api.objects[("services", name)]["metadata"]["uid"]
    api.calls.clear()
    api.delay_delete = True
    assert result.delete(ref).absent is False
    assert result.delete(ref).absent is False
    assert all(path.endswith("/pods/" + name) for method, path, _, _ in api.calls if method == "DELETE")
    assert ("services", name) in api.objects
    api.delay_delete = False
    assert result.delete(ref).absent is False
    assert ("pods", name) not in api.objects and ("services", name) in api.objects
    api.calls.clear()
    assert result.delete(ref).absent is True
    service_deletes = [(index, body) for index, (method, path, body, _) in enumerate(api.calls)
                       if method == "DELETE" and path.endswith("/services/" + name)]
    assert len(service_deletes) == 1
    index, body = service_deletes[0]
    assert body["preconditions"] == {"uid": service_uid}
    assert any(method == "GET" and path.endswith("/pods/" + name)
               for method, path, _, _ in api.calls[:index])
    assert any(method == "GET" and path.endswith("/services/" + name)
               for method, path, _, _ in api.calls[index + 1:])
    assert ("services", name) not in api.objects


def test_service_uid_cas_preserves_replacement_between_get_and_delete(provider):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    original_uid = api.objects[("services", name)]["metadata"]["uid"]
    replacement_uid = str(UUID(int=999))
    assert result.delete(ref).absent is False

    def replace_service(request):
        if request.method == "DELETE" and request.url.path.endswith("/services/" + name):
            api.objects[("services", name)]["metadata"]["uid"] = replacement_uid
        return api(request)

    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(replace_service),
                                 follow_redirects=False, trust_env=False)
    with pytest.raises(k.KubernetesAPIError, match="HTTP 409"):
        result.delete(ref)
    assert api.objects[("services", name)]["metadata"]["uid"] == replacement_uid
    deletes = [body for method, path, body, _ in api.calls
               if method == "DELETE" and path.endswith("/services/" + name)]
    assert len(deletes) == 1 and deletes[0]["preconditions"] == {"uid": original_uid}


def test_retained_service_delete_is_not_positive_auxiliary_absence(provider):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    assert result.delete(ref).absent is False
    api.delay_delete_kind = "services"
    with pytest.raises(RuntimeError, match="positive absence"):
        result.delete(ref)
    assert ("services", name) in api.objects
    api.delay_delete_kind = None
    assert result.delete(ref).absent is True
    assert ("services", name) not in api.objects


@pytest.mark.parametrize("drift", ["ownership", "selector", "type"])
def test_preflight_service_listing_rejects_foreign_or_drifted_services_without_writes(provider, drift):
    result, api = provider
    intent = _intent(result)
    owner = ownership("lumen", intent)
    name = result._name(intent.resource_id, intent.generation)
    service = result._service(name, owner)
    service["metadata"]["uid"] = str(UUID(int=999))
    if drift == "ownership":
        service["metadata"]["annotations"][k._KEYS[OWNER_RESOURCE]] = "foreign"
    elif drift == "selector":
        service["spec"]["selector"] = {"app": "foreign"}
    else:
        service["spec"]["type"] = "LoadBalancer"
    api.objects[("services", name)] = service
    with pytest.raises((ValueError, RuntimeError)):
        result.preflight()
    assert any(method == "GET" and path.endswith("/services") for method, path, _, _ in api.calls)
    assert not any(method == "DELETE" or method == "POST" and not path.endswith("/selfsubjectrulesreviews")
                   for method, path, _, _ in api.calls)


def test_orphan_cleanup_deletes_service_by_uid_only_after_pod_absence(provider):
    result, api = provider
    intent = _intent(result)
    api.fail_post = "pods"
    with pytest.raises(httpx.ReadTimeout):
        result.create(intent)
    name = result._name(intent.resource_id, intent.generation)
    uid = api.objects[("services", name)]["metadata"]["uid"]
    api.fail_post = None
    api.calls.clear()
    assert result.cleanup_orphans(intent).absent is True
    deletes = [(index, body) for index, (method, path, body, _) in enumerate(api.calls)
               if method == "DELETE" and path.endswith("/services/" + name)]
    assert len(deletes) == 1
    index, body = deletes[0]
    assert body["preconditions"] == {"uid": uid}
    assert any(method == "GET" and path.endswith("/pods/" + name)
               for method, path, _, _ in api.calls[:index])
    assert ("services", name) not in api.objects
    assert not any(method == "GET" and path.endswith("/secrets") for method, path, _, _ in api.calls)


def test_orphan_service_drift_refuses_cleanup_before_any_deletion(provider):
    result, api = provider
    intent = _intent(result)
    api.fail_post = "pods"
    with pytest.raises(httpx.ReadTimeout):
        result.create(intent)
    name = result._name(intent.resource_id, intent.generation)
    api.objects[("services", name)]["spec"]["externalIPs"] = ["192.168.50.99"]
    with pytest.raises(RuntimeError):
        result.cleanup_orphans(intent)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("dns_cidrs", [
    ("192.168.50.53/32",),
    ("192.168.50.53/32", "2606:4700:4700::1111/128", "192.168.50.54/32"),
])
def test_public_egress_uses_exact_dns_hosts_without_cluster_first_or_searches(provider, dns_cidrs):
    result, api = provider
    result.profile = KubernetesProfile.model_validate(result.profile.model_dump() | {
        "egress": "public", "dns_server_cidrs": dns_cidrs,
    })
    _, _, _, observation, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    spec = api.objects[("pods", name)]["spec"]
    assert spec["dnsPolicy"] == "None"
    assert spec["dnsConfig"] == {
        "nameservers": [str(ipaddress.ip_network(cidr).network_address) for cidr in dns_cidrs],
        "options": [{"name": "ndots", "value": "1"}],
    }
    dns = api.objects[("networkpolicies", name)]["spec"]["egress"][1]
    assert dns == {
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in dns_cidrs],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }
    assert result.observe(ref) == observation


def test_offline_egress_keeps_cluster_first_without_dns_egress(provider):
    result, api, _, _, ref = _create(provider)
    name = result._name(ref.resource_id, ref.generation)
    spec = api.objects[("pods", name)]["spec"]
    assert spec["dnsPolicy"] == "ClusterFirst"
    assert "dnsConfig" not in spec
    assert api.objects[("networkpolicies", name)]["spec"]["egress"] == [{
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in result.profile.bootstrap_egress_cidrs],
        "ports": [{"protocol": "TCP", "port": 8013}],
    }]


@pytest.mark.parametrize("dns_cidrs", [
    (), ("192.168.50.53/32", "192.168.50.54/32", "192.168.50.55/32", "192.168.50.56/32"),
    ("192.168.50.0/24",), ("2606:4700:4700::/64",),
])
def test_public_egress_requires_one_to_three_exact_dns_hosts(dns_cidrs):
    values = _pool()["profile"] | {"egress": "public", "dns_server_cidrs": dns_cidrs}
    with pytest.raises(ValidationError):
        KubernetesProfile.model_validate(values)


def test_public_egress_collapses_site_cluster_and_special_exclusions_per_ip_family(provider):
    result, _ = provider
    deny_cidrs = ("117.16.136.0/25", "117.16.136.128/25", "117.16.136.64/26", "10.42.0.0/24",
                 "2606:4700:abcd::/49", "2606:4700:abcd:8000::/49", "2606:4700:abcd::/64",
                 "2606:4700:1234::/64", "fc00::/7")
    result.profile = KubernetesProfile.model_validate(result.profile.model_dump() | {
        "egress": "public", "dns_server_cidrs": ("192.168.50.53/32",), "egress_deny_cidrs": deny_cidrs,
    })
    public = result._policy("example", ownership("lumen", _intent(result)))["spec"]["egress"][-1]
    assert public["ports"] == [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}]
    blocks = {ipaddress.ip_network(peer["ipBlock"]["cidr"]).version: peer["ipBlock"] for peer in public["to"]}
    assert set(blocks) == {4, 6}
    assert "117.16.136.0/24" in blocks[4]["except"]
    assert "2606:4700:abcd::/48" in blocks[6]["except"]
    for version, block in blocks.items():
        network = ipaddress.ip_network(block["cidr"])
        excluded = [ipaddress.ip_network(cidr) for cidr in block["except"]]
        assert all(value.version == version and value.subnet_of(network) for value in excluded)
        assert excluded == list(ipaddress.collapse_addresses(excluded))
        for cidr in (*deny_cidrs, *result.cluster.cluster_cidrs):
            denied = ipaddress.ip_network(cidr)
            if denied.version == version and denied.subnet_of(network):
                assert any(denied.subnet_of(value) for value in excluded)
    for address in ("117.16.136.1", "117.16.136.254", "10.42.0.5", "127.0.0.1", "169.254.169.254",
                    "2606:4700:abcd::1", "2606:4700:abcd:ffff::1", "2606:4700:1234::1", "2001:db8::1"):
        ip = ipaddress.ip_address(address)
        assert any(ip in ipaddress.ip_network(cidr) for cidr in blocks[ip.version]["except"])
    for address in ("8.8.8.8", "2606:4700:4700::1111"):
        ip = ipaddress.ip_address(address)
        assert ip in ipaddress.ip_network(blocks[ip.version]["cidr"])
        assert not any(ip in ipaddress.ip_network(cidr) for cidr in blocks[ip.version]["except"])


@pytest.mark.parametrize("deny_cidrs", [
    ("117.16.137.1/24",), ("2606:4700:abcd::1/48",), ("not-a-cidr",),
    ("117.16.137.0/24",) * 65, "117.16.137.0/24", (1,),
])
def test_kubernetes_egress_deny_cidrs_rejects_noncanonical_invalid_or_unbounded_config(deny_cidrs):
    values = _pool()["profile"] | {"egress_deny_cidrs": deny_cidrs}
    with pytest.raises(ValidationError):
        KubernetesProfile.model_validate_json(json.dumps(values), strict=True)


def test_kubernetes_egress_deny_cidrs_accepts_empty_and_at_most_64_strict_networks():
    baseline = KubernetesProfile.model_validate_json(json.dumps(_pool()["profile"]), strict=True)
    assert baseline.egress_deny_cidrs == ()
    deny_cidrs = tuple(f"117.16.{index}.0/24" for index in range(63)) + ("2606:4700:abcd::/48",)
    values = _pool()["profile"] | {"egress_deny_cidrs": deny_cidrs}
    profile = KubernetesProfile.model_validate_json(json.dumps(values), strict=True)
    assert profile.egress_deny_cidrs == deny_cidrs
