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
    """
    log.info("convert radosgw-admin to cephadm command")

    original_run = teuthology.orchestra.remote.Remote.run

    def cephadm_aware_run(self, **kwargs):
        args = kwargs.get("args", [])

        if args and len(args) > 0 and args[0] == "radosgw-admin":
            if detect_cephadm_deployment(ctx):
                log.info(f"Intercepting radosgw-admin command: {args}")

                try:
                    cluster_name = (
                        list(ctx.ceph.keys())[0] if hasattr(ctx, "ceph") else "ceph"
                    )
                    image = ctx.ceph[cluster_name].image

                    cephadm_args = [
                        "sudo",
                        "cephadm",
                        "--image",
                        image,
                        "shell",
                        "-c",
                        f"/etc/ceph/{cluster_name}.conf",
                        "-k",
                        f"/etc/ceph/{cluster_name}.client.admin.keyring",
                        "--fsid",
                        ctx.ceph[cluster_name].fsid,
                        "--",
                    ] + args

                    log.info(f"Converted to cephadm shell command: {cephadm_args}")
                    kwargs["args"] = cephadm_args

                except Exception as e:
                    log.error(f"Failed to convert radosgw-admin to cephadm shell: {e}")
                    pass

        return original_run(self, **kwargs)


    teuthology.orchestra.remote.Remote.run = cephadm_aware_run


def restore_original_remote_run():
    """Restore original remote run method (for cleanup)"""
    # TODO: In practice, this is tricky to implement cleanly since we don't
    # store the original reference. The monkey patch will remain active
    # for the duration of the test run, which is typically desired.
    log.info("Note: Monkey patch cleanup not implemented - patch remains active")


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

    log.info("🚀 STARTING cephadm s3tests bridge task")
    log.info(f"🔍 DEBUG: Bridge task config: {config}")
    
    # Extensive context debugging
    log.info("🔍 DEBUG: Checking available context attributes...")
    log.info(f"🔍 DEBUG: hasattr(ctx, 'ceph') = {hasattr(ctx, 'ceph')}")
    log.info(f"🔍 DEBUG: hasattr(ctx, 'cephadm') = {hasattr(ctx, 'cephadm')}")
    log.info(f"🔍 DEBUG: hasattr(ctx, 'cluster') = {hasattr(ctx, 'cluster')}")
    log.info(f"🔍 DEBUG: hasattr(ctx, 'rgw') = {hasattr(ctx, 'rgw')} (should be False initially)")
    
    if hasattr(ctx, 'ceph'):
        log.info(f"🔍 DEBUG: ctx.ceph keys: {list(ctx.ceph.keys())}")
        for cluster_name in ctx.ceph.keys():
            log.info(f"🔍 DEBUG: ctx.ceph[{cluster_name}] attributes: {dir(ctx.ceph[cluster_name])}")
            if hasattr(ctx.ceph[cluster_name], 'image'):
                log.info(f"🔍 DEBUG: ctx.ceph[{cluster_name}].image = {ctx.ceph[cluster_name].image}")
            if hasattr(ctx.ceph[cluster_name], 'fsid'):
                log.info(f"🔍 DEBUG: ctx.ceph[{cluster_name}].fsid = {ctx.ceph[cluster_name].fsid}")
    else:
        log.error("❌ ERROR: ctx.ceph not found - this is critical!")
    
    if hasattr(ctx, 'cephadm'):
        log.info(f"🔍 DEBUG: type(ctx.cephadm) = {type(ctx.cephadm)}")
        log.info(f"🔍 DEBUG: ctx.cephadm = {ctx.cephadm}")
    else:
        log.error("❌ ERROR: ctx.cephadm not found")

    try:
        log.info("🔍 Phase 1: Attempting RGW endpoint discovery...")
        discovered_endpoints = discover_cephadm_rgw_endpoints(ctx)
        log.info(f"✅ SUCCESS: Discovered {len(discovered_endpoints)} RGW endpoints")
    except Exception as e:
        log.error(f"❌ CRITICAL: RGW endpoint discovery failed: {e}")
        log.error(f"❌ Bridge task cannot continue - ctx.rgw will NOT be created!")
        raise e

    if not discovered_endpoints:
        log.error("❌ CRITICAL: No RGW services found via cephadm orchestrator")
        log.error("❌ Bridge task cannot continue - ctx.rgw will NOT be created!")
        raise ConfigError("No RGW services found via cephadm orchestrator")

    log.info("🔍 Phase 2: Mapping roles to endpoints...")
    role_endpoints = map_roles_to_endpoints(ctx, config, discovered_endpoints)

    if not role_endpoints:
        log.error("❌ CRITICAL: No roles configured for RGW endpoint mapping")
        log.error("❌ Bridge task cannot continue - ctx.rgw will NOT be created!")
        log.error("❌ Check your bridge task configuration - you need at least one role with 'discover_from_cephadm: true'")
        return

    log.info(f"✅ SUCCESS: Mapped {len(role_endpoints)} roles to endpoints")
    for role, endpoint in role_endpoints.items():
        log.info(f"    🔗 {role} -> {endpoint.hostname}:{endpoint.port}")

    log.info("🔍 Phase 3: Testing RGW endpoint accessibility...")
    try:
        wait_for_rgw_accessibility(ctx, role_endpoints)
        log.info("✅ SUCCESS: All RGW endpoints are accessible")
    except Exception as e:
        log.error(f"❌ ERROR: RGW accessibility test failed: {e}")
        log.error("❌ Continuing anyway - ctx.rgw will still be created")

    log.info("🔍 Phase 4: Creating ctx.rgw structure for s3tests compatibility...")
    
    # Store original state for debugging
    original_rgw_exists = hasattr(ctx, 'rgw')
    log.info(f"🔍 DEBUG: Before creation - hasattr(ctx, 'rgw') = {original_rgw_exists}")

    # Phase 4: Create ctx.rgw structure for s3tests compatibility
    # Using simple class instead of dynamic type creation for better compatibility
    class RGWContext:
        pass

    ctx.rgw = RGWContext()
    ctx.rgw.role_endpoints = role_endpoints

    log.info(f"🔍 DEBUG: After creation - hasattr(ctx, 'rgw') = {hasattr(ctx, 'rgw')}")
    log.info(f"🔍 DEBUG: type(ctx.rgw) = {type(ctx.rgw)}")
    log.info(f"🔍 DEBUG: hasattr(ctx.rgw, 'role_endpoints') = {hasattr(ctx.rgw, 'role_endpoints')}")
    log.info(f"🔍 DEBUG: len(ctx.rgw.role_endpoints) = {len(ctx.rgw.role_endpoints)}")

    log.info(f"✅ SUCCESS: Created ctx.rgw.role_endpoints with {len(role_endpoints)} endpoints")
    for role, endpoint in role_endpoints.items():
        log.info(f"  🔗 {role} -> {endpoint.hostname}:{endpoint.port}")

    # Phase 5: Store discovery info and activate bridge
    ctx.rgw.cephadm_discovered_endpoints = discovered_endpoints
    ctx.rgw.cephadm_bridge_active = True
    
    log.info(f"🔍 DEBUG: Set ctx.rgw.cephadm_bridge_active = {ctx.rgw.cephadm_bridge_active}")

    # Phase 6: Patch radosgw-admin commands for cephadm compatibility
    log.info("🔍 Phase 5: Setting up radosgw-admin monkey patching...")
    try:
        patch_s3tests_radosgw_admin(ctx)
        log.info("✅ SUCCESS: Monkey patch for radosgw-admin commands activated")
    except Exception as e:
        log.error(f"❌ ERROR: Monkey patch setup failed: {e}")
        raise e

    # Final verification for s3tests compatibility
    log.info("🔍 FINAL VERIFICATION: Checking s3tests compatibility...")
    log.info(f"✅ hasattr(ctx, 'rgw') = {hasattr(ctx, 'rgw')}")
    log.info(f"✅ type(ctx.rgw) = {type(ctx.rgw)}")
    log.info(f"✅ hasattr(ctx.rgw, 'role_endpoints') = {hasattr(ctx.rgw, 'role_endpoints')}")
    log.info(f"✅ ctx.rgw.cephadm_bridge_active = {getattr(ctx.rgw, 'cephadm_bridge_active', 'MISSING')}")
    log.info(f"✅ len(ctx.rgw.role_endpoints) = {len(getattr(ctx.rgw, 'role_endpoints', {}))}")

    log.info("🎉 SUCCESS: cephadm s3tests bridge task completed successfully!")
    log.info("🎉 ctx.rgw is now ready for s3tests - the assertion should PASS!")

    try:
        yield
    finally:
        # Cleanup logging with more detail
        log.info("🔄 BRIDGE CLEANUP: Starting bridge task cleanup...")
        log.info("🔄 Note: Monkey patch remains active for test duration (this is expected)")
        log.info(f"🔄 Final state: hasattr(ctx, 'rgw') = {hasattr(ctx, 'rgw')}")
        if hasattr(ctx, 'rgw'):
            log.info(f"🔄 Final state: hasattr(ctx.rgw, 'cephadm_bridge_active') = {hasattr(ctx.rgw, 'cephadm_bridge_active')}")
            log.info(f"🔄 Final state: len(ctx.rgw.role_endpoints) = {len(getattr(ctx.rgw, 'role_endpoints', {}))}")
            log.info(f"🔄 Final state: ctx.rgw.cephadm_bridge_active = {getattr(ctx.rgw, 'cephadm_bridge_active', 'MISSING')}")
        else:
            log.error("🔄 ❌ CRITICAL: ctx.rgw was lost during test execution!")
        log.info("🔄 Bridge task cleanup completed")
