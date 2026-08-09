import unittest

from tracker import (
    ApiResult,
    USER_ADDRESS,
    add_snapshot,
    backfill_orders_for_fills,
    canonicalize_funding,
    fill_key,
    merge_latest,
    new_snapshot_document,
    order_key,
)


def funding(time_ms, usdc, *, samples=None, rate="0.0001"):
    delta = {
        "type": "funding",
        "coin": "BTC",
        "usdc": str(usdc),
        "szi": "1.0",
        "fundingRate": rate,
    }
    if samples is not None:
        delta["nSamples"] = samples
    return {"time": time_ms, "hash": "0x0", "delta": delta}


class FundingMergeTests(unittest.TestCase):
    def test_complete_hourly_rows_replace_matching_daily_aggregate(self):
        day = 1_700_006_400_000
        hourly = [funding(day + index * 3_600_000, "-1") for index in range(24)]
        aggregate = funding(day + 23 * 3_600_000, "-24", samples=24)

        result, report = canonicalize_funding([hourly, [aggregate]])

        self.assertEqual(len(result), 24)
        self.assertTrue(all(row["delta"].get("nSamples") is None for row in result))
        self.assertEqual(report["recoveredHourlyDays"], 1)
        self.assertEqual(report["usdcTotal"], "-24")

    def test_incomplete_hourly_rows_fall_back_to_daily_aggregate(self):
        day = 1_700_006_400_000
        hourly = [funding(day + index * 3_600_000, "-1") for index in range(23)]
        aggregate = funding(day + 23 * 3_600_000, "-24", samples=24)

        result, report = canonicalize_funding([hourly, [aggregate]])

        self.assertEqual(result, [aggregate])
        self.assertEqual(report["fallbackAggregateDays"], 1)
        self.assertEqual(report["usdcTotal"], "-24")

    def test_latest_aggregate_revision_controls_representation(self):
        day = 1_700_006_400_000
        hourly = [funding(day + index * 3_600_000, "-1") for index in range(24)]
        old = funding(day + 23 * 3_600_000, "-24", samples=24)
        revised = funding(day + 23 * 3_600_000, "-24.5", samples=24)

        result, report = canonicalize_funding([hourly, [old], [revised]])

        self.assertEqual(result, [revised])
        self.assertEqual(report["usdcTotal"], "-24.5")


class StableIdentityTests(unittest.TestCase):
    def test_order_status_events_are_distinct_but_exact_repeats_deduplicate(self):
        base_order = {"coin": "BTC", "oid": 7, "timestamp": 100}
        opened = {"order": dict(base_order), "status": "open", "statusTimestamp": 100}
        filled = {"order": dict(base_order), "status": "filled", "statusTimestamp": 200}

        result, conflicts = merge_latest([[opened], [opened, filled]], order_key)

        self.assertEqual(len(result), 2)
        self.assertEqual(conflicts, 0)

    def test_fill_hash_is_not_part_of_identity(self):
        first = {"coin": "BTC", "time": 100, "tid": 9, "hash": "old"}
        second = {"coin": "BTC", "time": 100, "tid": 9, "hash": "new"}

        result, conflicts = merge_latest([[first], [second]], fill_key)

        self.assertEqual(result, [second])
        self.assertEqual(conflicts, 1)

    def test_missing_fill_order_is_recovered_from_order_status(self):
        recovered_record = {
            "order": {"coin": "BTC", "oid": 9, "timestamp": 100},
            "status": "filled",
            "statusTimestamp": 100,
        }

        class FakeClient:
            def post(self, payload):
                self.payload = payload
                return ApiResult(
                    {"status": "order", "order": recovered_record},
                    "2026-01-01T00:00:00+00:00",
                )

        client = FakeClient()
        records, report = backfill_orders_for_fills(
            client,
            USER_ADDRESS,
            [],
            [{"coin": "BTC", "time": 100, "tid": 1, "oid": 9}],
        )

        self.assertEqual(records, [recovered_record])
        self.assertEqual(report["recoveredOids"], [9])
        self.assertEqual(report["unresolvedOids"], [])
        self.assertEqual(client.payload["type"], "orderStatus")


class SnapshotHistoryTests(unittest.TestCase):
    def test_snapshot_history_keeps_separate_checkpoints_and_deduplicates_retry(self):
        document = new_snapshot_document("openOrders", USER_ADDRESS)
        response = [{"coin": "BTC", "oid": 1}]
        add_snapshot(document, "openOrders", USER_ADDRESS, "run-1", "2026-01-01T00:00:00Z", response)
        add_snapshot(document, "openOrders", USER_ADDRESS, "run-1", "2026-01-01T00:00:00Z", response)
        add_snapshot(document, "openOrders", USER_ADDRESS, "run-2", "2026-01-02T00:00:00Z", [])

        self.assertEqual(len(document["snapshots"]), 2)
        self.assertEqual(document["snapshots"][0]["response"], response)

    def test_later_retry_replaces_earlier_checkpoint_on_same_utc_day(self):
        document = new_snapshot_document("openOrders", USER_ADDRESS)
        add_snapshot(
            document,
            "openOrders",
            USER_ADDRESS,
            "run-1",
            "2026-01-01T00:00:00+00:00",
            [{"coin": "BTC", "oid": 1}],
        )
        add_snapshot(
            document,
            "openOrders",
            USER_ADDRESS,
            "run-2",
            "2026-01-01T12:00:00+00:00",
            [],
        )

        self.assertEqual(len(document["snapshots"]), 1)
        self.assertEqual(document["snapshots"][0]["runId"], "run-2")
        self.assertEqual(document["snapshots"][0]["response"], [])


if __name__ == "__main__":
    unittest.main()
