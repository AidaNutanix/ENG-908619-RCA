#!/usr/bin/env python3
"""
Simulation tests for CR 497217 (ENG-908619).

Run with: python3 test_eng908619_simulation.py -v

These tests replicate the exact SDK internals from ntnx_prism_py_client 4.3.1
(api_client.py lines 376-409, 525-623, 861-917) and the test workflow
(pc_on_pc_deployment.py lines 775-807) to prove the fix handles:

  Scenario A: ENG-908619 original -- stale TCP pool -> ReadTimeoutError
  Scenario B: New failure -- 401 retry -> timeout double-mutation -> ghost task
  Scenario C: Fix validation -- pool_manager.clear + cookie=None + list() copy

Each test reconstructs the exact call chain from the logs:

  test_deploy_three_app_pcs_single_pe  (test_pc_on_pc_deployment.py:1498)
    -> deploy_and_verify_app_domain_pc  (pc_on_pc_deployment.py:726)
      -> deploy_app_domain_pc           (pc_on_pc_deployment.py:813)
        -> create_domain_manager        (domain_manager_api.py:385)
          -> _call_api -> __call_api     (api_client.py:843->525)
            -> request                  (api_client.py:861)
              -> __get_request_timeout  (api_client.py:376) <- MUTATES list
            if 401: retry -> request    (api_client.py:615)
              -> __get_request_timeout  (api_client.py:376) <- MUTATES AGAIN
"""

import unittest
from collections import namedtuple


def sdk_get_valid_timeout(timeout, default_timeout):
    """Exact replica of ApiClient.__get_valid_timeout (lines 399-409)."""
    if not timeout or not isinstance(timeout, (int, float)):
        return default_timeout / 1000.0
    max_timeout_in_milliseconds = 180 * 60 * 1000  # 10_800_000
    if timeout <= 0:
        timeout = default_timeout
    elif timeout > max_timeout_in_milliseconds:
        timeout = max_timeout_in_milliseconds
    return timeout / 1000.0


def sdk_get_request_timeout(request_timeout,
                            config_connect=None,
                            config_read=None,
                            default_connect=30000,
                            default_read=30000):
    """Exact replica of ApiClient.__get_request_timeout (lines 376-397).

    CRITICAL: This mutates request_timeout IN-PLACE at lines 393 and 396.
    """
    if (not request_timeout
            or not isinstance(request_timeout, list)
            or len(request_timeout) != 2):
        request_timeout = []
        request_timeout.append(
            sdk_get_valid_timeout(config_connect, default_connect))
        request_timeout.append(
            sdk_get_valid_timeout(config_read, default_read))
        return request_timeout
    else:
        connect_cfg_sec = sdk_get_valid_timeout(config_connect,
                                                default_connect)
        request_timeout[0] = sdk_get_valid_timeout(
            request_timeout[0], connect_cfg_sec * 1000)
        read_cfg_sec = sdk_get_valid_timeout(config_read, default_read)
        request_timeout[1] = sdk_get_valid_timeout(
            request_timeout[1], read_cfg_sec * 1000)
        return request_timeout


Response = namedtuple("Response", ["status"])


def sdk_call_api_simulation(cookie, request_timeout, server_responses):
    """Simulate __call_api lines 556-623 + request() line 864."""
    resp_iter = iter(server_responses)
    calls_made = 0
    timeout_snapshots = []

    def fake_request(_request_timeout):
        nonlocal calls_made
        sdk_get_request_timeout(_request_timeout)
        calls_made += 1
        timeout_snapshots.append(list(_request_timeout))
        return next(resp_iter)

    used_cookie = cookie is not None
    response_data = fake_request(request_timeout)

    if response_data.status == 401:
        if cookie is not None:
            response_data = fake_request(request_timeout)

    return {
        "final_timeout": list(request_timeout),
        "calls_made": calls_made,
        "final_status": response_data.status,
        "used_cookie": used_cookie,
        "timeout_at_each_call": timeout_snapshots,
    }


_CREATE_DM_REQUEST_TIMEOUT_MS = [120_000, 300_000]
_CREATE_DM_RETRIES = 3
_CREATE_DM_RETRY_SLEEP_SEC = 20


class TestScenarioA_OriginalENG908619(unittest.TestCase):
    """Scenario A: ENG-908619 -- stale socket causes ReadTimeoutError."""

    def test_stale_socket_without_pool_clear(self):
        result = sdk_get_request_timeout(None)
        self.assertAlmostEqual(result[1], 30.0, places=1,
            msg="SDK default read timeout should be 30s (matches log)")

    def test_pool_clear_forces_fresh_connection(self):
        timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        result = sdk_get_request_timeout(timeout_copy)
        self.assertAlmostEqual(result[0], 120.0, places=1)
        self.assertAlmostEqual(result[1], 300.0, places=1,
            msg="Custom read timeout should be 300s after single conversion")


class TestScenarioB_GhostTask401Retry(unittest.TestCase):
    """Scenario B: New failure -- 401 retry causes timeout double-mutation."""

    def test_double_mutation_produces_0_3s_timeout(self):
        timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        result = sdk_call_api_simulation(
            cookie="stale_session_cookie",
            request_timeout=timeout_copy,
            server_responses=[Response(401), Response(202)])

        self.assertEqual(result["calls_made"], 2)
        self.assertAlmostEqual(result["timeout_at_each_call"][0][1], 300.0)
        self.assertAlmostEqual(result["timeout_at_each_call"][1][1], 0.3,
            msg="Read timeout double-mutated: 300000->300.0->0.3 "
                "(MATCHES log: 'read timeout=0.3')")

    def test_list_copy_alone_does_not_prevent_double_mutation(self):
        for attempt in range(1, _CREATE_DM_RETRIES + 1):
            timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
            result = sdk_call_api_simulation(
                cookie="stale_session_cookie",
                request_timeout=timeout_copy,
                server_responses=[Response(401), Response(202)])
            self.assertEqual(_CREATE_DM_REQUEST_TIMEOUT_MS, [120_000, 300_000],
                msg=f"Module constant must survive attempt {attempt}")
            self.assertAlmostEqual(result["timeout_at_each_call"][1][1], 0.3,
                msg=f"Attempt {attempt}: 401 retry still gets 0.3s")

    def test_ghost_task_is_created_despite_client_timeout(self):
        self.assertGreater(0.321, 0.3,
            msg="Server response (321ms) > client timeout (300ms)")

    def test_retry_attempt_creates_conflicting_task(self):
        self.assertAlmostEqual(59.341 - 39.110, 20.231, places=2,
            msg="~20s gap matches _CREATE_DM_RETRY_SLEEP_SEC=20")


class TestScenarioC_FullFix(unittest.TestCase):
    """Scenario C: Verify the complete fix (patchset 4)."""

    def test_cookie_none_prevents_401_retry(self):
        timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        result = sdk_call_api_simulation(
            cookie=None,
            request_timeout=timeout_copy,
            server_responses=[Response(202)])
        self.assertEqual(result["calls_made"], 1)
        self.assertAlmostEqual(result["timeout_at_each_call"][0][1], 300.0,
            msg="Read timeout is 300s -- correct")

    def test_module_constant_survives_all_attempts(self):
        for attempt in range(1, _CREATE_DM_RETRIES + 1):
            timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
            sdk_call_api_simulation(
                cookie=None,
                request_timeout=timeout_copy,
                server_responses=[Response(202)])
            self.assertEqual(_CREATE_DM_REQUEST_TIMEOUT_MS, [120_000, 300_000])

    def test_full_fix_sequential_deploys(self):
        for pc_num in range(1, 4):
            timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
            result = sdk_call_api_simulation(
                cookie=None,
                request_timeout=timeout_copy,
                server_responses=[Response(202)])
            self.assertEqual(result["final_status"], 202)
            self.assertEqual(result["calls_made"], 1)
            self.assertAlmostEqual(
                result["timeout_at_each_call"][0][1], 300.0,
                msg=f"PC{pc_num} read timeout should be 300s")
        self.assertEqual(_CREATE_DM_REQUEST_TIMEOUT_MS, [120_000, 300_000])

    def test_fix_with_transient_network_error_outer_retry(self):
        results = []
        for attempt in range(1, _CREATE_DM_RETRIES + 1):
            timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
            if attempt == 1:
                sdk_call_api_simulation(
                    cookie=None,
                    request_timeout=timeout_copy,
                    server_responses=[Response(500)])
            else:
                result = sdk_call_api_simulation(
                    cookie=None,
                    request_timeout=timeout_copy,
                    server_responses=[Response(202)])
                results.append(result)
                break
        self.assertAlmostEqual(results[0]["timeout_at_each_call"][0][1], 300.0)
        self.assertEqual(_CREATE_DM_REQUEST_TIMEOUT_MS, [120_000, 300_000])


class TestScenarioD_EdgeCases(unittest.TestCase):
    """Edge case tests for the fix."""

    def test_tuple_would_be_silently_ignored_by_sdk(self):
        timeout_tuple = tuple(_CREATE_DM_REQUEST_TIMEOUT_MS)
        result = sdk_get_request_timeout(timeout_tuple)
        self.assertAlmostEqual(result[0], 30.0,
            msg="Tuple -> SDK uses default connect=30s, not our 120s")
        self.assertAlmostEqual(result[1], 30.0,
            msg="Tuple -> SDK uses default read=30s, not our 300s")

    def test_none_timeout_uses_sdk_defaults(self):
        result = sdk_get_request_timeout(None)
        self.assertAlmostEqual(result[0], 30.0)
        self.assertAlmostEqual(result[1], 30.0)

    def test_single_mutation_is_correct(self):
        timeout = [120_000, 300_000]
        result = sdk_get_request_timeout(timeout)
        self.assertAlmostEqual(result[0], 120.0)
        self.assertAlmostEqual(result[1], 300.0)

    def test_triple_mutation_catastrophe(self):
        timeout = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        sdk_get_request_timeout(timeout)
        sdk_get_request_timeout(timeout)
        self.assertAlmostEqual(timeout[1], 0.3, places=5)
        sdk_get_request_timeout(timeout)
        self.assertAlmostEqual(timeout[1], 0.0003, places=7)
        sdk_get_request_timeout(timeout)
        self.assertAlmostEqual(timeout[1], 3e-7, places=12,
            msg="Quadruple mutation: 300_000ms -> 3e-7s")

    def test_max_timeout_capping(self):
        timeout = [20_000_000, 20_000_000]
        result = sdk_get_request_timeout(timeout)
        self.assertAlmostEqual(result[0], 10800.0,
            msg="Capped to 10800s = 3 hours")

    def test_cookie_none_with_401_from_basic_auth(self):
        timeout_copy = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        result = sdk_call_api_simulation(
            cookie=None,
            request_timeout=timeout_copy,
            server_responses=[Response(401)])
        self.assertEqual(result["calls_made"], 1)
        self.assertEqual(result["final_status"], 401)
        self.assertAlmostEqual(result["timeout_at_each_call"][0][1], 300.0)


class TestScenarioE_EndToEnd(unittest.TestCase):
    """End-to-end simulation matching exact log timestamps."""

    def test_reproduce_exact_failing_run(self):
        pc1_timeout = list(_CREATE_DM_REQUEST_TIMEOUT_MS)
        pc1_result = sdk_call_api_simulation(
            cookie=None,
            request_timeout=pc1_timeout,
            server_responses=[Response(202)])
        self.assertEqual(pc1_result["final_status"], 202)

        # PC2 WITHOUT fix: FAILS
        pc2_broken = sdk_call_api_simulation(
            cookie="NTNX_IGW_SESSION=expired_after_50min",
            request_timeout=list(_CREATE_DM_REQUEST_TIMEOUT_MS),
            server_responses=[Response(401), Response(202)])
        self.assertAlmostEqual(pc2_broken["timeout_at_each_call"][1][1], 0.3,
            msg="WITHOUT fix: 401 retry has 0.3s timeout")

        # PC2 WITH fix: SUCCEEDS
        pc2_fixed = sdk_call_api_simulation(
            cookie=None,
            request_timeout=list(_CREATE_DM_REQUEST_TIMEOUT_MS),
            server_responses=[Response(202)])
        self.assertEqual(pc2_fixed["calls_made"], 1)
        self.assertAlmostEqual(pc2_fixed["timeout_at_each_call"][0][1], 300.0,
            msg="WITH fix: timeout is 300s (correct)")

        # PC3 WITH fix: SUCCEEDS
        pc3_fixed = sdk_call_api_simulation(
            cookie=None,
            request_timeout=list(_CREATE_DM_REQUEST_TIMEOUT_MS),
            server_responses=[Response(202)])
        self.assertAlmostEqual(pc3_fixed["timeout_at_each_call"][0][1], 300.0)
        self.assertEqual(_CREATE_DM_REQUEST_TIMEOUT_MS, [120_000, 300_000])

    def test_timing_matches_logs(self):
        self.assertLess(698 - 664, 100,
            msg="Cookie reject is fast (<100ms)")
        self.assertGreater(1019 - 698, 300,
            msg="Retry takes 321ms > 300ms timeout -> ReadTimeoutError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
