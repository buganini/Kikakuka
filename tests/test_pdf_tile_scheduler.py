import unittest
from threading import Event

from pdf_tile_scheduler import PdfTileScheduler


class PdfTileSchedulerTests(unittest.TestCase):
    def make_scheduler(self, **kwargs):
        return PdfTileScheduler(
            object,
            lambda _renderer, _task: {},
            start_worker=False,
            **kwargs,
        )

    def test_request_reprioritizes_and_cancels_tiles_outside_viewport(self):
        scheduler = self.make_scheduler()
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        first_keys = scheduler.request(
            1.0, [(0, 0), (1, 0), (2, 0)], ("layer",)
        )
        second_keys = scheduler.request(
            1.0, [(2, 0), (1, 0)], ("layer",)
        )

        self.assertNotIn(first_keys[0], scheduler.pending)
        self.assertEqual(set(scheduler.pending), set(second_keys))
        self.assertEqual(
            scheduler.pending[second_keys[0]][1], 0
        )
        self.assertEqual(
            scheduler.pending[second_keys[1]][1], 1
        )

    def test_reordering_keeps_pending_tiles_without_better_priority(self):
        scheduler = self.make_scheduler()
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        keys = scheduler.request(1.0, [(0, 0), (1, 0), (2, 0)])
        original_tokens = {
            key: scheduler.pending[key][0] for key in keys
        }

        scheduler.request(1.0, [(2, 0), (1, 0), (0, 0)])

        self.assertEqual(scheduler.pending[keys[0]][0], original_tokens[keys[0]])
        self.assertEqual(scheduler.pending[keys[1]][0], original_tokens[keys[1]])
        self.assertNotEqual(
            scheduler.pending[keys[2]][0], original_tokens[keys[2]]
        )
        self.assertEqual(scheduler.queue.qsize(), 4)

    def test_coarse_tile_stays_pending_when_viewport_changes(self):
        scheduler = self.make_scheduler()
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        coarse_key = scheduler.prime_coarse(("layer",))

        scheduler.request(2.0, [(3, 1)], ("layer",))

        self.assertIn(coarse_key, scheduler.pending)
        self.assertEqual(scheduler.pending[coarse_key][1], -1)

    def test_cache_snapshot_counts_visible_results_and_pending_tiles(self):
        scheduler = self.make_scheduler(cache_bytes=1024)
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        keys = scheduler.request(1.0, [(0, 0), (1, 0), (2, 0)])
        scheduler.pending.pop(keys[0])
        scheduler.pending.pop(keys[1])
        scheduler.results[keys[0]] = {
            "memory_bytes": 16,
            "render_ms": 12.5,
            "pdf_render_ms": 8.0,
            "composite_ms": 3.0,
        }
        scheduler.results[keys[1]] = {"error": "render failed"}
        scheduler.cache_bytes = 16

        snapshot = scheduler.cache_snapshot(keys)

        self.assertEqual(snapshot["ready"], 1)
        self.assertEqual(snapshot["errors"], 1)
        self.assertEqual(snapshot["pending"], 1)
        self.assertEqual(snapshot["entries"], 2)
        self.assertEqual(snapshot["bytes"], 16)
        self.assertEqual(snapshot["limit_bytes"], 1024)
        self.assertEqual(snapshot["evictions"], 0)
        self.assertEqual(snapshot["queue_depth"], 3)
        self.assertEqual(snapshot["render_ms_avg"], 12.5)
        self.assertEqual(snapshot["pdf_render_ms_avg"], 8.0)
        self.assertEqual(snapshot["composite_ms_avg"], 3.0)

    def test_cache_snapshot_reports_evictions(self):
        scheduler = self.make_scheduler(cache_bytes=8)
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        first = (scheduler.generation, 2.0, 0, 0, ())
        second = (scheduler.generation, 2.0, 1, 0, ())

        scheduler._cache_result(first, {"memory_bytes": 6})
        scheduler._cache_result(second, {"memory_bytes": 6})

        self.assertNotIn(first, scheduler.results)
        self.assertEqual(scheduler.cache_snapshot([second])["evictions"], 1)

    def test_fallback_prefers_coarse_plus_nearest_detailed_lod(self):
        scheduler = self.make_scheduler()
        scheduler.reset({"canvas_size": (100.0, 100.0)})
        generation = scheduler.generation
        variant = ("layer",)
        scheduler.results[(generation, 0.25, 0, 0, variant)] = {
            "bounds": (0.0, 0.0, 100.0, 100.0),
            "coarse": True,
        }
        scheduler.results[(generation, 1.0, 0, 0, variant)] = {
            "bounds": (0.0, 0.0, 100.0, 100.0),
            "name": "near",
        }
        scheduler.results[(generation, 4.0, 0, 0, variant)] = {
            "bounds": (0.0, 0.0, 100.0, 100.0),
            "name": "far",
        }

        results = scheduler.fallback(
            2.0, variant, (0.0, 0.0, 100.0, 100.0)
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["coarse"])
        self.assertEqual(results[1]["name"], "near")

    def test_worker_publishes_renderer_result(self):
        ready = Event()

        def render_task(_renderer, task):
            return {
                "bounds": (
                    float(task["tile_x"]), float(task["tile_y"]), 1.0, 1.0
                ),
                "memory_bytes": 4,
            }

        scheduler = PdfTileScheduler(
            object, render_task, on_result=ready.set
        )
        scheduler.reset({"canvas_size": (10.0, 10.0)})
        key = scheduler.request(1.0, [(0, 0)])[0]

        self.assertTrue(ready.wait(1.0))
        self.assertEqual(scheduler.get(key)["memory_bytes"], 4)
        self.assertGreaterEqual(scheduler.get(key)["render_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
