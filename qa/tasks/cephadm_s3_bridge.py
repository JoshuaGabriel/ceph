"""
Bridge task to make cephadm-deployed RGW compatible with s3tests.

This task discovers RGW endpoints deployed via cephadm orchestrator
and creates the ctx.rgw.role_endpoints structure that s3tests expects.
"""

import json
import logging
import time
from io import StringIO

from teuthology.orchestra import run
from teuthology import misc as teuthology
from teuthology.exceptions import ConfigError
import teuthology.orchestra.remote

import sys
import os

qa_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, qa_dir)
from rgw import RGWEndpoint

log = logging.getLogger(__name__)


def detect_cephadm_deployment(ctx):
    """Detect if we're in a cephadm environment with bridge active"""
    return (
        hasattr(ctx, "rgw")
        and hasattr(ctx.rgw, "cephadm_bridge_active")
        and ctx.rgw.cephadm_bridge_active
    )


def patch_s3tests_radosgw_admin(ctx):
    """
    Monkey patch teuthology remote execution to make radosgw-admin commands
    work inside cephadm containers when running s3tests.

    Many teuthology tasks (eg. s3tests, rgw helpers) invoke radosgw-admin with
    wrapper prefixes like ["adjust-ulimits", "ceph-coverage", <path>, ... ,
    "radosgw-admin", ...]. The original patch only matched when args[0] was
    "radosgw-admin" which missed these cases. Here we detect radosgw-admin at
    any position, split the prefix, and wrap only the radosgw-admin portion
    inside a 'sudo <cephadm> shell -c ... -k ... -- <radosgw-admin ...>' call.
    """
    log.info("Enabling cephadm-aware radosgw-admin monkey patch for s3tests")

    original_run = teuthology.orchestra.remote.Remote.run

    def cephadm_aware_run(self, **kwargs):
        args = kwargs.get("args", [])

        try:
            # Locate the radosgw-admin binary within args (not just at index 0)
            admin_idx = -1
            for i, a in enumerate(args):
                if isinstance(a, str) and a == "radosgw-admin":
                    admin_idx = i
                    break

            if admin_idx != -1 and detect_cephadm_deployment(ctx):
                log.info(f"Intercepting radosgw-admin command: {args}")

                cluster_name = list(ctx.ceph.keys())[0] if hasattr(ctx, "ceph") else "ceph"
                image = ctx.ceph[cluster_name].image
                fsid = ctx.ceph[cluster_name].fsid
                cephadm_bin = getattr(ctx, "cephadm", "cephadm")

                # Everything before radosgw-admin should remain as-is
                prefix = list(args[:admin_idx])
                admin_and_rest = list(args[admin_idx:])

                cephadm_prefix = [
                    "sudo",
                    cephadm_bin,
                    "--image", image,
                    "shell",
                    "-c", f"/etc/ceph/{cluster_name}.conf",
                    "-k", f"/etc/ceph/{cluster_name}.client.admin.keyring",
                    "--fsid", fsid,
                    "--",
                ]

                new_args = prefix + cephadm_prefix + admin_and_rest
                log.info(f"Converted to cephadm shell command: {new_args}")
                kwargs["args"] = new_args

        except Exception as e:
            # On any failure, fall back to original behavior
            log.error(f"cephadm radosgw-admin monkey patch error: {e}")

        return original_run(self, **kwargs)

    teuthology.orchestra.remote.Remote.run = cephadm_aware_run


def restore_original_remote_run():
    """Restore original remote run method (for cleanup)"""
    log.info("not implemented - patch remains active")


def discover_cephadm_rgw_endpoints(ctx):
    """
    Discover RGW endpoints from cephadm orchestrator using cephadm shell.
    Returns dict mapping service names to endpoint info.
    """
    log.info("Discovering cephadm RGW endpoints via 'ceph orch ps'")

    cluster_roles = list(ctx.cluster.remotes.keys())
    if not cluster_roles:
        raise ConfigError("No cluster nodes available for ceph commands")

    remote = cluster_roles[0]

    try:
        # Get cluster name (usually 'ceph')
        cluster_name = list(ctx.ceph.keys())[0] if hasattr(ctx, "ceph") else "ceph"

        result = remote.run(
            args=[
                "sudo",
                ctx.cephadm,
                "--image",
                ctx.ceph[cluster_name].image,
                "shell",
                "-c",
                f"/etc/ceph/{cluster_name}.conf",
                "-k",
                f"/etc/ceph/{cluster_name}.client.admin.keyring",
                "--fsid",
                ctx.ceph[cluster_name].fsid,
                "--",
                "ceph",
                "orch",
                "ps",
                "--daemon_type",
                "rgw",
                "--format",
                "json",
            ],
            stdout=StringIO(),
        )
    except AttributeError as e:
        log.error(f"Missing cephadm context attributes: {e}")
        log.error(
            "Available ctx.cephadm attributes: " + str(dir(ctx.cephadm))
            if hasattr(ctx, "cephadm")
            else "No ctx.cephadm found"
        )
        raise ConfigError(f"cephadm context not properly initialized: {e}")
    except Exception as e:
        log.error(f"Failed to run ceph orch ps command: {e}")
        raise ConfigError(f"RGW endpoint discovery failed: {e}")

    services_json = result.stdout.getvalue()
    log.info(f"Raw ceph orch ps output: {services_json}")

    if not services_json.strip():
        log.warning("No RGW services found via 'ceph orch ps'")
        return {}

    try:
        services = json.loads(services_json)
        log.info(f"Parsed RGW services: {services}")
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse JSON from ceph orch ps: {e}")
        log.error(f"Raw output was: {services_json}")
        raise ConfigError(f"Invalid JSON from ceph orch ps: {e}")

    endpoints = {}
    for service in services:
        service_name = service.get("service_name", "")
        hostname = service.get("hostname", "")
        ports = service.get("ports", [])
        status = service.get("status_desc", "")

        if not service_name.startswith("rgw."):
            continue

        if status != "running":
            log.warning(f"RGW service {service_name} is not running: {status}")
            continue

        if not ports:
            log.warning(f"No ports found for RGW service {service_name}")
            continue

        # Extract port number (ports is typically ['8080/tcp'] format)
        port = None
        for port_spec in ports:
            if isinstance(port_spec, str) and "/" in port_spec:
                port = int(port_spec.split("/")[0])
                break
            elif isinstance(port_spec, int):
                port = port_spec
                break

        if port is None:
            log.warning(f"Could not parse port for RGW service {service_name}: {ports}")
            continue

        endpoints[service_name] = {
            "hostname": hostname,
            "port": port,
            "service_name": service_name,
            "status": status,
        }

    log.info(f"Discovered RGW endpoints: {endpoints}")
    return endpoints


def map_roles_to_endpoints(ctx, config, discovered_endpoints):
    """
    Map teuthology roles to discovered RGW endpoints.
    """
    role_endpoints = {}

    for role, client_config in config.items():
        if not client_config.get("discover_from_cephadm"):
            continue

        log.info(f"Mapping role {role} to cephadm RGW endpoint")

        target_service = client_config.get("rgw_service")
        if target_service and target_service in discovered_endpoints:
            endpoint_info = discovered_endpoints[target_service]
            log.info(f"Using explicit service mapping: {role} -> {target_service}")
        else:
            if not discovered_endpoints:
                raise ConfigError(f"No RGW endpoints discovered for role {role}")

            service_name = list(discovered_endpoints.keys())[0]
            endpoint_info = discovered_endpoints[service_name]
            log.info(f"Using first available RGW service: {role} -> {service_name}")

        hostname = endpoint_info["hostname"]
        port = endpoint_info["port"]

        dns_name = client_config.get("dns_name", hostname)

        rgw_endpoint = RGWEndpoint(
            hostname=hostname,
            port=port,
            cert=None,
            dns_name=dns_name,
            website_dns_name=None,
        )

        role_endpoints[role] = rgw_endpoint
        log.info(f"Created endpoint for {role}: {hostname}:{port} (dns: {dns_name})")

    return role_endpoints


def wait_for_rgw_accessibility(ctx, role_endpoints, timeout=60):
    """
    Wait for RGW endpoints to be accessible via HTTP.
    """
    log.info("Verifying RGW endpoint accessibility")

    cluster_roles = list(ctx.cluster.remotes.keys())
    test_remote = cluster_roles[0]

    for role, endpoint in role_endpoints.items():
        log.info(
            f"Testing accessibility of {role} at {endpoint.hostname}:{endpoint.port}"
        )

        start_time = time.time()
        accessible = False

        while time.time() - start_time < timeout:
            try:
                result = test_remote.run(
                    args=[
                        "curl",
                        "-s",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "--connect-timeout",
                        "5",
                        f"http://{endpoint.hostname}:{endpoint.port}/",
                    ],
                    stdout=StringIO(),
                    check_status=False,
                )

                http_code = result.stdout.getvalue().strip()
                log.info(f"HTTP response from {role}: {http_code}")

                if http_code and http_code.isdigit():
                    accessible = True
                    break

            except Exception as e:
                log.debug(f"Accessibility test failed for {role}: {e}")

            log.info(f"Waiting for {role} to become accessible...")
            time.sleep(2)

        if not accessible:
            raise ConfigError(f"RGW endpoint {role} not accessible after {timeout}s")

        log.info(f"RGW endpoint {role} is accessible")


def task(ctx, config):
    """
    Bridge task to make cephadm-deployed RGW compatible with s3tests.

    Example usage:
    - cephadm_s3_bridge:
        client.0:
          discover_from_cephadm: true
          dns_name: rgw.example.com  # optional
          rgw_service: rgw.myservice  # optional, defaults to first found
    """
    if config is None:
        config = {}

    log.info("Starting cephadm s3tests bridge task")

    assert hasattr(ctx, "ceph"), (
        "ctx.ceph not found - cephadm bridge requires ceph context"
    )
    assert hasattr(ctx, "cephadm"), (
        "ctx.cephadm not found - cephadm bridge requires cephadm context"
    )
    assert hasattr(ctx, "cluster"), (
        "ctx.cluster not found - cephadm bridge requires cluster context"
    )

    log.info("Context assertions passed, checking for existing ctx.rgw...")

    # Allow ctx.rgw to exist from cephadm tasks, but ensure it doesn't have role_endpoints
    if hasattr(ctx, "rgw") and hasattr(ctx.rgw, "role_endpoints"):
        raise ConfigError(
            "ctx.rgw.role_endpoints already exists - bridge should run before other rgw configuration tasks"
        )

    try:
        discovered_endpoints = discover_cephadm_rgw_endpoints(ctx)
    except Exception as e:
        log.error(f"RGW endpoint discovery failed: {e}")
        raise e

    if not discovered_endpoints:
        raise ConfigError("No RGW services found via cephadm orchestrator")

    role_endpoints = map_roles_to_endpoints(ctx, config, discovered_endpoints)

    if not role_endpoints:
        log.error("No roles configured for RGW endpoint mapping")
        log.error(
            "Check your bridge task configuration - you need at least one role with 'discover_from_cephadm: true'"
        )
        return

    try:
        wait_for_rgw_accessibility(ctx, role_endpoints)
    except Exception as e:
        log.error(f"RGW accessibility test failed: {e}")
        log.error("Continuing anyway - ctx.rgw will still be created")

    # Create ctx.rgw structure for s3tests compatibility
    if not hasattr(ctx, "rgw"):

        class RGWContext:
            pass

        ctx.rgw = RGWContext()

    ctx.rgw.role_endpoints = role_endpoints
    ctx.rgw.cephadm_discovered_endpoints = discovered_endpoints
    ctx.rgw.cephadm_bridge_active = True

    try:
        patch_s3tests_radosgw_admin(ctx)
    except Exception as e:
        log.error(f"Monkey patch setup failed: {e}")
        raise e

    assert hasattr(ctx, "rgw"), "ctx.rgw was not created successfully"
    assert hasattr(ctx.rgw, "role_endpoints"), "ctx.rgw.role_endpoints was not created"
    assert hasattr(ctx.rgw, "cephadm_bridge_active"), (
        "ctx.rgw.cephadm_bridge_active was not set"
    )
    assert ctx.rgw.cephadm_bridge_active, "ctx.rgw.cephadm_bridge_active is not True"
    assert len(ctx.rgw.role_endpoints) > 0, "ctx.rgw.role_endpoints is empty"

    try:
        yield
    finally:
        assert hasattr(ctx, "rgw"), "ctx.rgw was lost during test execution"
        assert hasattr(ctx.rgw, "cephadm_bridge_active"), (
            "ctx.rgw.cephadm_bridge_active was lost"
        )
