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

    def test_coarse_tile_stays_pending_when_viewport_changes(self):
        scheduler = self.make_scheduler()
        scheduler.reset({"canvas_size": (2048.0, 1024.0)})
        coarse_key = scheduler.prime_coarse(("layer",))

        scheduler.request(2.0, [(3, 1)], ("layer",))

        self.assertIn(coarse_key, scheduler.pending)
        self.assertEqual(scheduler.pending[coarse_key][1], -1)

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


if __name__ == "__main__":
    unittest.main()
