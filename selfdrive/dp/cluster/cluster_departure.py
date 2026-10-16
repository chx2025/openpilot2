from __future__ import annotations

import math


# Matches sunnypilot's lead-departure distance and timing thresholds.
DEPARTURE_CLOSE_LEAD_M = 8.0
DEPARTURE_ARM_SECONDS = 1.0
DEPARTURE_DISTANCE_M = 1.0
DEPARTURE_MIN_RELATIVE_SPEED = 0.1
DEPARTURE_CONFIRM_SECONDS = 0.3
DEPARTURE_DISPLAY_SECONDS = 3.0
DEPARTURE_MAX_SAMPLE_GAP_SECONDS = 0.5


class DepartureReminder:
    def __init__(self) -> None:
        self._candidate_since: float | None = None
        self._last_sample_time: float | None = None
        self._arm_since: float | None = None
        self._baseline_distance: float | None = None
        self._visible_until = 0.0
        self._consumed = False

    def update(
        self,
        now: float,
        *,
        allowed: bool,
        valid: bool,
        sample_time: float,
        lead_present: bool,
        lead_distance: float | None,
        lead_relative_speed: float | None,
    ) -> bool:
        if not allowed:
            self._candidate_since = None
            self._last_sample_time = None
            self._arm_since = None
            self._baseline_distance = None
            self._visible_until = 0.0
            self._consumed = False
            return False

        fresh = (
            valid
            and math.isfinite(sample_time)
            and 0.0 <= now - sample_time <= DEPARTURE_MAX_SAMPLE_GAP_SECONDS
            and lead_present
            and lead_distance is not None
            and math.isfinite(lead_distance)
            and lead_distance > 0.0
            and lead_relative_speed is not None
            and math.isfinite(lead_relative_speed)
        )
        if not fresh:
            self._candidate_since = None
            self._arm_since = None
            self._baseline_distance = None
            self._last_sample_time = None
            self._visible_until = 0.0
            return False

        if self._last_sample_time is None or sample_time != self._last_sample_time:
            if self._last_sample_time is not None and not 0.0 < sample_time - self._last_sample_time <= DEPARTURE_MAX_SAMPLE_GAP_SECONDS:
                self._candidate_since = None
                self._arm_since = None
                self._baseline_distance = None
                self._visible_until = 0.0
            self._last_sample_time = sample_time
            if not self._consumed:
                assert lead_distance is not None and lead_relative_speed is not None
                if self._baseline_distance is None:
                    if lead_distance >= DEPARTURE_CLOSE_LEAD_M or lead_relative_speed > DEPARTURE_MIN_RELATIVE_SPEED:
                        self._arm_since = None
                    elif self._arm_since is None:
                        self._arm_since = sample_time
                    elif sample_time - self._arm_since >= DEPARTURE_ARM_SECONDS:
                        self._baseline_distance = lead_distance
                else:
                    self._baseline_distance = min(self._baseline_distance, lead_distance)
                    departing = (
                        lead_distance - self._baseline_distance > DEPARTURE_DISTANCE_M
                        and lead_relative_speed > DEPARTURE_MIN_RELATIVE_SPEED
                    )
                    if not departing:
                        self._candidate_since = None
                    elif self._candidate_since is None:
                        self._candidate_since = sample_time
                    elif sample_time - self._candidate_since > DEPARTURE_CONFIRM_SECONDS:
                        self._consumed = True
                        self._visible_until = now + DEPARTURE_DISPLAY_SECONDS
        return now < self._visible_until
