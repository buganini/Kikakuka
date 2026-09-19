import math
import queue
from collections import OrderedDict
from threading import Lock, Thread


DEFAULT_TILE_SIZE = 512
DEFAULT_RENDER_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def choose_fallback_scale(available_scales, render_scale):
    available_scales = tuple(available_scales)
    if not available_scales:
        return None
    return min(
        available_scales,
        key=lambda scale: abs(math.log2(scale / render_scale)),
    )


def select_fallback_results(coarse_results, candidates_by_scale,
                            render_scale):
    results = list(coarse_results)
    fallback_scale = choose_fallback_scale(
        candidates_by_scale, render_scale
    )
    if fallback_scale is not None:
        results.extend(candidates_by_scale[fallback_scale])
    return results


def choose_coarse_render_scale(canvas_size, tile_size=DEFAULT_TILE_SIZE,
                               render_scales=DEFAULT_RENDER_SCALES):
    width, height = canvas_size
    if width <= 0 or height <= 0:
        raise ValueError("PDF canvas must have a positive size")
    fit_scale = min(tile_size / width, tile_size / height)
    standard_scales = [
        scale for scale in render_scales
        if scale <= 0.5 and scale <= fit_scale
    ]
    if standard_scales:
        return standard_scales[-1]
    return min(0.5, fit_scale * (1.0 - 1e-9))


class PdfTileScheduler:
    """Prioritize, cancel, and cache viewport tiles for a PDF renderer."""

    def __init__(self, renderer_factory, render_task, *,
                 cache_bytes=384 * 1024 * 1024,
                 low_res_cache_bytes=64 * 1024 * 1024,
                 low_res_max_scale=1.0, on_result=None,
                 start_worker=True):
        self.renderer_factory = renderer_factory
        self.render_task = render_task
        self.cache_limit = cache_bytes
        self.low_res_cache_limit = low_res_cache_bytes
        self.low_res_max_scale = low_res_max_scale
        self.on_result = on_result

        self.queue = queue.PriorityQueue()
        self.lock = Lock()
        self.task_sequence = 0
        self.generation = 0
        self.metadata = None
        self.pending = {}
        self.results = OrderedDict()
        self.cache_bytes = 0
        self.active = set()
        self.coarse_key = None
        self.priority_order = ()

        if start_worker:
            Thread(target=self._worker, daemon=True).start()

    def reset(self, metadata):
        with self.lock:
            self.generation += 1
            self.metadata = metadata
            self.pending.clear()
            self.results.clear()
            self.cache_bytes = 0
            self.active.clear()
            self.coarse_key = None
            self.priority_order = ()

    def _queue_task(self, task, priority):
        self.task_sequence += 1
        token = self.task_sequence
        task["token"] = token
        self.pending[task["key"]] = (token, priority)
        self.queue.put((priority, token, task))

    def prime_coarse(self, variant=()):
        with self.lock:
            if self.metadata is None:
                return None
            generation = self.generation
            render_scale = choose_coarse_render_scale(
                self.metadata["canvas_size"]
            )
            variant = tuple(variant)
            key = (generation, render_scale, 0, 0, variant)
            self.coarse_key = key
            if key in self.results or key in self.pending:
                return key
            task = {
                "key": key,
                "generation": generation,
                "metadata": self.metadata,
                "render_scale": render_scale,
                "tile_x": 0,
                "tile_y": 0,
                "variant": variant,
                "pinned": True,
                "coarse": True,
            }
            self._queue_task(task, -1)
            return key

    def request(self, render_scale, tile_indices, variant=()):
        with self.lock:
            generation = self.generation
            variant = tuple(variant)
            keys = [
                (generation, render_scale, tile_x, tile_y, variant)
                for tile_x, tile_y in tile_indices
            ]
            self.active = set(keys)
            priority_order = tuple(keys)
            if priority_order != self.priority_order:
                self.priority_order = priority_order
                for pending_key in tuple(self.pending):
                    if pending_key != self.coarse_key:
                        self.pending.pop(pending_key, None)
            for pending_key in tuple(self.pending):
                if (pending_key not in self.active and
                        pending_key != self.coarse_key):
                    self.pending.pop(pending_key, None)
            if self.metadata is None:
                return keys
            for priority, (key, (tile_x, tile_y)) in enumerate(
                    zip(keys, tile_indices)):
                if key in self.results:
                    continue
                pending = self.pending.get(key)
                if pending is not None and pending[1] <= priority:
                    continue
                task = {
                    "key": key,
                    "generation": generation,
                    "metadata": self.metadata,
                    "render_scale": render_scale,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "variant": variant,
                }
                self._queue_task(task, priority)
            return keys

    def get(self, key):
        with self.lock:
            result = self.results.get(key)
            if result is not None:
                self.results.move_to_end(key)
            return result

    def _cache_result(self, key, result):
        previous = self.results.pop(key, None)
        if previous is not None:
            self.cache_bytes -= previous.get("memory_bytes", 0)
        self.results[key] = result
        self.cache_bytes += result.get("memory_bytes", 0)

        protected = set(self.active)
        if self.coarse_key is not None:
            protected.add(self.coarse_key)

        low_res_bytes = 0
        for cached_key, cached_result in reversed(self.results.items()):
            _generation, scale, _tile_x, _tile_y, _variant = cached_key
            if scale > self.low_res_max_scale:
                continue
            size = cached_result.get("memory_bytes", 0)
            if low_res_bytes + size > self.low_res_cache_limit:
                continue
            protected.add(cached_key)
            low_res_bytes += size

        while self.cache_bytes > self.cache_limit:
            victim = next(
                (cached_key for cached_key in self.results
                 if cached_key not in protected),
                None,
            )
            if victim is None:
                break
            removed = self.results.pop(victim)
            self.cache_bytes -= removed.get("memory_bytes", 0)

    def fallback(self, render_scale, variant, viewport_bounds):
        generation = self.generation
        variant = tuple(variant)
        left, top, right, bottom = viewport_bounds
        with self.lock:
            coarse_results = []
            candidates = {}
            for key, result in self.results.items():
                key_generation, key_scale, _tile_x, _tile_y, key_variant = key
                if (key_generation != generation or
                        key_scale == render_scale or
                        key_variant != variant or
                        "error" in result):
                    continue
                x, y, width, height = result["bounds"]
                if (x >= right or x + width <= left or
                        y >= bottom or y + height <= top):
                    continue
                if result.get("coarse"):
                    coarse_results.append(result)
                else:
                    candidates.setdefault(key_scale, []).append(result)
            return select_fallback_results(
                coarse_results, candidates, render_scale
            )

    def _worker(self):
        renderer = self.renderer_factory()
        while True:
            _priority, _sequence, task = self.queue.get()
            key = task["key"]
            with self.lock:
                pending = self.pending.get(key)
                if (pending is None or pending[0] != task["token"]):
                    continue
                if (task["generation"] != self.generation or
                        (not task.get("pinned") and key not in self.active)):
                    self.pending.pop(key, None)
                    continue
            try:
                result = self.render_task(renderer, task)
                if task.get("coarse"):
                    result["coarse"] = True
            except Exception as exc:
                import traceback
                traceback.print_exc()
                result = {"error": str(exc)}

            with self.lock:
                self.pending.pop(key, None)
                if (task["generation"] != self.generation or
                        (not task.get("pinned") and key not in self.active)):
                    continue
                self._cache_result(key, result)
            if self.on_result is not None:
                self.on_result()
