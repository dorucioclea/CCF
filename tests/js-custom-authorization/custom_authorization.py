# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
import http
import infra.network
import infra.path
import infra.proc
import infra.net
import infra.e2e_args
import suite.test_requirements as reqs
from infra.runner import ConcurrentRunner
import os
import tempfile
import base64
import json
import time
import infra.jwt_issuer
import datetime
import re
from http import HTTPStatus

from loguru import logger as LOG


@reqs.description("Test custom authorization")
def test_custom_auth(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        r = c.get("/app/custom_auth", headers={"Authorization": "Bearer 42"})
        assert r.status_code == http.HTTPStatus.OK, r.status_code
        assert r.body.json()

    return network


def run(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network = test_custom_auth(network, args)


@reqs.description("Test stack size limit")
def test_stack_size_limit(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        r = c.post("/app/recursive", body={"depth": 50})
        assert r.status_code == http.HTTPStatus.OK, r.status_code

    with primary.client("user0") as c:
        r = c.post("/app/recursive", body={"depth": 2000})
        assert r.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR, r.status_code

    return network


@reqs.description("Test heap size limit")
def test_heap_size_limit(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        r = c.post("/app/alloc", body={"size": 5 * 1024 * 1024})
        assert r.status_code == http.HTTPStatus.OK, r.status_code

    with primary.client("user0") as c:
        r = c.post("/app/alloc", body={"size": 500 * 1024 * 1024})
        assert r.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR, r.status_code

    return network


def run_limits(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network = test_stack_size_limit(network, args)
        network = test_heap_size_limit(network, args)


@reqs.description("Cert authentication")
def test_cert_auth(network, args):
    def create_keypair(local_id, valid_from, validity_days):
        privk_pem, _ = infra.crypto.generate_ec_keypair()
        with open(
            os.path.join(network.common_dir, f"{local_id}_privk.pem"),
            "w",
            encoding="ascii",
        ) as f:
            f.write(privk_pem)

        cert = infra.crypto.generate_cert(
            privk_pem,
            valid_from=valid_from,
            validity_days=validity_days,
        )
        with open(
            os.path.join(network.common_dir, f"{local_id}_cert.pem"),
            "w",
            encoding="ascii",
        ) as f:
            f.write(cert)

    primary, _ = network.find_primary()

    LOG.info("User with old cert cannot call user-authenticated endpoint")
    local_user_id = "in_the_past"
    create_keypair(
        local_user_id, datetime.datetime.utcnow() - datetime.timedelta(days=50), 3
    )
    network.consortium.add_user(primary, local_user_id)

    with primary.client(local_user_id) as c:
        r = c.get("/app/cert")
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r
        assert "Not After" in r.body.json()["error"]["details"][0]["message"], r

    LOG.info("User with future cert cannot call user-authenticated endpoint")
    local_user_id = "in_the_future"
    create_keypair(
        local_user_id, datetime.datetime.utcnow() + datetime.timedelta(days=50), 3
    )
    network.consortium.add_user(primary, local_user_id)

    with primary.client(local_user_id) as c:
        r = c.get("/app/cert")
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r
        assert "Not Before" in r.body.json()["error"]["details"][0]["message"], r

    LOG.info("No leeway added to cert time evaluation")
    local_user_id = "just_expired"
    valid_from = datetime.datetime.utcnow() - datetime.timedelta(days=1, seconds=2)
    create_keypair(local_user_id, valid_from, 1)
    network.consortium.add_user(primary, local_user_id)

    with primary.client(local_user_id) as c:
        r = c.get("/app/cert")
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r
        assert "Not After" in r.body.json()["error"]["details"][0]["message"], r

    return network


@reqs.description("JWT authentication")
def test_jwt_auth(network, args):
    primary, _ = network.find_nodes()

    issuer = infra.jwt_issuer.JwtIssuer("https://example.issuer")

    jwt_kid = "my_key_id"

    LOG.info("Add JWT issuer with initial keys")
    with tempfile.NamedTemporaryFile(prefix="ccf", mode="w+") as metadata_fp:
        jwt_cert_der = infra.crypto.cert_pem_to_der(issuer.cert_pem)
        der_b64 = base64.b64encode(jwt_cert_der).decode("ascii")
        data = {
            "issuer": issuer.name,
            "jwks": {"keys": [{"kty": "RSA", "kid": jwt_kid, "x5c": [der_b64]}]},
        }
        json.dump(data, metadata_fp)
        metadata_fp.flush()
        network.consortium.set_jwt_issuer(primary, metadata_fp.name)

    LOG.info("Calling jwt endpoint after storing keys")
    with primary.client("user0") as c:
        r = c.get("/app/jwt", headers=infra.jwt_issuer.make_bearer_header("garbage"))
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r.status_code

        jwt_mismatching_key_priv_pem, _ = infra.crypto.generate_rsa_keypair(2048)
        jwt = infra.crypto.create_jwt({}, jwt_mismatching_key_priv_pem, jwt_kid)
        r = c.get("/app/jwt", headers=infra.jwt_issuer.make_bearer_header(jwt))
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r.status_code

        r = c.get(
            "/app/jwt",
            headers=infra.jwt_issuer.make_bearer_header(issuer.issue_jwt(jwt_kid)),
        )
        assert r.status_code == HTTPStatus.OK, r.status_code

        LOG.info("Calling JWT with too-late nbf")
        r = c.get(
            "/app/jwt",
            headers=infra.jwt_issuer.make_bearer_header(
                issuer.issue_jwt(jwt_kid, claims={"nbf": time.time() + 60})
            ),
        )
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r.status_code

        LOG.info("Calling JWT with too-early exp")
        r = c.get(
            "/app/jwt",
            headers=infra.jwt_issuer.make_bearer_header(
                issuer.issue_jwt(jwt_kid, claims={"exp": time.time() - 60})
            ),
        )
        assert r.status_code == HTTPStatus.UNAUTHORIZED, r.status_code

    return network


@reqs.description("Role-based access")
def test_role_based_access(network, args):
    primary, _ = network.find_nodes()

    users = network.users
    assert len(users) >= 3

    # Confirm that initially, no user can read or write from the secret, or modify roles
    for user in users:
        with primary.client(user.local_id) as c:
            r = c.get("/app/secret")
            assert r.status_code == 401, r.status_code

            r = c.post("/app/secret", {"new_secret": "I'm in charge"})
            assert r.status_code == 401, r.status_code

            r = c.post(
                "/app/roles",
                {"target_id": user.service_id, "target_role": "secret_reader"},
            )
            assert r.status_code == 401, r.status_code

    # Bootstrap with a member-governance operation to give first user special role, allowing them to set other users' roles
    network.consortium.set_user_data(
        primary, users[0].service_id, {"role": "role_master"}
    )

    readers = []
    writers = []

    # First user can now assign roles of itself and others
    with primary.client(users[0].local_id) as c:
        r = c.post(
            "/app/roles",
            {"target_id": users[0].service_id, "target_role": "secret_reader"},
        )
        assert r.status_code == 204, r.status_code
        readers.append(users[0])

        r = c.post(
            "/app/roles",
            {"target_id": users[1].service_id, "target_role": "role_master"},
        )
        assert r.status_code == 204, r.status_code

    # Second user can now assign roles, thanks to assignment from first user
    with primary.client(users[1].local_id) as c:
        r = c.post(
            "/app/roles",
            {"target_id": users[2].service_id, "target_role": "secret_reader"},
        )
        assert r.status_code == 204, r.status_code
        readers.append(users[2])

        r = c.post(
            "/app/roles",
            {"target_id": users[2].service_id, "target_role": "secret_writer"},
        )
        assert r.status_code == 204, r.status_code
        writers.append(users[2])

        r = c.post(
            "/app/roles",
            {"target_id": users[3].service_id, "target_role": "secret_reader"},
        )
        assert r.status_code == 204, r.status_code
        readers.append(users[3])

    # Those role assignments allow some users to read and write
    for user in users:
        with primary.client(user.local_id) as c:
            r = c.post(
                "/app/secret", {"new_secret": f"My favourite person is {user.local_id}"}
            )
            if user in writers:
                assert r.status_code == 204, r.status_code
            else:
                assert r.status_code == 401, r.status_code

            r = c.get("/app/secret")
            if user in readers:
                assert r.status_code == 200, r.status_code
            else:
                assert r.status_code == 401, r.status_code


def run_authn(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network = test_cert_auth(network, args)
        network = test_jwt_auth(network, args)
        network = test_role_based_access(network, args)


@reqs.description("Test content types")
def test_content_types(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        r = c.post("/app/text", body="text")
        assert r.status_code == http.HTTPStatus.OK, r.status_code
        assert r.headers["content-type"] == "text/plain"
        assert r.body.text() == "text"

        r = c.post("/app/json", body={"foo": "bar"})
        assert r.status_code == http.HTTPStatus.OK, r.status_code
        assert r.headers["content-type"] == "application/json"
        assert r.body.json() == {"foo": "bar"}

        r = c.post("/app/binary", body=b"\x00" * 42)
        assert r.status_code == http.HTTPStatus.OK, r.status_code
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.body.data() == b"\x00" * 42, r.body

        r = c.post("/app/custom", body="text", headers={"content-type": "foo/bar"})
        assert r.status_code == http.HTTPStatus.OK, r.status_code
        assert r.headers["content-type"] == "text/plain"
        assert r.body.text() == "text"

    return network


@reqs.description("Test accept header")
def test_accept_header(network, args):
    primary, _ = network.find_nodes()

    with primary.client() as c:
        r = c.get("/node/commit", headers={"accept": "nonsense"})
        assert r.status_code == http.HTTPStatus.BAD_REQUEST.value

        r = c.get("/node/commit", headers={"accept": "text/html"})
        assert r.status_code == http.HTTPStatus.NOT_ACCEPTABLE.value

        r = c.get(
            "/node/commit",
            headers={"accept": "text/html;q=0.9,image/jpeg;video/mpeg;q=0.8"},
        )
        assert r.status_code == http.HTTPStatus.NOT_ACCEPTABLE.value

        r = c.get("/node/commit", headers={"accept": "*/*"})
        assert r.status_code == http.HTTPStatus.OK.value

        r = c.get("/node/commit", headers={"accept": "application/*"})
        assert r.status_code == http.HTTPStatus.OK.value

        r = c.get("/node/commit", headers={"accept": "application/json"})
        assert r.status_code == http.HTTPStatus.OK.value
        assert r.headers["content-type"] == "application/json"

        r = c.get("/node/commit", headers={"accept": "application/msgpack"})
        assert r.status_code == http.HTTPStatus.OK.value
        assert r.headers["content-type"] == "application/msgpack"

        r = c.get(
            "/node/commit",
            headers={"accept": "text/html;q=0.9,image/jpeg;video/mpeg;q=0.8,*/*;q=0.1"},
        )
        assert r.status_code == http.HTTPStatus.OK.value

        r = c.get(
            "/node/commit",
            headers={
                "accept": "text/html;q=0.9,image/jpeg;video/mpeg;q=0.8,application/json;q=0.1"
            },
        )
        assert r.status_code == http.HTTPStatus.OK.value
        assert r.headers["content-type"] == "application/json"

        r = c.get(
            "/node/commit",
            headers={
                "accept": "text/html;q=0.9,image/jpeg;video/mpeg;q=0.8,application/msgpack;q=0.1"
            },
        )
        assert r.status_code == http.HTTPStatus.OK.value
        assert r.headers["content-type"] == "application/msgpack"

    return network


@reqs.description("Test supported methods")
def test_supported_methods(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        # Test ALLOW header when wrong method is used
        r = c.delete("/app/text")
        assert r.status_code == http.HTTPStatus.METHOD_NOT_ALLOWED
        allow = r.headers.get("allow")
        assert allow is not None
        assert "OPTIONS" in allow
        assert "POST" in allow

        # Test ALLOW header when OPTIONS method is used on POST-only app endpoint
        r = c.options("/app/text")
        assert r.status_code == http.HTTPStatus.NO_CONTENT
        allow = r.headers.get("allow")
        assert allow is not None
        assert "OPTIONS" in allow
        assert "POST" in allow

        # Test ALLOW header when OPTIONS method is used on GET-only framework endpoint
        r = c.options("/node/commit")
        assert r.status_code == http.HTTPStatus.NO_CONTENT
        allow = r.headers.get("allow")
        assert allow is not None
        assert "OPTIONS" in allow
        assert "GET" in allow

    return network


@reqs.description("Test unknown path")
def test_unknown_path(network, args):
    primary, _ = network.find_nodes()

    with primary.client("user0") as c:
        r = c.get("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code
        r = c.post("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code
        r = c.delete("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code

        r = c.post("/app/unknown")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code

    with primary.client() as c:
        r = c.get("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code
        r = c.post("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code
        r = c.delete("/app/not/a/real/path")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code

        r = c.post("/app/unknown")
        assert r.status_code == http.HTTPStatus.NOT_FOUND, r.status_code

    return network


def run_content_types(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network = test_content_types(network, args)
        network = test_accept_header(network, args)
        network = test_supported_methods(network, args)
        network = test_unknown_path(network, args)


@reqs.description("Test request object API")
def test_random_api(args):
    # Test that Math.random() does not repeat values:
    # - on multiple invocations within a single request
    # - on multiple requests
    # - on network restarts

    seen_values = set()

    def assert_fresh(n):
        assert n not in seen_values, f"{n} has already been returned"
        seen_values.add(n)

    n_repeats = 3
    for _ in range(n_repeats):
        with infra.network.network(
            args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
        ) as network:
            network.start_and_open(args)
            primary, _ = network.find_nodes()
            for _ in range(n_repeats):
                with primary.client() as c:
                    r = c.get("/app/make_randoms")
                    assert r.status_code == 200, r
                    for _, n in r.body.json().items():
                        assert_fresh(n)


@reqs.description("Test request object API")
def test_request_object_api(network, args):
    primary, _ = network.find_nodes()

    def test_expectations(expected_fields, response):
        assert response.status_code == http.HTTPStatus.OK, response
        j = response.body.json()
        for k, v in expected_fields.items():
            ks = k.split(".")
            current = j
            for k_ in ks:
                assert k_ in current, f"Missing field {k} from response body"
                current = current[k_]
            actual = current
            assert (
                v == actual
            ), f"Mismatch at field {k}. Expected '{v}', response contains '{actual}'"
            # LOG.success(f"{k} == {v}")

    def test_url(expected_fields, client, base_url, query="", url_params=None):
        url = base_url
        expected_fields["route"] = base_url

        if url_params is not None:
            url = url.format(**url_params)

        expected_fields["path"] = url

        if len(query) > 0:
            url += f"?{query}"

        expected_fields["query"] = query
        expected_fields["url"] = url

        expected_fields["method"] = "GET"
        test_expectations(expected_fields, client.get(url))
        expected_fields["method"] = "POST"
        test_expectations(expected_fields, client.post(url))
        expected_fields["method"] = "DELETE"
        test_expectations(expected_fields, client.delete(url))

    def test_client(expected_fields, client):
        test_url(
            {**expected_fields, "hostname": f"{client.hostname}"},
            client,
            "/app/echo",
        )
        test_url(
            {**expected_fields, "hostname": f"{client.hostname}"},
            client,
            "/app/echo",
            query="a=42&b=hello_world",
        )
        test_url(
            {**expected_fields, "hostname": f"{client.hostname}"},
            client,
            "/app/echo/{foo}",
            url_params={"foo": "bar"},
        )
        test_url(
            {**expected_fields, "hostname": f"{client.hostname}"},
            client,
            "/app/echo/{foo}",
            query="a=42&b=hello_world",
            url_params={"foo": "bar"},
        )

    user = network.users[0]
    with primary.client(user.local_id) as c:
        test_client({"caller.policy": "user_cert", "caller.id": user.service_id}, c)

    with primary.client() as c:
        test_client({"caller.policy": "no_auth"}, c)

    return network


@reqs.description("Test Date API")
def test_datetime_api(network, args):
    primary, _ = network.find_nodes()

    with primary.client() as c:
        r = c.get("/time_now")
        local_time = datetime.datetime.now(datetime.timezone.utc)
        assert r.status_code == http.HTTPStatus.OK, r
        body = r.body.json()

        # Python datetime "ISO" doesn't parse Z suffix, so replace it
        default = body["default"].replace("Z", "+00:00")
        definitely_now = body["definitely_now"].replace("Z", "+00:00")
        definitely_1970 = body["definitely_1970"].replace("Z", "+00:00")

        # Assume less than 5ms of execution time between grabbing timestamps, and confirm that default call gets real timestamp from global activation
        default_time = datetime.datetime.fromisoformat(default)
        service_time = datetime.datetime.fromisoformat(definitely_now)
        diff = (service_time - default_time).total_seconds()
        assert diff < 0.005, diff

        # Assume less than 1 second of clock skew + execution time
        diff = (local_time - service_time).total_seconds()
        assert abs(diff) < 1, diff

        local_epoch_start = datetime.datetime.fromtimestamp(0, datetime.timezone.utc)
        service_epoch_start = datetime.datetime.fromisoformat(definitely_1970)
        assert local_epoch_start == service_epoch_start, service_epoch_start
    return network


@reqs.description("Test metrics logging")
def test_metrics_logging(network, args):
    primary, _ = network.find_nodes()

    # Add and test on a new node, so we can kill it to safely read its logs
    new_node = network.create_node("local://localhost")
    network.join_node(
        new_node,
        args.package,
        args,
    )
    network.trust_node(new_node, args)

    # Submit several requests
    assertions = []
    with new_node.client() as c:
        c.get("/app/echo")
        assertions.append({"Method": "GET", "Path": "/app/echo"})
        c.post("/app/echo")
        assertions.append({"Method": "POST", "Path": "/app/echo"})
        c.get("/app/echo/hello")
        assertions.append({"Method": "GET", "Path": "/app/echo/{foo}"})
        c.post("/app/echo/bar")
        assertions.append({"Method": "POST", "Path": "/app/echo/{foo}"})
        c.get("/app/fibonacci/10")
        assertions.append({"Method": "GET", "Path": "/app/fibonacci/{n}"})
        c.get("/app/fibonacci/20")
        assertions.append({"Method": "GET", "Path": "/app/fibonacci/{n}"})
        c.get("/app/fibonacci/30")
        assertions.append({"Method": "GET", "Path": "/app/fibonacci/{n}"})

    # Remove node
    network.retire_node(primary, new_node)
    new_node.stop()

    # Read node's logs
    metrics_regex = re.compile(
        r".*\[js\].*\| JS execution complete: Method=(?P<Method>.*), Path=(?P<Path>.*), Status=(?P<Status>\d+), ExecMilliseconds=(?P<ExecMilliseconds>\d+)$"
    )
    out_path, _ = new_node.get_logs()
    for line in open(out_path, "r", encoding="utf-8").readlines():
        match = metrics_regex.match(line)
        if match is not None:
            expected_groups = assertions.pop(0)
            for k, v in expected_groups.items():
                actual_match = match.group(k)
                assert actual_match == v
            LOG.success(f"Found metrics logging line: {line}")
            LOG.info(f"Parsed to: {match.groups()}")

    assert len(assertions) == 0

    return network


def run_api(args):
    test_random_api(args)

    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network = test_request_object_api(network, args)
        network = test_datetime_api(network, args)
        network = test_metrics_logging(network, args)


if __name__ == "__main__":
    cr = ConcurrentRunner()

    cr.add(
        "authz",
        run,
        nodes=infra.e2e_args.nodes(cr.args, 1),
        js_app_bundle=os.path.join(cr.args.js_app_bundle, "js-custom-authorization"),
    )

    cr.add(
        "limits",
        run_limits,
        nodes=infra.e2e_args.nodes(cr.args, 1),
        js_app_bundle=os.path.join(cr.args.js_app_bundle, "js-limits"),
    )

    cr.add(
        "authn",
        run_authn,
        nodes=infra.e2e_args.nodes(cr.args, 1),
        js_app_bundle=os.path.join(cr.args.js_app_bundle, "js-authentication"),
        initial_user_count=4,
        initial_member_count=2,
    )

    cr.add(
        "content_types",
        run_content_types,
        nodes=infra.e2e_args.nodes(cr.args, 1),
        js_app_bundle=os.path.join(cr.args.js_app_bundle, "js-content-types"),
    )

    cr.add(
        "api",
        run_api,
        nodes=infra.e2e_args.nodes(cr.args, 1),
        js_app_bundle=os.path.join(cr.args.js_app_bundle, "js-api"),
    )

    cr.run()
