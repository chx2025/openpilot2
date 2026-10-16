from __future__ import annotations


class AdaptiveFpsController:
    def __init__(
        self,
        maximum_fps: float,
        minimum_fps: float = 5.0,
        decrease_step: float = 5.0,
        increase_step: float = 1.0,
        recovery_delay_s: float = 10.0,
        decrease_cooldown_s: float = 2.0,
        recovery_interval_s: float = 5.0,
    ) -> None:
        self.minimum_fps = max(0.0, minimum_fps)
        self.decrease_step = max(0.0, decrease_step)
        self.increase_step = max(0.0, increase_step)
        self.recovery_delay_s = max(0.0, recovery_delay_s)
        self.decrease_cooldown_s = max(0.0, decrease_cooldown_s)
        self.recovery_interval_s = max(0.0, recovery_interval_s)
        self.maximum_fps = 0.0
        self.current_fps = 0.0
        self._last_drop_at: float | None = None
        self._last_change_at: float | None = None
        self.set_maximum(maximum_fps)

    def set_maximum(self, maximum_fps: float) -> None:
        self.maximum_fps = max(0.0, maximum_fps)
        if self.maximum_fps == 0.0:
            self.current_fps = 0.0
        elif self.current_fps == 0.0:
            self.current_fps = self.maximum_fps
        else:
            self.current_fps = min(self.current_fps, self.maximum_fps)

    def update(self, *, dropped: bool, now: float) -> bool:
        if self.maximum_fps <= 0.0:
            return False

        if dropped:
            self._last_drop_at = now
            if (
                self._last_change_at is None
                or now - self._last_change_at >= self.decrease_cooldown_s
            ):
                next_fps = max(self.minimum_fps, self.current_fps - self.decrease_step)
                if next_fps < self.current_fps:
                    self.current_fps = next_fps
                    self._last_change_at = now
                    return True
            return False

        if self._last_drop_at is None or now - self._last_drop_at < self.recovery_delay_s:
            return False
        if (
            self._last_change_at is not None
            and now - self._last_change_at < self.recovery_interval_s
        ):
            return False

        next_fps = min(self.maximum_fps, self.current_fps + self.increase_step)
        if next_fps > self.current_fps:
            self.current_fps = next_fps
            self._last_change_at = now
            return True
        return False
