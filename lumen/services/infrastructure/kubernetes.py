"""One-shot, pinned-HTTPS Kubernetes sandboxes in an operator-owned namespace.

No kubeconfig loading, ambient credentials, shell commands, redirects or HTTP retries.
API object existence is not runtime/CNI qualification: preflight also requires a pinned
operator evidence report; the sandbox itself must measure its effective cgroup limits.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import ssl
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from lumen.services.infrastructure.config import KubernetesClusterProfile, KubernetesProfile, PoolConfig
from lumen.services.infrastructure.nova import validate_controller_material
from lumen.services.infrastructure.providers import (
    OWNER_DEPLOYMENT,
    OWNER_FINGERPRINT,
    OWNER_GENERATION,
    OWNER_POLICY,
    OWNER_POOL,
    OWNER_RESOURCE,
    OWNERSHIP_KEYS,
    DeleteResult,
    Observation,
    ResourceIntent,
    ResourceRef,
    owned,
    ownership,
)


def _quantity(value: str) -> Decimal:
    """Compare API-canonical quantities without accepting a wider resource bound."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([eE][+-]?[0-9]+|[numkKMGTP]i?|Ei?)?", str(value))
    if not match:
        raise RuntimeError("Kubernetes resource quantity invalid")
    number, suffix = match.groups()
    factors = {"n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
               "k": Decimal(1000), "K": Decimal(1000)}
    for power, unit in enumerate("MGTPE", 2):
        factors[unit] = Decimal(1000) ** power
    for power, unit in enumerate("KMGTPE", 1):
        factors[unit + "i"] = Decimal(1024) ** power
    try:
        return Decimal(number) * (factors[suffix] if suffix in factors else Decimal("1" + suffix) if suffix else 1)
    except (InvalidOperation, KeyError) as exc:
        raise RuntimeError("Kubernetes resource quantity invalid") from exc


def _pod_spec(spec: dict) -> dict:
    result = copy.deepcopy(spec)
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        result.setdefault(key, False)
    # Scheduling chooses the node after admission; all selectors/runtime
    # constraints remain exact. No workload-provided spec field is discarded.
    node = result.pop("nodeName", None)
    if node is not None and (not isinstance(node, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node)):
        raise RuntimeError("Kubernetes assigned node name invalid")
    for container in result.get("containers", []):
        for resources in container.get("resources", {}).values():
            for key, value in resources.items():
                resources[key] = _quantity(value)
    for volume in result.get("volumes", []):
        directory = volume.get("emptyDir", {})
        if "sizeLimit" in directory:
            directory["sizeLimit"] = _quantity(directory["sizeLimit"])
    for key, value in result.get("overhead", {}).items():
        result["overhead"][key] = _quantity(value)
    return result


def _service_spec(spec: dict) -> dict:
    """Discard only Service allocation fields and harmless API-server defaults."""
    result = copy.deepcopy(spec)
    cluster_ip = result.pop("clusterIP", None)
    cluster_ips = result.pop("clusterIPs", None)
    try:
        if cluster_ip is not None:
            ipaddress.ip_address(cluster_ip)
        if cluster_ips is not None:
            if not isinstance(cluster_ips, list) or not 1 <= len(cluster_ips) <= 2:
                raise ValueError("clusterIPs")
            for address in cluster_ips:
                ipaddress.ip_address(address)
            if cluster_ip is not None and cluster_ips[0] != cluster_ip:
                raise ValueError("clusterIP mismatch")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Kubernetes Service cluster address invalid") from exc
    families = result.pop("ipFamilies", None)
    if families is not None and (not isinstance(families, list) or not 1 <= len(families) <= 2
                                 or len(set(families)) != len(families)
                                 or any(value not in {"IPv4", "IPv6"} for value in families)):
        raise RuntimeError("Kubernetes Service IP families invalid")
    family_policy = result.pop("ipFamilyPolicy", None)
    if family_policy is not None and family_policy not in {"SingleStack", "PreferDualStack", "RequireDualStack"}:
        raise RuntimeError("Kubernetes Service IP family policy invalid")
    for key, default in (("internalTrafficPolicy", "Cluster"), ("sessionAffinity", "None")):
        if key in result and result.pop(key) != default:
            raise RuntimeError("Kubernetes immutable Service default drift")
    for port in result.get("ports", []):
        port.setdefault("protocol", "TCP")
        node_port = port.pop("nodePort", None)
        if node_port is not None and (type(node_port) is not int or not 1 <= node_port <= 65535):
            raise RuntimeError("Kubernetes Service node port invalid")
    return result

_PREFIX = "lumen.dev/"
_KEYS = {key: _PREFIX + key.removeprefix("lumen_").replace("_", "-") for key in OWNERSHIP_KEYS}
_DEADLINE = _PREFIX + "deadline-seconds"
_SECRET_HASH = _PREFIX + "bootstrap-sha256"
_AUX_UIDS = _PREFIX + "auxiliary-uids"
_HEX = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_UID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,127}")
_MAX_BODY = 2 * 1024 * 1024
# Deliberately excludes special-use address space, including globally routed cluster
# ranges supplied explicitly by the operator. IPv6 is limited to global unicast.
_PRIVATE_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)
_PRIVATE_V6 = ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _label(value: str) -> str:
    # A digest label is only a selector, never authority. Exact originals live in
    # annotations; this also handles 64-character hashes and underscore pool IDs.
    if len(value) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value):
        return value
    return "h-" + hashlib.sha256(value.encode()).hexdigest()[:60]


def _metadata(name: str, namespace: str, owner: dict[str, str], **annotations: str) -> dict:
    return {
        "name": name, "namespace": namespace,
        "labels": {_KEYS[key]: _label(value) for key, value in owner.items()},
        "annotations": {_KEYS[key]: value for key, value in owner.items()} | annotations,
    }


def _owner(obj: dict) -> dict[str, str]:
    metadata = obj.get("metadata", {})
    annotations, labels = metadata.get("annotations", {}), metadata.get("labels", {})
    result = {key: annotations.get(_KEYS[key], "") for key in OWNERSHIP_KEYS}
    if any(not isinstance(value, str) or not value or labels.get(_KEYS[key]) != _label(value)
           for key, value in result.items()):
        raise RuntimeError("Kubernetes ownership labels/annotations mismatch")
    if not _HEX.fullmatch(result[OWNER_FINGERPRINT]) or not _HEX.fullmatch(result[OWNER_POLICY]):
        raise RuntimeError("Kubernetes ownership fingerprint/policy invalid")
    return result


class KubernetesAPIError(RuntimeError):
    """Sanitized status only: neither API error bodies nor Secret contents escape."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Kubernetes API request failed (HTTP {status})")


class KubernetesProvider:
    requires_delivery = False

    def __init__(self, cluster: KubernetesClusterProfile, pool: PoolConfig, deployment_id: str,
                 *, controller_url: str, controller_ca_pem: str):
        if pool.backend != "kubernetes" or not isinstance(pool.profile, KubernetesProfile):
            raise ValueError("Kubernetes provider requires Kubernetes pool")
        if cluster.id != pool.profile.cluster_profile_id or not deployment_id:
            raise ValueError("Kubernetes cluster/pool identity mismatch")
        validate_controller_material(controller_url, controller_ca_pem)
        self.cluster, self.pool, self.profile = cluster, pool, pool.profile
        self.deployment_id = deployment_id
        self.controller_url, self.controller_ca_pem = controller_url, controller_ca_pem
        self._bootstrap_port = urlsplit(controller_url).port or 443
        secret = cluster.bearer_token
        if secret.env:
            token = os.environ[secret.env]
        else:
            with Path(secret.file).open("r", encoding="utf-8") as source:
                token = source.read(16385).strip()
        if not token or len(token) > 16384 or any(char.isspace() for char in token):
            raise ValueError("Kubernetes bearer token must be a bounded single token")
        context = ssl.create_default_context(cafile=cluster.ca_file)
        self.client = httpx.Client(
            base_url=cluster.server.rstrip("/"), verify=context,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(10, connect=5, pool=5), follow_redirects=False, trust_env=False,
            transport=httpx.HTTPTransport(verify=context, retries=0, trust_env=False,
                                          limits=httpx.Limits(max_connections=4, max_keepalive_connections=2)),
        )
        self._core = f"/api/v1/namespaces/{quote(cluster.namespace, safe='')}"
        self._network = f"/apis/networking.k8s.io/v1/namespaces/{quote(cluster.namespace, safe='')}"

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 params: dict | None = None, missing: bool = False) -> dict | None:
        started = time.monotonic()
        # Streaming places an actual bound on response bytes, not merely the
        # Content-Length supplied by a possibly faulty upstream. Each read is timed.
        with self.client.stream(method, path, json=body, params=params) as response:
            if response.status_code == 404 and missing:
                return None
            if not 200 <= response.status_code < 300:
                raise KubernetesAPIError(response.status_code)
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > _MAX_BODY or time.monotonic() - started > 20:
                    raise RuntimeError("Kubernetes API response exceeded bounds")
                data.extend(chunk)
            try:
                result = json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("Kubernetes API response is not JSON") from exc
            if not isinstance(result, dict):
                raise RuntimeError("Kubernetes API response is not an object")
            return result

    def _path(self, kind: str, name: str | None = None) -> str:
        root = self._network if kind == "networkpolicies" else self._core
        return f"{root}/{kind}" + (f"/{quote(name, safe='')}" if name else "")

    def _get(self, kind: str, name: str) -> dict | None:
        return self._request("GET", self._path(kind, name), missing=True)

    def _list(self, kind: str, selector: str | None = None) -> list[dict]:
        result, continuation, seen = [], "", set()
        for _ in range(64):
            params = {"limit": "200"}
            if selector:
                params["labelSelector"] = selector
            if continuation:
                params["continue"] = continuation
            page = self._request("GET", self._path(kind), params=params)
            items = page.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RuntimeError("Kubernetes list response invalid")
            result.extend(items)
            if len(result) > 12800:
                raise RuntimeError("Kubernetes list exceeded finite inventory bound")
            continuation = page.get("metadata", {}).get("continue", "")
            if not continuation:
                return result
            if not isinstance(continuation, str) or continuation in seen or len(continuation) > 8192:
                raise RuntimeError("Kubernetes list continuation invalid")
            seen.add(continuation)
        raise RuntimeError("Kubernetes list exceeded pagination bound")

    def _qualification(self, namespace: dict, runtime: dict) -> None:
        with Path(self.cluster.qualification_file).open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != self.cluster.qualification_sha256:
            raise ValueError("Kubernetes qualification digest mismatch")
        report = json.loads(raw)
        expected = {
            "version": 1, "server": self.cluster.server.rstrip("/"),
            "namespace": self.cluster.namespace, "namespace_uid": namespace["metadata"]["uid"],
            "runtime_class_name": self.cluster.runtime_class_name,
            "runtime_class_handler": self.cluster.runtime_class_handler,
            "runtime_class_uid": runtime["metadata"]["uid"],
            "runtime_class_spec_sha256": _digest({key: value for key, value in runtime.items()
                                                   if key not in {"metadata", "apiVersion", "kind"}}),
            "cluster_cidrs": list(self.cluster.cluster_cidrs), "pool_digest": self.pool.digest(),
        }
        if not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items()):
            raise ValueError("Kubernetes qualification does not match immutable cluster/profile")
        # The referenced, operator-approved reports must be real qualification
        # evidence, not boolean feature flags inferred from API object existence.
        for key in ("runtime_probe_sha256", "cni_probe_sha256"):
            if not isinstance(report.get(key), str) or not _HEX.fullmatch(report[key]):
                raise ValueError("Kubernetes qualification requires runtime and CNI evidence digests")
        maximum = report.get("measured_pids_max")
        if type(maximum) is not int or not 1 <= maximum <= self.pool.sandbox.pids_limit:
            raise ValueError("Kubernetes runtime has no qualified finite PID bound")

    def preflight(self, pool: PoolConfig | None = None) -> None:
        if pool is not None and pool != self.pool:
            raise ValueError("pool does not match Kubernetes provider")
        namespace = self._request("GET", f"/api/v1/namespaces/{quote(self.cluster.namespace, safe='')}")
        labels = namespace.get("metadata", {}).get("labels", {})
        annotations = namespace.get("metadata", {}).get("annotations", {})
        for key, value in ((OWNER_DEPLOYMENT, self.deployment_id), (OWNER_POOL, self.pool.name)):
            if labels.get(_KEYS[key]) != _label(value) or annotations.get(_KEYS[key]) != value:
                raise ValueError("Kubernetes namespace is not explicitly owned by deployment/pool")
        if labels.get("pod-security.kubernetes.io/enforce") != "restricted":
            raise ValueError("Kubernetes namespace requires Pod Security restricted enforcement")
        if namespace.get("metadata", {}).get("deletionTimestamp"):
            raise ValueError("Kubernetes namespace is terminating")
        runtime = self._request("GET", "/apis/node.k8s.io/v1/runtimeclasses/" + quote(self.cluster.runtime_class_name, safe=""))
        if runtime.get("handler") != self.cluster.runtime_class_handler:
            raise ValueError("Kubernetes RuntimeClass handler mismatch")
        if runtime.get("metadata", {}).get("deletionTimestamp"):
            raise ValueError("Kubernetes RuntimeClass is terminating")
        self._qualification(namespace, runtime)
        self._runtime = runtime
        with Path(self.profile.tool_manifest_file).open("rb") as source:
            manifest = source.read(1048577)
        if len(manifest) > 1048576 or hashlib.sha256(manifest).hexdigest() != self.profile.tool_manifest_sha256:
            raise ValueError("Kubernetes tool manifest pin mismatch")
        policies = self._list("networkpolicies")
        deny = [item for item in policies if item.get("metadata", {}).get("name") == "lumen-default-deny"]
        deny_spec = deny[0].get("spec", {}) if len(deny) == 1 else {}
        if (len(deny) != 1 or set(deny_spec) - {"podSelector", "policyTypes", "ingress", "egress"}
                or deny_spec.get("podSelector") != {} or set(deny_spec.get("policyTypes", [])) != {"Ingress", "Egress"}
                or deny_spec.get("ingress") or deny_spec.get("egress")):
            raise ValueError("Kubernetes namespace requires pre-provisioned default-deny policy")
        for item in policies:
            if item is deny[0]:
                continue
            owner = _owner(item)
            self._check_owner(owner)
            name = self._name(owner[OWNER_RESOURCE], int(owner[OWNER_GENERATION]))
            self._check_object(item, self._policy(name, owner), owner)
        for item in self._list("services"):
            owner = _owner(item)
            self._check_owner(owner)
            name = self._name(owner[OWNER_RESOURCE], int(owner[OWNER_GENERATION]))
            self._check_object(item, self._service(name, owner), owner)
        self._check_rbac()
        self._preflight_done = True

    def _check_rbac(self) -> None:
        # Authorization review is non-persisted; unlike a dry-run Pod create it
        # invokes no admission webhook and makes no workload/namespace writes.
        review = self._request("POST", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews", body={
            "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": self.cluster.namespace},
        })
        status = review.get("status", {})
        if status.get("incomplete") or status.get("evaluationError"):
            raise ValueError("Kubernetes RBAC review incomplete")
        rules = status.get("resourceRules", [])
        required = {
            ("", "pods"): {"get", "list", "create", "delete"},
            ("", "secrets"): {"get", "create", "delete"},
            ("", "services"): {"get", "list", "create", "delete"},
            ("networking.k8s.io", "networkpolicies"): {"get", "list", "create", "delete"},
        }
        for (group, resource), verbs in required.items():
            allowed = set()
            for rule in rules:
                if (group in rule.get("apiGroups", []) and resource in rule.get("resources", [])
                        and not rule.get("resourceNames")):
                    allowed.update(rule.get("verbs", []))
            if not verbs <= allowed:
                raise ValueError("Kubernetes namespace RBAC insufficient")
        # Refuse namespace-admin/wildcard credentials and Secret enumeration. This
        # review cannot discover grants in other namespaces; operator provisioning
        # and the pinned qualification remain responsible for cluster-wide scope.
        for rule in rules:
            resources, verbs = rule.get("resources", []), rule.get("verbs", [])
            if ("*" in rule.get("apiGroups", []) or "*" in resources or "*" in verbs
                    or ("secrets" in resources and {"list", "watch"} & set(verbs))):
                raise ValueError("Kubernetes credential exceeds least-privilege resource scope")
            for group in rule.get("apiGroups", []):
                for resource in resources:
                    permitted = required.get((group, resource), set())
                    if (group, resource) == ("", "namespaces") or (group, resource) == ("node.k8s.io", "runtimeclasses"):
                        permitted = {"get"}
                    elif (group, resource) in {
                        ("authorization.k8s.io", "selfsubjectaccessreviews"),
                        ("authorization.k8s.io", "selfsubjectrulesreviews"),
                        ("authentication.k8s.io", "selfsubjectreviews"),
                    }:
                        permitted = {"create"}
                    if not set(verbs) <= permitted:
                        raise ValueError("Kubernetes credential grants unrelated resource operations")

    @staticmethod
    def _name(resource_id: str, generation: int) -> str:
        name = f"lumen-{resource_id}-g{generation}"
        if not _NAME.fullmatch(name) or type(generation) is not int or generation < 1:
            raise ValueError("Kubernetes resource identity is not a safe deterministic Pod name")
        return name

    def _check_owner(self, owner: dict[str, str]) -> None:
        if (not owned(owner, self.deployment_id, self.pool.name)
                or owner[OWNER_POLICY] != self.pool.digest()):
            raise RuntimeError("Kubernetes full ownership/profile mismatch")
        self._name(owner[OWNER_RESOURCE], int(owner[OWNER_GENERATION]))

    def _provider_id(self, pod: dict, owner: dict[str, str]) -> str:
        metadata = pod["metadata"]
        annotations = metadata["annotations"]
        uids = json.loads(annotations[_AUX_UIDS])
        try:
            payload = b"".join(UUID(value).bytes for value in (
                metadata["uid"], uids["secrets"], uids["networkpolicies"], uids["services"]))
            payload += bytes.fromhex(owner[OWNER_FINGERPRINT]) + bytes.fromhex(annotations[_SECRET_HASH])
            payload += int(annotations[_DEADLINE]).to_bytes(4, "big")
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise RuntimeError("Kubernetes UID/admission identity invalid") from exc
        # 180 ASCII characters: fits the existing durable VARCHAR(190) while
        # binding all four UIDs, the full fingerprint and bootstrap content.
        return "k8s:" + base64.urlsafe_b64encode(payload).decode().rstrip("=")

    def _decode(self, ref: ResourceRef) -> tuple[str, str, dict[str, str], dict]:
        name = self._name(ref.resource_id, ref.generation)
        try:
            if ref.pool_id != self.pool.name or not ref.provider_id.startswith("k8s:"):
                raise ValueError("prefix/pool")
            encoded = ref.provider_id[4:]
            payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(payload) != 132:
                raise ValueError("length")
            uid, secret_uid, policy_uid, service_uid = (
                str(UUID(bytes=payload[start:start + 16])) for start in (0, 16, 32, 48))
            owner = {OWNER_DEPLOYMENT: self.deployment_id, OWNER_POOL: ref.pool_id,
                     OWNER_RESOURCE: ref.resource_id, OWNER_GENERATION: str(ref.generation),
                     OWNER_FINGERPRINT: payload[64:96].hex(), OWNER_POLICY: self.pool.digest()}
            admission = {"secrets": secret_uid, "networkpolicies": policy_uid, "services": service_uid,
                         "secret_hash": payload[96:128].hex(), "seconds": int.from_bytes(payload[128:], "big")}
            if not 1 <= admission["seconds"] <= self.pool.max_lifetime_seconds:
                raise ValueError("deadline")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Kubernetes provider identity mismatch") from exc
        return name, uid, owner, admission

    def _check_object(self, actual: dict, expected: dict, owner: dict[str, str], *, uid: str | None = None) -> None:
        metadata, desired = actual.get("metadata", {}), expected["metadata"]
        if (_owner(actual) != owner or metadata.get("name") != desired["name"]
                or metadata.get("namespace") != self.cluster.namespace
                or (uid is not None and metadata.get("uid") != uid)):
            raise RuntimeError("Kubernetes resource ownership/UID mismatch; refusing operation")
        if not isinstance(metadata.get("uid"), str) or not _UID.fullmatch(metadata["uid"]):
            raise RuntimeError("Kubernetes resource UID missing")
        if metadata.get("ownerReferences") or metadata.get("finalizers"):
            raise RuntimeError("Kubernetes resource has unexpected owner/finalizer")
        for key, value in desired.get("annotations", {}).items():
            if metadata.get("annotations", {}).get(key) != value:
                raise RuntimeError("Kubernetes immutable resource annotation drift")
        if actual.get("apiVersion") != expected["apiVersion"] or actual.get("kind") != expected["kind"]:
            raise RuntimeError("Kubernetes resource type mismatch")
        for key in set(actual) | set(expected):
            actual_value, expected_value = actual.get(key), expected.get(key)
            if key == "spec" and expected["kind"] == "Pod":
                actual_value, expected_value = _pod_spec(actual_value), _pod_spec(expected_value)
            elif key == "spec" and expected["kind"] == "Service":
                actual_value, expected_value = _service_spec(actual_value), _service_spec(expected_value)
            if key not in {"metadata", "status"} and actual_value != expected_value:
                raise RuntimeError("Kubernetes immutable resource specification drift")

    def _policy(self, name: str, owner: dict[str, str]) -> dict:
        def peers(cidrs):
            return [{"ipBlock": {"cidr": cidr}} for cidr in cidrs]

        egress = [{"to": peers(self.profile.bootstrap_egress_cidrs),
                   "ports": [{"protocol": "TCP", "port": self._bootstrap_port}]}]
        if self.profile.egress == "public":
            egress.append({"to": peers(self.profile.dns_server_cidrs),
                           "ports": [{"protocol": protocol, "port": 53} for protocol in ("UDP", "TCP")]})
            public = []
            for cidr, excluded in (("0.0.0.0/0", _PRIVATE_V4), ("2000::/3", _PRIVATE_V6)):
                network = ipaddress.ip_network(cidr)
                exclusions = (*excluded, *self.cluster.cluster_cidrs, *self.profile.egress_deny_cidrs)
                blocks = [ipaddress.ip_network(value) for value in exclusions
                          if ipaddress.ip_network(value).version == network.version
                          and ipaddress.ip_network(value).subnet_of(network)]
                # A wider operator exclusion leaves no public egress for this family.
                if any(network.subnet_of(ipaddress.ip_network(value)) for value in exclusions
                       if ipaddress.ip_network(value).version == network.version):
                    continue
                public.append({"ipBlock": {"cidr": cidr, "except": [str(value) for value in ipaddress.collapse_addresses(blocks)]}})
            if public:
                egress.append({"to": public, "ports": [{"protocol": "TCP", "port": port} for port in (80, 443)]})
        return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": _metadata(name, self.cluster.namespace, owner),
                "spec": {"podSelector": {"matchLabels": {_KEYS[key]: _label(value) for key, value in owner.items()}},
                         "policyTypes": ["Ingress", "Egress"],
                         "ingress": [{"from": peers(self.profile.controller_ingress_cidrs),
                                      "ports": [{"protocol": "TCP", "port": 8013}]}], "egress": egress}}

    def _service(self, name: str, owner: dict[str, str]) -> dict:
        return {"apiVersion": "v1", "kind": "Service",
                "metadata": _metadata(name, self.cluster.namespace, owner),
                "spec": {"type": "NodePort", "externalTrafficPolicy": "Local",
                         "selector": {_KEYS[key]: _label(value) for key, value in owner.items()},
                         "ports": [{"protocol": "TCP", "port": 8013, "targetPort": 8013}]}}

    def _secret(self, name: str, owner: dict[str, str], token: str) -> dict:
        return {"apiVersion": "v1", "kind": "Secret", "metadata": _metadata(name, self.cluster.namespace, owner),
                "type": "Opaque", "immutable": True,
                "data": {"token": base64.b64encode(token.encode()).decode(),
                         "ca.pem": base64.b64encode(self.controller_ca_pem.encode()).decode()}}

    def _pod(self, name: str, owner: dict[str, str], seconds: int, secret_hash: str, aux_uids: str) -> dict:
        policy = self.pool.sandbox
        resources = {"cpu": f"{policy.cpu_millis}m", "memory": str(policy.memory_bytes),
                     "ephemeral-storage": str(policy.workspace_bytes)}
        environment = {
            "SANDBOX_CONTROLLER_URL": self.controller_url,
            "SANDBOX_CONTROLLER_CA": "/run/lumen-bootstrap/ca.pem",
            "SANDBOX_BOOTSTRAP_FILE": "/run/lumen-bootstrap/token", "SANDBOX_STATE_DIR": "/state",
            "SANDBOX_WORKSPACE_DIR": "/work", "SANDBOX_TOOL_MANIFEST": self.profile.tool_manifest_path,
            "SANDBOX_TOOL_MANIFEST_SHA256": self.profile.tool_manifest_sha256,
            "SANDBOX_MEMORY_BYTES": str(policy.memory_bytes), "SANDBOX_WORKSPACE_BYTES": str(policy.workspace_bytes),
            "SANDBOX_CPU_MILLIS": str(policy.cpu_millis), "SANDBOX_PIDS_LIMIT": str(policy.pids_limit),
            "SANDBOX_PORT": "8013", "SANDBOX_IMAGE": self.pool.image, "SANDBOX_POLICY_DIGEST": self.pool.digest(),
        }
        pod = {"apiVersion": "v1", "kind": "Pod",
                "metadata": _metadata(name, self.cluster.namespace, owner, **{
                    _DEADLINE: str(seconds), _SECRET_HASH: secret_hash, _AUX_UIDS: aux_uids}),
                "spec": {
                    "runtimeClassName": self.cluster.runtime_class_name, "restartPolicy": "Never",
                    "activeDeadlineSeconds": seconds, "terminationGracePeriodSeconds": 5,
                    "automountServiceAccountToken": False, "serviceAccountName": "default", "serviceAccount": "default",
                    "enableServiceLinks": False, "hostNetwork": False, "hostPID": False, "hostIPC": False,
                    "nodeSelector": {"kubernetes.io/os": "linux", "kubernetes.io/arch": {"x86_64": "amd64", "aarch64": "arm64"}[self.pool.architecture]},
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 4300, "runAsGroup": 4300,
                                        "fsGroup": 4300, "fsGroupChangePolicy": "OnRootMismatch",
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
                    "priority": 0, "preemptionPolicy": "PreemptLowerPriority",
                    "tolerations": [{"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 0}
                                    for key in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")],
                    "containers": [{"name": "sandbox", "image": self.pool.image, "imagePullPolicy": "IfNotPresent",
                                    "command": ["lumen-sandbox-kubernetes"],
                                    "env": [{"name": key, "value": value} for key, value in environment.items()],
                                    "ports": [{"name": "mtls", "containerPort": 8013, "protocol": "TCP"}],
                                    "resources": {"requests": dict(resources), "limits": dict(resources)},
                                    "securityContext": {"runAsNonRoot": True, "runAsUser": 4300, "runAsGroup": 4300,
                                                        "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                                        "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
                                    "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
                                    "volumeMounts": [{"name": "bootstrap", "mountPath": "/run/lumen-bootstrap/token", "subPath": "token", "readOnly": True},
                                                     {"name": "bootstrap", "mountPath": "/run/lumen-bootstrap/ca.pem", "subPath": "ca.pem", "readOnly": True},
                                                     {"name": "state", "mountPath": "/state"}, {"name": "work", "mountPath": "/work"}]}],
                    "volumes": [{"name": "bootstrap", "secret": {"secretName": name, "defaultMode": 0o440,
                                                                "items": [{"key": "token", "path": "token"}, {"key": "ca.pem", "path": "ca.pem"}]}},
                                {"name": "state", "emptyDir": {"medium": "Memory", "sizeLimit": str(policy.workspace_bytes // 4)}},
                                {"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": str(policy.workspace_bytes - policy.workspace_bytes // 4)}}],
                }}
        if self.profile.egress == "public":
            pod["spec"]["dnsPolicy"] = "None"
            pod["spec"]["dnsConfig"] = {
                "nameservers": [str(ipaddress.ip_network(value).network_address) for value in self.profile.dns_server_cidrs],
                "options": [{"name": "ndots", "value": "1"}],
            }
        runtime = getattr(self, "_runtime", {})
        if runtime.get("overhead", {}).get("podFixed"):
            overhead = runtime["overhead"]["podFixed"]
            if set(overhead) - {"cpu", "memory"} or any(_quantity(value) <= 0 for value in overhead.values()):
                raise ValueError("Kubernetes RuntimeClass overhead must have finite positive CPU/memory")
            pod["spec"]["overhead"] = dict(overhead)
        scheduling = runtime.get("scheduling", {})
        for key, value in scheduling.get("nodeSelector", {}).items():
            current = pod["spec"]["nodeSelector"].get(key)
            if current is not None and current != value:
                raise ValueError("Kubernetes RuntimeClass node selector conflicts with pool")
            pod["spec"]["nodeSelector"][key] = value
        for toleration in scheduling.get("tolerations", []):
            if toleration not in pod["spec"]["tolerations"]:
                pod["spec"]["tolerations"].append(copy.deepcopy(toleration))
        return pod

    def _expected_pod(self, pod: dict, owner: dict[str, str]) -> dict:
        annotations = pod.get("metadata", {}).get("annotations", {})
        try:
            seconds = int(annotations[_DEADLINE])
            secret_hash, aux_uids = annotations[_SECRET_HASH], annotations[_AUX_UIDS]
            uids = json.loads(aux_uids)
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError("Kubernetes immutable Pod admission annotations missing") from exc
        if (not 1 <= seconds <= self.pool.max_lifetime_seconds or not _HEX.fullmatch(secret_hash)
                or not isinstance(uids, dict) or set(uids) != {"secrets", "networkpolicies", "services"}
                or any(not isinstance(value, str) or not _UID.fullmatch(value) for value in uids.values())):
            raise RuntimeError("Kubernetes immutable Pod admission bounds invalid")
        return self._pod(self._name(owner[OWNER_RESOURCE], int(owner[OWNER_GENERATION])), owner, seconds, secret_hash, aux_uids)

    def _check_pod(self, pod: dict, owner: dict[str, str], uid: str | None = None) -> None:
        self._check_owner(owner)
        self._check_object(pod, self._expected_pod(pod, owner), owner, uid=uid)

    def _observation(self, pod: dict, owner: dict[str, str], service: dict) -> Observation:
        state = pod.get("status", {}).get("phase", "Pending")
        state = {"Pending": "booting", "Running": "booting", "Succeeded": "stopped", "Failed": "error"}.get(state, "unknown")
        address = pod.get("status", {}).get("hostIP")
        if address is not None:
            try:
                if not isinstance(address, str):
                    raise ValueError("hostIP")
                address = str(ipaddress.ip_address(address))
            except ValueError as exc:
                raise RuntimeError("Kubernetes Pod host address invalid") from exc
        port = service["spec"]["ports"][0].get("nodePort")
        if not address or port is None:
            address, port = None, None
        if pod.get("metadata", {}).get("deletionTimestamp"):
            state = "deleting"
        return Observation(self._provider_id(pod, owner), state, address, port, owner)

    def _ensure_aux(self, kind: str, desired: dict, owner: dict[str, str]) -> dict:
        name = desired["metadata"]["name"]
        current = self._get(kind, name)
        if current is None:
            # Do not retry unknown POST, including an apparently harmless Secret
            # POST. Recovery must observe exact content/UID before proceeding.
            current = self._request("POST", self._path(kind), body=desired)
        self._check_object(current, desired, owner)
        if current.get("metadata", {}).get("deletionTimestamp"):
            raise RuntimeError("Kubernetes auxiliary resource is terminating")
        return current

    def create(self, intent: ResourceIntent) -> Observation:
        if (intent.pool_id != self.pool.name or intent.role != "sandbox" or intent.image_ref != self.pool.image
                or intent.policy_digest != self.pool.digest() or not _HEX.fullmatch(intent.request_fingerprint)
                or intent.guest_profile_id is not None or intent.guest_profile_digest is not None):
            raise ValueError("Kubernetes intent does not match immutable pool")
        if not intent.bootstrap_token or len(intent.bootstrap_token.encode()) > 16384:
            raise ValueError("Kubernetes creation requires bounded one-use bootstrap token")
        name = self._name(intent.resource_id, intent.generation)
        self.preflight()
        if self._get("pods", name) is not None:
            raise RuntimeError("Kubernetes Pod name already exists; recover via observation, never retry create")
        owner = ownership(self.deployment_id, intent)
        secret = self._secret(name, owner, intent.bootstrap_token)
        created_secret = self._ensure_aux("secrets", secret, owner)
        created_policy = self._ensure_aux("networkpolicies", self._policy(name, owner), owner)
        created_service = self._ensure_aux("services", self._service(name, owner), owner)
        uids = _canonical({"secrets": created_secret["metadata"]["uid"],
                           "networkpolicies": created_policy["metadata"]["uid"],
                           "services": created_service["metadata"]["uid"]}).decode()
        desired = self._pod(name, owner, self.pool.max_lifetime_seconds, _digest(secret["data"]), uids)
        pod = self._request("POST", self._path("pods"), body=desired)
        self._check_object(pod, desired, owner)
        return self._observation(pod, owner, created_service)

    def observe(self, ref: ResourceRef) -> Observation:
        name, uid, owner, _ = self._decode(ref)
        pod = self._get("pods", name)
        if pod is None:
            return Observation(ref.provider_id, "absent", None, None, {})
        self._check_pod(pod, owner, uid)
        if self._provider_id(pod, owner) != ref.provider_id:
            raise RuntimeError("Kubernetes immutable admission identity drift")
        service = self._check_auxiliaries(pod, owner)
        return self._observation(pod, owner, service)

    def list_owned(self, pool_id: str) -> list[Observation]:
        if pool_id != self.pool.name:
            raise ValueError("pool does not match Kubernetes provider")
        selector = ",".join(f"{_KEYS[key]}={_label(value)}" for key, value in (
            (OWNER_DEPLOYMENT, self.deployment_id), (OWNER_POOL, pool_id)))
        result = []
        for pod in self._list("pods", selector):
            owner = _owner(pod)
            self._check_pod(pod, owner)
            service = self._check_auxiliaries(pod, owner)
            result.append(self._observation(pod, owner, service))
        return result

    def _check_auxiliaries(self, pod: dict, owner: dict[str, str]) -> dict:
        annotations = pod["metadata"]["annotations"]
        uids = json.loads(annotations[_AUX_UIDS])
        name = pod["metadata"]["name"]
        secret, policy = self._get("secrets", name), self._get("networkpolicies", name)
        service = self._get("services", name)
        if secret is None or policy is None or service is None:
            raise RuntimeError("Kubernetes owned auxiliary resource missing")
        # Content comparison on a normal Secret GET, never a broad Secrets list.
        data = secret.get("data", {})
        if (set(data) != {"token", "ca.pem"} or _digest(data) != annotations[_SECRET_HASH]
                or data.get("ca.pem") != base64.b64encode(self.controller_ca_pem.encode()).decode()):
            raise RuntimeError("Kubernetes bootstrap Secret drift")
        desired = {"apiVersion": "v1", "kind": "Secret", "metadata": _metadata(name, self.cluster.namespace, owner),
                   "type": "Opaque", "immutable": True, "data": data}
        self._check_object(secret, desired, owner, uid=uids["secrets"])
        self._check_object(policy, self._policy(name, owner), owner, uid=uids["networkpolicies"])
        self._check_object(service, self._service(name, owner), owner, uid=uids["services"])
        return service

    def _delete_exact(self, kind: str, current: dict) -> None:
        uid = current["metadata"]["uid"]
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise RuntimeError("Kubernetes deletion requires exact UID")
        self._request("DELETE", self._path(kind, current["metadata"]["name"]), body={
            "apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid},
            "propagationPolicy": "Background", "gracePeriodSeconds": 5,
        }, missing=True)

    def delete(self, ref: ResourceRef) -> DeleteResult:
        name, uid, owner, admission = self._decode(ref)
        pod = self._get("pods", name)
        if pod is not None:
            self._check_pod(pod, owner, uid)
            if self._provider_id(pod, owner) != ref.provider_id:
                raise RuntimeError("Kubernetes immutable admission identity drift")
            # Validate every auxiliary before the first mutation; drift must never
            # cause deletion of even the otherwise correctly owned Pod.
            self._check_auxiliaries(pod, owner)
            self._delete_exact("pods", pod)
            # A successful DELETE is not absence (finalizers/grace/transport races).
            return DeleteResult(absent=False)
        # The Pod is positively absent. UID-bound auxiliary cleanup is still needed
        # before releasing the durable resource/quota record.
        self._cleanup(name, owner, expected_uids=admission, secret_hash=admission["secret_hash"])
        return DeleteResult(absent=self._get("pods", name) is None)

    def _cleanup(self, name: str, owner: dict[str, str], token: str | None = None, *,
                 expected_uids: dict | None = None, secret_hash: str | None = None,
                 bootstrap_token_hash: str | None = None) -> None:
        candidates = []
        for kind in ("secrets", "networkpolicies", "services"):
            current = self._get(kind, name)
            if current is None:
                continue
            if kind == "networkpolicies":
                expected = self._policy(name, owner)
            elif kind == "services":
                expected = self._service(name, owner)
            else:
                data = current.get("data", {})
                if token is None and secret_hash is None and bootstrap_token_hash is None:
                    raise RuntimeError("Kubernetes orphan Secret content authority unavailable")
                if bootstrap_token_hash is not None:
                    try:
                        encoded = data.get("token", "")
                        if not isinstance(encoded, str) or not 1 <= len(encoded) <= 21848:
                            raise ValueError("length")
                        decoded = base64.b64decode(encoded, validate=True)
                        if not decoded or len(decoded) > 16384 or hashlib.sha256(decoded).hexdigest() != bootstrap_token_hash:
                            raise ValueError("hash")
                    except (ValueError, TypeError) as exc:
                        raise RuntimeError("Kubernetes orphan bootstrap token hash mismatch") from exc
                if (set(data) != {"token", "ca.pem"} or data.get("ca.pem") != base64.b64encode(self.controller_ca_pem.encode()).decode()
                        or (token is not None and data.get("token") != base64.b64encode(token.encode()).decode())
                        or (secret_hash is not None and _digest(data) != secret_hash)):
                    raise RuntimeError("Kubernetes orphan Secret drift")
                expected = {"apiVersion": "v1", "kind": "Secret", "metadata": _metadata(name, self.cluster.namespace, owner),
                            "type": "Opaque", "immutable": True, "data": data}
            self._check_object(current, expected, owner, uid=expected_uids[kind] if expected_uids else None)
            candidates.append((kind, current))
        for kind, current in candidates:
            if self._get("pods", name) is not None:
                raise RuntimeError("Kubernetes Pod appeared during auxiliary cleanup")
            self._delete_exact(kind, current)
        for kind, _ in candidates:
            if self._get(kind, name) is not None:
                raise RuntimeError("Kubernetes auxiliary deletion has not reached positive absence")

    def cleanup_orphans(self, intent: ResourceIntent, *, bootstrap_token_hash: str | None = None) -> DeleteResult:
        """After durable unknown-create resolution only; never a POST timeout handler.

        A 404 cannot prove that a previously timed-out server-side POST cannot
        commit later. The caller must establish there is no in-flight create first.
        Exact-name GETs avoid broad Secret enumeration or label-based deletion.
        """
        if bootstrap_token_hash is not None and not _HEX.fullmatch(bootstrap_token_hash):
            raise ValueError("Kubernetes orphan bootstrap hash invalid")
        owner = ownership(self.deployment_id, intent)
        self._check_owner(owner)
        name = self._name(intent.resource_id, intent.generation)
        if self._get("pods", name) is not None:
            raise RuntimeError("Kubernetes Pod exists; orphan cleanup refused")
        self._cleanup(name, owner, intent.bootstrap_token, bootstrap_token_hash=bootstrap_token_hash)
        return DeleteResult(absent=self._get("pods", name) is None)

    def deliver_bootstrap(self, ref: ResourceRef, token: str) -> bool:
        # Bootstrap is immutable and already delivered before Pod admission. A
        # separate delivery retry must never replace or expose the one-use token.
        self._decode(ref)
        return True
