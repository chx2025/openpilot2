from __future__ import annotations

from dataclasses import replace
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

from cluster_config import (
    BLUE,
    DEFAULT_LANE_WIDTH_M,
    MAX_SPEED_KPH,
    SHOW_PLOT_MODE_PARAM,
    read_float_param,
    read_int_param,
    vehicle_diagnostic_log_enabled,
)
from cluster_departure import DEPARTURE_MAX_SAMPLE_GAP_SECONDS, DepartureReminder
from cluster_models import (
    ClusterUiState,
    DebugPlotSnapshot,
    DetectedVehicle,
    LaneMarking,
    LiveDebugInfo,
    ModelPathPoint,
    RadarPoint,
)
from cluster_route_replay import PRIUS_TSS2_GEAR_ADDRESS, PRIUS_TSS2_PLATFORM, RouteLogParser, finite_float, frame_to_state, safe_get, safe_optional_float
from cluster_scene import (
    FRONT_VEHICLE_LANE_RANGE_LANES,
    LANE_RANGE_EXIT_HYSTERESIS_LANES,
    radar_point_is_vehicle_candidate,
    vehicle_lane_offset_from_lateral,
)
from cluster_utils import clamp


from cluster_paths import REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIVE_SERVICES_BASE = (
    "carState",
    "carParams",
    "modelV2",
    "radarState",
    "radarTracks",
    "longitudinalPlan",
    "longitudinalPlanSP",
    #"lateralPlan",
    "controlsState",
    "selfdriveState",
    "carControl",
    "deviceState",
    "pandaStates",
    "cameraOdometry",
    "drivingModelData",
    "lateralDelay",
    "vehicleParameters",
    "lateralTorqueParameters",
    "wideRoadCameraState",
)
# sunnypilot renamed these services; the old names are kept for older cereal trees.
LIVE_SERVICE_LEGACY_NAMES = {
    "radarTracks": "liveTracks",
    "lateralDelay": "liveDelay",
    "vehicleParameters": "liveParameters",
    "lateralTorqueParameters": "liveTorqueParameters",
}


def resolve_live_services(services: tuple[str, ...], service_list: Any) -> list[str]:
    resolved: list[str] = []
    for service in services:
        if service_list is None or service in service_list:
            resolved.append(service)
            continue
        legacy = LIVE_SERVICE_LEGACY_NAMES.get(service)
        if legacy is not None and legacy in service_list:
            resolved.append(legacy)
        else:
            print(f"[cluster_live] service {service} not available; skipping", flush=True)
    return resolved


def torque_params_valid(torque_params: Any) -> Any | None:
    # sunnypilot renamed liveTorqueParameters.liveValid to lateralTorqueParameters.valid.
    value = safe_get(torque_params, "liveValid")
    return value if value is not None else safe_get(torque_params, "valid")
LIVE_CAN_SERVICES = ("can", "sendcan")
LIVE_DATA_STALE_SECONDS = 2.0
LIVE_SCENE_SMOOTHING_TAU_SECONDS = 0.16
LIVE_VEHICLE_HOLD_SECONDS = 0.30
LIVE_VEHICLE_FADE_SECONDS = 0.30
LIVE_VEHICLE_FADE_MIN_PROBABILITY = 0.8001
RADAR_STATE_TARGET2_STABLE_SECONDS = 0.25
RADAR_POINT_STABLE_SECONDS = 0.10
MODEL_LEAD_STABLE_SECONDS = 0.18
# radarPoint has no upstream fusion/smoothing like detected_vehicles, so a point
# that just cleared the stability filter can still vanish on the very next liveTracks
# sample. Hold it briefly and fade it out instead of dropping it instantly.
RADAR_POINT_HOLD_SECONDS = 0.15
RADAR_POINT_FADE_SECONDS = 0.15
RADAR_POINT_FADE_MIN_PROBABILITY = 0.40
# The fixed 3-lane display range boundary (FRONT_VEHICLE_LANE_RANGE_LANES /
# LANE_RANGE_EXIT_HYSTERESIS_LANES, both in cluster_scene.py) is a hard
# cutoff recomputed every frame from the live lane-offset estimate. An object
# hovering right at that boundary (e.g. from road-curvature noise) flickers
# in/out every frame with no hysteresis, so a wider "stay visible" margin is
# applied here than the "become visible" one so an object already on screen
# has to clearly leave the display range before it drops.
VEHICLE_FILTER_LOG_PATH = "/data/media/0/cluster_vehicle_objects.jsonl"
VEHICLE_FILTER_LOG_VERSION = 5
# Shared placeholder used when diagnostic logging is off. The stability filters
# still track a payload slot per key so the expiry paths can index it, but the
# rounded dict is never built. Never mutate it; callers only ** it into records.
EMPTY_FILTER_PAYLOAD: dict[str, object] = {}


class OpenpilotLiveSource:
    def __init__(self, include_can: bool = True, timeout_ms: int = 0) -> None:
        try:
            try:
                import openpilot.cereal.messaging as messaging
            except ImportError:
                import cereal.messaging as messaging
        except Exception as exc:
            raise RuntimeError(
                "Openpilot live input requires cereal.messaging. Run from an openpilot environment "
                "or use --input route/random for local checks."
            ) from exc

        self.messaging: Any = messaging
        try:
            try:
                from openpilot.cereal import log
            except ImportError:
                from cereal import log

            self.log: Any | None = log
        except Exception:
            self.log = None
        try:
            try:
                from openpilot.cereal.services import SERVICE_LIST
            except ImportError:
                from cereal.services import SERVICE_LIST
        except Exception:
            SERVICE_LIST = None
        self.services = resolve_live_services(
            LIVE_SERVICES_BASE + (LIVE_CAN_SERVICES if include_can else ()),
            SERVICE_LIST,
        )
        self._service_aliases = {legacy: new for new, legacy in LIVE_SERVICE_LEGACY_NAMES.items()}
        self.sm = messaging.SubMaster(self.services)
        self._gear_can_sock = None
        self.parser = RouteLogParser()
        self.timeout_ms = max(0, int(timeout_ms))
        self.last_state: ClusterUiState | None = None
        self._last_car_state_update_t: float | None = None
        # Mirrors ui_state.py's ignition tracking: read directly from pandaStates
        # rather than only trusting deviceState.started, so cluster's offroad dim
        # matches the same onroad/offroad signal the main openpilot UI uses.
        self._ignition = False
        self._panda_type_known = False
        self._smoothed_state: ClusterUiState | None = None
        self._smoothed_state_t: float | None = None
        self._vehicle_missing_since: dict[tuple[str, str], float] = {}
        self._vehicle_seen_since: dict[tuple[str, str], float] = {}
        self._stable_vehicle_keys: set[tuple[str, str]] = set()
        self._vehicle_filter_last_payload: dict[tuple[str, str], dict[str, object]] = {}
        self._vehicle_filter_sample_count: dict[tuple[str, str], int] = {}
        self._last_target2_filter_update_t = -999.0
        self._model_vehicle_seen_since: dict[tuple[str, str], float] = {}
        self._stable_model_vehicle_keys: set[tuple[str, str]] = set()
        self._model_vehicle_filter_last_payload: dict[tuple[str, str], dict[str, object]] = {}
        self._model_vehicle_filter_sample_count: dict[tuple[str, str], int] = {}
        self._last_model_filter_update_t = -999.0
        self._radar_seen_since: dict[tuple[str, str], float] = {}
        self._stable_radar_keys: set[tuple[str, str]] = set()
        self._radar_filter_last_payload: dict[tuple[str, str], dict[str, object]] = {}
        self._radar_filter_sample_count: dict[tuple[str, str], int] = {}
        self._last_radar_filter_update_t = -999.0
        self._radar_missing_since: dict[tuple[str, str], float] = {}
        self._lane_range_visible: dict[tuple[str, str], bool] = {}
        self._filter_log_file = None
        self._filter_log_disabled = not vehicle_diagnostic_log_enabled()
        self.start_t = time.monotonic()
        self.frames = 0
        self.params: Any | None = None
        self.params_memory: Any | None = None
        self._next_debug_param_read_t = 0.0
        self._custom_steer_ratio: float | None = None
        self._steer_actuator_delay_param_s: float | None = None
        self._cached_live_debug: LiveDebugInfo | None = None
        self._show_plot_mode = 0
        self._hud_debug_mode = 0
        self._live_debug_enabled = False
        self._debug_plot_enabled = False
        self._standby_state = standby_state()
        self._smoothed_ambient_brightness: float | None = None
        self._departure_reminder = DepartureReminder()
        self.profile_enabled = False
        self._profile_samples: list[tuple[str, float]] = []
        try:
            from openpilot.common.params import Params

            self.params = Params()
            self.params_memory = Params("/dev/shm/params")
        except Exception:
            pass

    def set_profile_enabled(self, enabled: bool) -> None:
        self.profile_enabled = enabled

    def set_debug_panels_enabled(self, *, live_debug: bool, debug_plot: bool) -> None:
        if (
            live_debug != self._live_debug_enabled
            or debug_plot != self._debug_plot_enabled
        ):
            self._next_debug_param_read_t = 0.0
        self._live_debug_enabled = live_debug
        self._debug_plot_enabled = debug_plot

    def set_hud_debug_mode(self, mode: int) -> None:
        try:
            next_mode = int(mode)
        except (TypeError, ValueError):
            next_mode = 0
        next_mode = max(0, min(3, next_mode))
        if next_mode != self._hud_debug_mode:
            self._next_debug_param_read_t = 0.0
        self._hud_debug_mode = next_mode

    def profile_samples(self) -> tuple[tuple[str, float], ...]:
        samples = tuple(self._profile_samples)
        self._profile_samples.clear()
        return samples

    def _profile_start(self) -> float:
        return time.perf_counter() if self.profile_enabled else 0.0

    def _profile_add(self, name: str, start_time: float) -> None:
        if self.profile_enabled:
            self._profile_samples.append((name, (time.perf_counter() - start_time) * 1000.0))

    def update(self) -> ClusterUiState:
        profile_stage = self._profile_start()
        self.sm.update(self.timeout_ms)
        self._profile_add("source.live.submaster_update", profile_stage)

        self._update_ignition()

        profile_stage = self._profile_start()
        self._update_current_speed()
        self._profile_add("source.live.current_speed", profile_stage)

        profile_stage = self._profile_start()
        for service in self.services:
            if not self._service_updated(service):
                continue
            event_t = self._service_time(service)
            service_stage = self._profile_start()
            self._apply_service_update(service, event_t)
            self._profile_add(f"source.live.apply.{service}", service_stage)
            if service == "carState":
                self._last_car_state_update_t = time.monotonic()
        self._profile_add("source.live.apply_updates", profile_stage)

        if self._gear_can_sock is not None:
            for event in self.messaging.drain_sock(self._gear_can_sock):
                event_t = float(event.logMonoTime) / 1_000_000_000.0
                for can_message in event.can:
                    if can_message.address == PRIUS_TSS2_GEAR_ADDRESS:
                        self.parser._update_prius_gear(can_message, event_t)

        if self._service_alive("carState"):
            profile_stage = self._profile_start()
            event_t = self._service_time("carState")
            frame = self.parser._frame_from_car_state(self.sm["carState"], event_t)
            self._profile_add("source.live.car_frame", profile_stage)

            profile_stage = self._profile_start()
            state = frame_to_state(frame)
            if not self._service_alive("selfdriveState") or not self._service_valid("selfdriveState"):
                state = replace(state, wheel_critical=False)
            self._profile_add("source.live.frame_to_state", profile_stage)

            profile_stage = self._profile_start()
            state = self._smooth_scene_state(state)
            state = replace(state, departure_reminder=self._update_departure_reminder(state))
            self._profile_add("source.live.smooth_scene", profile_stage)
            self.last_state = self._with_debug_state(state)
            self.frames += 1
            return self.last_state

        profile_stage = self._profile_start()
        experimental_mode = self.parser.experimental_mode
        if self.params is not None:
            experimental_mode = self.params.get_bool("ExperimentalMode")
        state = replace(self._standby_state, experimental_mode=experimental_mode)
        self._departure_reminder.update(time.monotonic(), allowed=False, valid=False, sample_time=0.0,
                                        lead_present=False, lead_distance=None, lead_relative_speed=None)
        self._profile_add("source.live.standby_state", profile_stage)

        self._reset_stability_filters(time.monotonic())
        self.last_state = self._with_debug_state(state)
        return self.last_state

    def _update_departure_reminder(self, state: ClusterUiState) -> bool:
        now = time.monotonic()
        if self.vehicle_started() is not True:
            return self._departure_reminder.update(now, allowed=False, valid=False, sample_time=0.0,
                                                    lead_present=False, lead_distance=None, lead_relative_speed=None)
        services = ("carState", "selfdriveState", "radarState")
        valid = all(
            self._service_alive(service)
            and self.sm.valid.get(service, False)
            and 0.0 < self.sm.logMonoTime.get(service, 0) / 1e9 <= now
            and now - self.sm.logMonoTime[service] / 1e9 <= DEPARTURE_MAX_SAMPLE_GAP_SECONDS
            for service in services
        )
        if not valid:
            return self._departure_reminder.update(now, allowed=True, valid=False, sample_time=0.0,
                                                    lead_present=False, lead_distance=None, lead_relative_speed=None)

        car = self.sm["carState"]
        speed = safe_optional_float(car, "vEgo")
        allowed = (
            state.gear_text in ("D", "B")
            and speed is not None
            and abs(speed) < 0.1
            and not self.sm["selfdriveState"].enabled
            and not car.cruiseState.enabled
            and not car.gasPressed
        )
        lead = self.sm["radarState"].leadOne
        return self._departure_reminder.update(
            now,
            allowed=allowed,
            valid=not self.sm["radarState"].radarErrors,
            sample_time=self.sm.logMonoTime["radarState"] / 1e9,
            lead_present=lead.present,
            lead_distance=safe_optional_float(lead, "dRel"),
            lead_relative_speed=safe_optional_float(lead, "vRel"),
        )

    def live_data_available(self) -> bool:
        last_update_t = self._last_car_state_update_t
        return last_update_t is not None and time.monotonic() - last_update_t <= LIVE_DATA_STALE_SECONDS

    def _update_ignition(self) -> None:
        """Track panda ignition the same way openpilot's own ui_state.py does.

        This is read directly from pandaStates (not derived from carState/CAN
        traffic, which can keep flowing for a while after key-off on some
        vehicles), so it reflects hardware ignition immediately regardless of
        whether thermald's deviceState.started has caught up yet.
        """
        if self._service_updated("pandaStates"):
            panda_states = self.sm["pandaStates"]
            if len(panda_states) > 0:
                panda_type = panda_states[0].pandaType
                unknown_type = self.log is not None and panda_type == self.log.PandaState.PandaType.unknown
                self._panda_type_known = not unknown_type
                if self._panda_type_known:
                    self._ignition = any(state.ignitionLine or state.ignitionCan for state in panda_states)
        elif not self._service_alive("pandaStates"):
            self._panda_type_known = False

    def vehicle_started(self) -> bool | None:
        """Return the current onroad state, or None until it can be determined.

        Mirrors ui_state.py's `deviceState.started and ignition`: the main
        openpilot UI switches between its onroad and offroad screens using
        that exact combination, not deviceState.started alone. Trusting only
        deviceState.started let a stale/delayed thermald update (or the
        selfdriveState fallback below, whose service is dead offroad) leave
        vehicle_started() reporting True/None after the car had actually gone
        offroad, so the cluster screen never dimmed. ANDing in a directly-read
        ignition signal from pandaStates keeps this in sync with the same
        onroad/offroad transition the main UI uses.
        """
        device_started: bool | None = None
        if self._service_alive("deviceState") and self._service_valid("deviceState"):
            value = safe_get(self.sm["deviceState"], "started")
            if value is not None:
                device_started = bool(value)
        if device_started is None:
            if not self._service_alive("selfdriveState") or not self._service_valid("selfdriveState"):
                return None
            value = safe_get(self.sm["selfdriveState"], "started")
            device_started = bool(value) if value is not None else None
        if device_started is None:
            return None
        if not device_started:
            return False
        if self._service_alive("pandaStates") and self._panda_type_known:
            return self._ignition
        # No usable ignition signal yet (e.g. right at boot, before the first
        # pandaStates message); fall back to trusting deviceState alone.
        return device_started

    def _smooth_scene_state(self, state: ClusterUiState) -> ClusterUiState:
        now = time.monotonic()
        target2_keys = {
            (vehicle.label, vehicle.source)
            for vehicle in state.detected_vehicles
            if vehicle.label == "TARGET2" and vehicle.source == "radarState"
        }
        target2_update_t = self.parser.radar_detection_t
        if target2_update_t != self._last_target2_filter_update_t:
            self._last_target2_filter_update_t = target2_update_t
            target2_by_key = {
                (vehicle.label, vehicle.source): vehicle
                for vehicle in state.detected_vehicles
                if vehicle.label == "TARGET2" and vehicle.source == "radarState"
            }
            for key, vehicle in target2_by_key.items():
                payload = (
                    EMPTY_FILTER_PAYLOAD
                    if self._filter_log_disabled
                    else self._vehicle_filter_payload(vehicle)
                )
                self._vehicle_filter_last_payload[key] = payload
                if key not in self._vehicle_seen_since:
                    self._vehicle_seen_since[key] = target2_update_t
                    self._vehicle_filter_sample_count[key] = 1
                    self._write_filter_event(
                        "filter_pending",
                        now,
                        RADAR_STATE_TARGET2_STABLE_SECONDS,
                        {**payload, "sample_count": 1},
                    )
                else:
                    self._vehicle_filter_sample_count[key] += 1
                candidate_duration_s = target2_update_t - self._vehicle_seen_since[key]
                if (
                    self._vehicle_filter_sample_count[key] >= 2
                    and candidate_duration_s >= RADAR_STATE_TARGET2_STABLE_SECONDS
                    and key not in self._stable_vehicle_keys
                ):
                    self._stable_vehicle_keys.add(key)
                    self._write_filter_event(
                        "filter_passed",
                        now,
                        RADAR_STATE_TARGET2_STABLE_SECONDS,
                        {**payload, "sample_count": self._vehicle_filter_sample_count[key]},
                        candidate_duration_s,
                    )
            for key in tuple(self._vehicle_seen_since):
                if key in target2_keys:
                    continue
                event = "filter_expired" if key in self._stable_vehicle_keys else "filtered"
                self._write_filter_event(
                    event,
                    now,
                    RADAR_STATE_TARGET2_STABLE_SECONDS,
                    {
                        **self._vehicle_filter_last_payload[key],
                        "sample_count": self._vehicle_filter_sample_count[key],
                    },
                    max(0.0, target2_update_t - self._vehicle_seen_since[key]),
                )
                self._vehicle_seen_since.pop(key, None)
                self._vehicle_filter_last_payload.pop(key, None)
                self._vehicle_filter_sample_count.pop(key, None)
                self._stable_vehicle_keys.discard(key)

        current_radar_keys = {
            (point.label, point.source) for point in state.radar_points
        }
        radar_update_t = self.parser.live_track_radar_t
        radar_snapshot_changed = radar_update_t != self._last_radar_filter_update_t
        radar_snapshot_expired = not current_radar_keys and bool(self._radar_seen_since)
        if radar_snapshot_changed or radar_snapshot_expired:
            self._last_radar_filter_update_t = radar_update_t
            radar_by_key = {
                (point.label, point.source): point for point in state.radar_points
            }
            for key, point in radar_by_key.items():
                payload = (
                    EMPTY_FILTER_PAYLOAD
                    if self._filter_log_disabled
                    else self._radar_filter_payload(point, state)
                )
                self._radar_filter_last_payload[key] = payload
                if key not in self._radar_seen_since:
                    self._radar_seen_since[key] = radar_update_t
                    self._radar_filter_sample_count[key] = 1
                    self._write_filter_event(
                        "filter_pending",
                        now,
                        RADAR_POINT_STABLE_SECONDS,
                        {**payload, "sample_count": 1},
                    )
                else:
                    self._radar_filter_sample_count[key] += 1
                candidate_duration_s = radar_update_t - self._radar_seen_since[key]
                if (
                    self._radar_filter_sample_count[key] >= 2
                    and candidate_duration_s >= RADAR_POINT_STABLE_SECONDS
                    and key not in self._stable_radar_keys
                ):
                    self._stable_radar_keys.add(key)
                    self._write_filter_event(
                        "filter_passed",
                        now,
                        RADAR_POINT_STABLE_SECONDS,
                        {**payload, "sample_count": self._radar_filter_sample_count[key]},
                        candidate_duration_s,
                    )
            for key in tuple(self._radar_seen_since):
                if key in current_radar_keys:
                    continue
                event = "filter_expired" if key in self._stable_radar_keys else "filtered"
                self._write_filter_event(
                    event,
                    now,
                    RADAR_POINT_STABLE_SECONDS,
                    {
                        **self._radar_filter_last_payload[key],
                        "sample_count": self._radar_filter_sample_count[key],
                    },
                    max(0.0, radar_update_t - self._radar_seen_since[key]),
                )
                self._radar_seen_since.pop(key, None)
                self._radar_filter_last_payload.pop(key, None)
                self._radar_filter_sample_count.pop(key, None)
                self._stable_radar_keys.discard(key)

        state = replace(
            state,
            detected_vehicles=tuple(
                vehicle
                for vehicle in state.detected_vehicles
                if (
                    vehicle.label != "TARGET2"
                    or vehicle.source != "radarState"
                    or (vehicle.label, vehicle.source) in self._stable_vehicle_keys
                )
            ),
            radar_points=tuple(
                point
                for point in state.radar_points
                if (point.label, point.source) in self._stable_radar_keys
            ),
        )
        unsupported_model_by_key: dict[tuple[str, str], DetectedVehicle] = {}
        unsupported_model_keys: set[tuple[str, str]] = set()
        for vehicle in state.detected_vehicles:
            if not vehicle.source.startswith("modelV2"):
                continue
            if self._model_vehicle_supported_by_radar(vehicle, state.radar_points):
                continue
            key = (vehicle.label, vehicle.source)
            unsupported_model_by_key[key] = vehicle
            unsupported_model_keys.add(key)

        model_update_t = self.parser.model_detection_t
        if model_update_t != self._last_model_filter_update_t:
            self._last_model_filter_update_t = model_update_t
            for key, vehicle in unsupported_model_by_key.items():
                payload = (
                    EMPTY_FILTER_PAYLOAD
                    if self._filter_log_disabled
                    else self._vehicle_filter_payload(vehicle)
                )
                self._model_vehicle_filter_last_payload[key] = payload
                if key not in self._model_vehicle_seen_since:
                    self._model_vehicle_seen_since[key] = model_update_t
                    self._model_vehicle_filter_sample_count[key] = 1
                    self._write_filter_event(
                        "filter_pending",
                        now,
                        MODEL_LEAD_STABLE_SECONDS,
                        {**payload, "sample_count": 1},
                    )
                else:
                    self._model_vehicle_filter_sample_count[key] += 1
                candidate_duration_s = model_update_t - self._model_vehicle_seen_since[key]
                if (
                    self._model_vehicle_filter_sample_count[key] >= 2
                    and candidate_duration_s >= MODEL_LEAD_STABLE_SECONDS
                    and key not in self._stable_model_vehicle_keys
                ):
                    self._stable_model_vehicle_keys.add(key)
                    self._write_filter_event(
                        "filter_passed",
                        now,
                        MODEL_LEAD_STABLE_SECONDS,
                        {
                            **payload,
                            "sample_count": self._model_vehicle_filter_sample_count[key],
                        },
                        candidate_duration_s,
                    )
            for key in tuple(self._model_vehicle_seen_since):
                if key in unsupported_model_keys:
                    continue
                event = "filter_expired" if key in self._stable_model_vehicle_keys else "filtered"
                self._write_filter_event(
                    event,
                    now,
                    MODEL_LEAD_STABLE_SECONDS,
                    {
                        **self._model_vehicle_filter_last_payload[key],
                        "sample_count": self._model_vehicle_filter_sample_count[key],
                    },
                    max(0.0, model_update_t - self._model_vehicle_seen_since[key]),
                )
                self._model_vehicle_seen_since.pop(key, None)
                self._model_vehicle_filter_last_payload.pop(key, None)
                self._model_vehicle_filter_sample_count.pop(key, None)
                self._stable_model_vehicle_keys.discard(key)

        state = replace(
            state,
            detected_vehicles=tuple(
                vehicle
                for vehicle in state.detected_vehicles
                if (
                    not vehicle.source.startswith("modelV2")
                    or (vehicle.label, vehicle.source) not in unsupported_model_keys
                    or (vehicle.label, vehicle.source) in self._stable_model_vehicle_keys
                )
            ),
        )

        # Stamp diagnostic-only stability_gate/render_phase fields for the
        # vehicle object log. These do not affect rendering, only what gets
        # written to the audit trail so flicker can be traced back to its
        # source: a stability filter delay vs. the hold/fade below vs. a
        # scene-composition step dropping the object outright. vehicle_candidate
        # (see _apply_lane_range_hysteresis()) is intentionally NOT stamped
        # here yet: radar_point_is_vehicle_candidate() also depends on
        # state.detected_vehicles, and at this point in the function that
        # still includes not-yet-stability-filtered TARGET2/model candidates
        # and excludes this frame's hold/fade-retained objects, so evaluating
        # it here could disagree with what build_cluster_scene() actually
        # sees. It gets stamped later against the final smoothed state instead.
        state = replace(
            state,
            detected_vehicles=tuple(
                replace(
                    vehicle,
                    stability_gate=(
                        "delayed"
                        if (vehicle.label, vehicle.source) in unsupported_model_keys
                        or (vehicle.label == "TARGET2" and vehicle.source == "radarState")
                        else "immediate"
                    ),
                    render_phase="active",
                )
                for vehicle in state.detected_vehicles
            ),
            radar_points=tuple(
                replace(
                    point,
                    stability_gate="delayed",
                    render_phase="active",
                )
                for point in state.radar_points
            ),
        )
        previous = self._smoothed_state
        previous_t = self._smoothed_state_t
        if previous is None or previous_t is None:
            tagged_vehicles, tagged_radar_points = self._apply_lane_range_hysteresis(
                state.detected_vehicles, state.radar_points, state
            )
            state = replace(state, detected_vehicles=tagged_vehicles, radar_points=tagged_radar_points)
            self._smoothed_state = state
            self._smoothed_state_t = now
            return state

        alpha = 1.0 - math.exp(
            -max(0.001, now - previous_t) / LIVE_SCENE_SMOOTHING_TAU_SECONDS
        )
        previous_vehicles = {
            (vehicle.label, vehicle.source): vehicle for vehicle in previous.detected_vehicles
        }
        current_vehicle_keys = {(vehicle.label, vehicle.source) for vehicle in state.detected_vehicles}
        for key in current_vehicle_keys:
            self._vehicle_missing_since.pop(key, None)
        smoothed_vehicles = tuple(
            self._smooth_vehicle(vehicle, previous_vehicles.get((vehicle.label, vehicle.source)), alpha)
            for vehicle in state.detected_vehicles
        )
        for vehicle in previous.detected_vehicles:
            key = (vehicle.label, vehicle.source)
            if key in current_vehicle_keys:
                continue
            missing_since = self._vehicle_missing_since.setdefault(key, now)
            missing_for = now - missing_since
            if missing_for > LIVE_VEHICLE_HOLD_SECONDS + LIVE_VEHICLE_FADE_SECONDS:
                continue
            if missing_for <= LIVE_VEHICLE_HOLD_SECONDS:
                smoothed_vehicles += (replace(vehicle, render_phase="held"),)
                continue
            fade = 1.0 - (
                (missing_for - LIVE_VEHICLE_HOLD_SECONDS) / LIVE_VEHICLE_FADE_SECONDS
            )
            faded_probability = LIVE_VEHICLE_FADE_MIN_PROBABILITY + (
                max(LIVE_VEHICLE_FADE_MIN_PROBABILITY, vehicle.probability)
                - LIVE_VEHICLE_FADE_MIN_PROBABILITY
            ) * clamp(fade, 0.0, 1.0)
            smoothed_vehicles += (replace(vehicle, probability=faded_probability, render_phase="fading"),)

        active_vehicle_keys = {
            (vehicle.label, vehicle.source)
            for vehicle in smoothed_vehicles
        }
        self._vehicle_missing_since = {
            key: missing_since
            for key, missing_since in self._vehicle_missing_since.items()
            if key in active_vehicle_keys
        }
        self._stable_vehicle_keys.intersection_update(active_vehicle_keys)
        self._vehicle_seen_since = {
            key: seen_since
            for key, seen_since in self._vehicle_seen_since.items()
            if key in active_vehicle_keys or key in target2_keys
        }
        self._vehicle_filter_last_payload = {
            key: payload
            for key, payload in self._vehicle_filter_last_payload.items()
            if key in active_vehicle_keys or key in target2_keys
        }
        self._vehicle_filter_sample_count = {
            key: sample_count
            for key, sample_count in self._vehicle_filter_sample_count.items()
            if key in active_vehicle_keys or key in target2_keys
        }
        self._stable_model_vehicle_keys.intersection_update(active_vehicle_keys)
        self._model_vehicle_seen_since = {
            key: seen_since
            for key, seen_since in self._model_vehicle_seen_since.items()
            if key in active_vehicle_keys or key in unsupported_model_keys
        }
        self._model_vehicle_filter_last_payload = {
            key: payload
            for key, payload in self._model_vehicle_filter_last_payload.items()
            if key in active_vehicle_keys or key in unsupported_model_keys
        }
        self._model_vehicle_filter_sample_count = {
            key: sample_count
            for key, sample_count in self._model_vehicle_filter_sample_count.items()
            if key in active_vehicle_keys or key in unsupported_model_keys
        }

        current_radar_active_keys = {
            (point.label, point.source) for point in state.radar_points
        }
        for key in current_radar_active_keys:
            self._radar_missing_since.pop(key, None)
        smoothed_radar_points = state.radar_points
        for point in previous.radar_points:
            key = (point.label, point.source)
            if key in current_radar_active_keys:
                continue
            if key in current_radar_keys:
                # The raw sensor already reports a new candidate under this key
                # (e.g. a reused liveTracks trackId). Let it requalify through the
                # stability filter instead of continuing to show the stale point.
                self._radar_missing_since.pop(key, None)
                continue
            missing_since = self._radar_missing_since.setdefault(key, now)
            missing_for = now - missing_since
            if missing_for > RADAR_POINT_HOLD_SECONDS + RADAR_POINT_FADE_SECONDS:
                continue
            if missing_for <= RADAR_POINT_HOLD_SECONDS:
                smoothed_radar_points += (replace(point, render_phase="held"),)
                continue
            fade = 1.0 - (
                (missing_for - RADAR_POINT_HOLD_SECONDS) / RADAR_POINT_FADE_SECONDS
            )
            base_probability = (
                point.probability if point.probability is not None else RADAR_POINT_FADE_MIN_PROBABILITY
            )
            faded_probability = RADAR_POINT_FADE_MIN_PROBABILITY + (
                max(RADAR_POINT_FADE_MIN_PROBABILITY, base_probability)
                - RADAR_POINT_FADE_MIN_PROBABILITY
            ) * clamp(fade, 0.0, 1.0)
            smoothed_radar_points += (replace(point, probability=faded_probability, render_phase="fading"),)

        active_radar_keys = {
            (point.label, point.source) for point in smoothed_radar_points
        }
        self._radar_missing_since = {
            key: missing_since
            for key, missing_since in self._radar_missing_since.items()
            if key in active_radar_keys
        }

        smoothed = replace(
            state,
            lanes=self._smooth_lanes(state.lanes, previous.lanes, alpha),
            model_path=self._smooth_model_path(state.model_path, previous.model_path, alpha),
            detected_vehicles=smoothed_vehicles,
            radar_points=smoothed_radar_points,
        )
        tagged_vehicles, tagged_radar_points = self._apply_lane_range_hysteresis(
            smoothed.detected_vehicles, smoothed.radar_points, smoothed
        )
        smoothed = replace(smoothed, detected_vehicles=tagged_vehicles, radar_points=tagged_radar_points)
        self._smoothed_state = smoothed
        self._smoothed_state_t = now
        return smoothed

    def _apply_lane_range_hysteresis(
        self,
        vehicles: tuple[DetectedVehicle, ...],
        radar_points: tuple[RadarPoint, ...],
        state: ClusterUiState,
    ) -> tuple[tuple[DetectedVehicle, ...], tuple[RadarPoint, ...]]:
        tagged_vehicles = tuple(
            replace(
                vehicle,
                in_display_lanes=self._lane_range_hysteresis_visible(
                    (vehicle.label, vehicle.source), vehicle.longitudinal_m, vehicle.lateral_m, state
                ),
            )
            for vehicle in vehicles
        )
        # vehicle_candidate is stamped here (against the final smoothed state,
        # which includes this frame's hold/fade-retained detected_vehicles/
        # radar_points) rather than earlier in _smooth_scene_state(), so this
        # diagnostic-only mirror of radar_point_is_vehicle_candidate() matches
        # what build_cluster_scene() will actually see as closely as possible.
        tagged_radar_points = tuple(
            replace(
                point,
                in_display_lanes=self._lane_range_hysteresis_visible(
                    (point.label, point.source), point.longitudinal_m, point.lateral_m, state
                ),
                vehicle_candidate=radar_point_is_vehicle_candidate(point, state, DEFAULT_LANE_WIDTH_M),
            )
            for point in radar_points
        )
        active_keys = {(vehicle.label, vehicle.source) for vehicle in tagged_vehicles}
        active_keys |= {(point.label, point.source) for point in tagged_radar_points}
        self._lane_range_visible = {
            key: visible for key, visible in self._lane_range_visible.items() if key in active_keys
        }
        return tagged_vehicles, tagged_radar_points

    def _lane_range_hysteresis_visible(
        self,
        key: tuple[str, str],
        longitudinal_m: float,
        lateral_m: float,
        state: ClusterUiState,
    ) -> bool:
        if longitudinal_m <= 0.0:
            self._lane_range_visible[key] = False
            return False
        offset = abs(vehicle_lane_offset_from_lateral(lateral_m, longitudinal_m, DEFAULT_LANE_WIDTH_M, state))
        was_visible = self._lane_range_visible.get(key, False)
        threshold = (
            FRONT_VEHICLE_LANE_RANGE_LANES + LANE_RANGE_EXIT_HYSTERESIS_LANES
            if was_visible
            else FRONT_VEHICLE_LANE_RANGE_LANES
        )
        visible = offset < threshold
        self._lane_range_visible[key] = visible
        return visible

    def _write_filter_event(
        self,
        event: str,
        now: float,
        threshold_s: float,
        payload: dict[str, object],
        candidate_duration_s: float | None = None,
    ) -> None:
        if self._filter_log_disabled:
            return
        record = {
            "event": event,
            "monotonic_s": round(now, 3),
            "log_version": VEHICLE_FILTER_LOG_VERSION,
            "record_type": "stability_filter",
            "filter_reason": "stability_delay",
            "stability_threshold_s": threshold_s,
            **payload,
        }
        if candidate_duration_s is not None:
            record["candidate_duration_s"] = round(candidate_duration_s, 3)
        try:
            if self._filter_log_file is None:
                path = Path(os.environ.get("CLUSTER_VEHICLE_LOG_PATH", VEHICLE_FILTER_LOG_PATH))
                path.parent.mkdir(parents=True, exist_ok=True)
                self._filter_log_file = path.open("a", encoding="utf-8", buffering=1)
            self._filter_log_file.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as exc:
            if self._filter_log_file is not None:
                self._filter_log_file.close()
                self._filter_log_file = None
            print(f"Cluster vehicle filter log disabled: {exc}", file=sys.stderr)
            self._filter_log_disabled = True

    def _reset_stability_filters(self, now: float) -> None:
        for key, seen_since in tuple(self._vehicle_seen_since.items()):
            event = "filter_expired" if key in self._stable_vehicle_keys else "filtered"
            self._write_filter_event(
                event,
                now,
                RADAR_STATE_TARGET2_STABLE_SECONDS,
                {
                    **self._vehicle_filter_last_payload[key],
                    "sample_count": self._vehicle_filter_sample_count[key],
                },
                max(0.0, self.parser.radar_detection_t - seen_since),
            )
        for key, seen_since in tuple(self._model_vehicle_seen_since.items()):
            event = "filter_expired" if key in self._stable_model_vehicle_keys else "filtered"
            self._write_filter_event(
                event,
                now,
                MODEL_LEAD_STABLE_SECONDS,
                {
                    **self._model_vehicle_filter_last_payload[key],
                    "sample_count": self._model_vehicle_filter_sample_count[key],
                },
                max(0.0, self.parser.model_detection_t - seen_since),
            )
        for key, seen_since in tuple(self._radar_seen_since.items()):
            event = "filter_expired" if key in self._stable_radar_keys else "filtered"
            self._write_filter_event(
                event,
                now,
                RADAR_POINT_STABLE_SECONDS,
                {
                    **self._radar_filter_last_payload[key],
                    "sample_count": self._radar_filter_sample_count[key],
                },
                max(0.0, self.parser.live_track_radar_t - seen_since),
            )
        self._vehicle_seen_since.clear()
        self._stable_vehicle_keys.clear()
        self._vehicle_filter_last_payload.clear()
        self._vehicle_filter_sample_count.clear()
        self._model_vehicle_seen_since.clear()
        self._stable_model_vehicle_keys.clear()
        self._model_vehicle_filter_last_payload.clear()
        self._model_vehicle_filter_sample_count.clear()
        self._radar_seen_since.clear()
        self._stable_radar_keys.clear()
        self._radar_filter_last_payload.clear()
        self._radar_filter_sample_count.clear()
        self._radar_missing_since.clear()
        self._lane_range_visible.clear()
        self._last_target2_filter_update_t = -999.0
        self._last_model_filter_update_t = -999.0
        self._last_radar_filter_update_t = -999.0

    @staticmethod
    def _vehicle_filter_payload(vehicle: DetectedVehicle) -> dict[str, object]:
        return {
            "label": vehicle.label,
            "source": vehicle.source,
            "source_base": vehicle.source.split("+radar:", 1)[0],
            "raw_probability": round(vehicle.probability, 3),
            "longitudinal_m": round(vehicle.longitudinal_m, 3),
            "lateral_m": round(vehicle.lateral_m, 3),
            "relative_speed_mps": (
                round(vehicle.relative_speed_mps, 3)
                if vehicle.relative_speed_mps is not None else None
            ),
            "absolute_speed_kph": (
                round(vehicle.absolute_speed_kph, 3)
                if vehicle.absolute_speed_kph is not None else None
            ),
        }

    @staticmethod
    def _radar_filter_payload(point: RadarPoint, state: ClusterUiState) -> dict[str, object]:
        return {
            "label": point.label,
            "source": "radarPoint",
            "source_base": "radarPoint",
            "sensor_source": point.source,
            "raw_probability": (
                round(point.probability, 3) if point.probability is not None else None
            ),
            "longitudinal_m": round(point.longitudinal_m, 3),
            "lateral_m": round(point.lateral_m, 3),
            "relative_speed_mps": (
                round(point.relative_speed_mps, 3)
                if point.relative_speed_mps is not None else None
            ),
            "absolute_speed_kph": (
                round(point.absolute_speed_kph, 3)
                if point.absolute_speed_kph is not None else None
            ),
            "valid": point.valid,
            "valid_count": point.valid_count,
            "in_my_lane": point.in_my_lane,
            "motion_consistent": point.motion_consistent,
            "promotion_held": point.promotion_held,
            # point.vehicle_candidate isn't stamped yet at this stage of
            # _smooth_scene_state() (it's set later, after the stability
            # filter loops below run), so recompute it directly here instead
            # of reading the not-yet-set field.
            "vehicle_candidate": radar_point_is_vehicle_candidate(point, state, DEFAULT_LANE_WIDTH_M),
        }

    @staticmethod
    def _model_vehicle_supported_by_radar(
        vehicle: DetectedVehicle,
        radar_points: tuple[RadarPoint, ...],
    ) -> bool:
        if not vehicle.source.startswith("modelV2"):
            return False
        if "+radar:" in vehicle.source:
            return True
        if vehicle.longitudinal_m <= 0.0:
            return False
        longitudinal_tolerance = max(3.5, min(10.0, vehicle.longitudinal_m * 0.20))
        for point in radar_points:
            if point.longitudinal_m <= 0.0:
                continue
            if (
                abs(point.longitudinal_m - vehicle.longitudinal_m) <= longitudinal_tolerance
                and abs(point.lateral_m - vehicle.lateral_m) <= 1.4
            ):
                return True
        return False

    @staticmethod
    def _smooth_lanes(
        current: tuple[LaneMarking, ...],
        previous: tuple[LaneMarking, ...],
        alpha: float,
    ) -> tuple[LaneMarking, ...]:
        if not current:
            return previous
        if len(current) != len(previous):
            return current
        return tuple(
            replace(
                marking,
                offset=previous_marking.offset + (marking.offset - previous_marking.offset) * alpha,
                model_points=OpenpilotLiveSource._smooth_model_path(
                    marking.model_points,
                    previous_marking.model_points,
                    alpha,
                ),
                model_lateral_shift_m=(
                    previous_marking.model_lateral_shift_m
                    + (marking.model_lateral_shift_m - previous_marking.model_lateral_shift_m) * alpha
                ),
            )
            for marking, previous_marking in zip(current, previous)
        )

    @staticmethod
    def _smooth_model_path(
        current: tuple[ModelPathPoint, ...],
        previous: tuple[ModelPathPoint, ...],
        alpha: float,
    ) -> tuple[ModelPathPoint, ...]:
        if len(current) != len(previous):
            return current
        return tuple(
            replace(
                point,
                forward_m=previous_point.forward_m + (point.forward_m - previous_point.forward_m) * alpha,
                lateral_m=previous_point.lateral_m + (point.lateral_m - previous_point.lateral_m) * alpha,
                lateral_std_m=OpenpilotLiveSource._smooth_optional(
                    point.lateral_std_m, previous_point.lateral_std_m, alpha
                ),
            )
            for point, previous_point in zip(current, previous)
        )

    @staticmethod
    def _smooth_optional(current: float | None, previous: float | None, alpha: float) -> float | None:
        if current is None or previous is None:
            return current
        return previous + (current - previous) * alpha

    @staticmethod
    def _smooth_vehicle(
        current: DetectedVehicle,
        previous: DetectedVehicle | None,
        alpha: float,
    ) -> DetectedVehicle:
        if previous is None:
            return current
        return replace(
            current,
            longitudinal_m=previous.longitudinal_m + (current.longitudinal_m - previous.longitudinal_m) * alpha,
            lateral_m=previous.lateral_m + (current.lateral_m - previous.lateral_m) * alpha,
            relative_speed_mps=OpenpilotLiveSource._smooth_optional(
                current.relative_speed_mps, previous.relative_speed_mps, alpha
            ),
            absolute_speed_kph=OpenpilotLiveSource._smooth_optional(
                current.absolute_speed_kph, previous.absolute_speed_kph, alpha
            ),
        )

    def status_text(self) -> str:
        profile_stage = self._profile_start()
        alive = sum(1 for service in self.services if self._service_alive(service))
        updated = sum(1 for service in self.services if self._service_updated(service))
        can_status = "can/sendcan" if "sendcan" in self.services else "no-can"
        age = time.monotonic() - self.start_t
        fps = self.frames / age if age > 0.1 else 0.0
        radar_count = len(self.last_state.radar_points) if self.last_state is not None else 0
        detected_count = len(self.last_state.detected_vehicles) if self.last_state is not None else 0
        text = (
            f"live {can_status} alive={alive}/{len(self.services)} upd={updated} state={fps:.1f}Hz "
            f"radar={radar_count} detected={detected_count}"
        )
        self._profile_add("source.live.status_text", profile_stage)
        return text

    def ambient_brightness_percent(self) -> int | None:
        if not self._service_alive("wideRoadCameraState"):
            return None
        try:
            camera_state = self.sm["wideRoadCameraState"]
            exposure = float(camera_state.exposureValPercent)
            scale = 6.0 if camera_state.sensor == "ar0231" else 1.0
            light_sensor = max(100.0 - scale * exposure, 0.0)
            if light_sensor <= 8.0:
                luminance = light_sensor / 903.3
            else:
                luminance = ((light_sensor + 16.0) / 116.0) ** 3.0
            target = 30.0 + 70.0 * min(max(luminance, 0.0), 1.0)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(target):
            return None
        if self._smoothed_ambient_brightness is None:
            self._smoothed_ambient_brightness = target
        else:
            self._smoothed_ambient_brightness += (target - self._smoothed_ambient_brightness) * 0.25
        return int(round(clamp(self._smoothed_ambient_brightness, 0.0, 100.0)))

    def close(self) -> None:
        self._reset_stability_filters(time.monotonic())
        if self._filter_log_file is not None:
            self._filter_log_file.close()
            self._filter_log_file = None

    def _apply_service_update(self, service: str, event_t: float) -> None:
        data = self.sm[service]
        service = self._service_aliases.get(service, service)
        if service == "drivingModelData":
            self.parser._update_driving_model(data)
        elif service == "carParams":
            self.parser._update_car_params(data)
            if "can" in self.services and self.parser.car_fingerprint == PRIUS_TSS2_PLATFORM and self._gear_can_sock is None:
                self._gear_can_sock = self.messaging.sub_sock("can", conflate=False)
            elif self.parser.car_fingerprint != PRIUS_TSS2_PLATFORM:
                self._gear_can_sock = None
        elif service == "modelV2":
            self.parser._update_model_v2(data, event_t)
        elif service == "lateralPlan":
            self.parser._update_lateral_plan(data)
        elif service == "longitudinalPlan":
            self.parser._update_longitudinal_plan(data)
        elif service == "longitudinalPlanSP":
            self.parser._update_longitudinal_plan_sp(data)
        elif service == "controlsState":
            self.parser._update_controls_state(data)
        elif service == "selfdriveState":
            self.parser._update_selfdrive_state(data)
        elif service == "carControl":
            self.parser._update_car_control(data)
        elif service == "cameraOdometry":
            self.parser._update_camera_odometry(data, self._service_valid(service))
        elif service == "radarState":
            self.parser._update_radar_state(data, event_t)
        elif service == "radarTracks":
            self.parser._update_live_tracks(data, event_t)
        elif service in ("can", "sendcan"):
            self.parser._update_can_detections(data, event_t, service)

    def _with_debug_state(self, state: ClusterUiState) -> ClusterUiState:
        force_debug_ui = self._hud_debug_mode in (2, 3)
        if force_debug_ui:
            state = replace(state, debug_ui_visible=True)
        if not self._live_debug_enabled and not self._debug_plot_enabled:
            return state

        profile_stage = self._profile_start()
        live_debug = self._live_debug_info() if self._live_debug_enabled else None
        self._profile_add("source.live.debug_info", profile_stage)

        profile_stage = self._profile_start()
        debug_plot = self._debug_plot_snapshot() if self._debug_plot_enabled else None
        self._profile_add("source.live.debug_plot", profile_stage)

        profile_stage = self._profile_start()
        profile_stage = self._profile_start()
        state = replace(state, live_debug=live_debug, debug_plot=debug_plot)
        self._profile_add("source.live.debug_replace", profile_stage)
        return state

    def _live_debug_info(self) -> LiveDebugInfo | None:
        self._refresh_debug_params()
        cached = self._cached_live_debug

        live_delay_calibration_percent = cached.live_delay_calibration_percent if cached is not None else None
        live_delay_lateral_s = cached.live_delay_lateral_s if cached is not None else None
        if self._service_alive("lateralDelay"):
            live_delay = self._service_data("lateralDelay")
            live_delay_calibration_percent = self._first_present(
                safe_optional_float(live_delay, "calPerc"),
                live_delay_calibration_percent,
            )
            live_delay_lateral_s = self._first_present(
                safe_optional_float(live_delay, "lateralDelay"),
                live_delay_lateral_s,
            )
        steer_actuator_delay_s = self._effective_steer_actuator_delay(live_delay_lateral_s)

        live_torque_calibration_percent = cached.live_torque_calibration_percent if cached is not None else None
        live_torque_valid = cached.live_torque_valid if cached is not None else None
        live_torque_lat_accel_factor = cached.live_torque_lat_accel_factor if cached is not None else None
        live_torque_friction = cached.live_torque_friction if cached is not None else None
        if self._service_alive("lateralTorqueParameters"):
            live_torque = self._service_data("lateralTorqueParameters")
            live_torque_calibration_percent = self._first_present(
                safe_optional_float(live_torque, "calPerc"),
                live_torque_calibration_percent,
            )
            live_valid = torque_params_valid(live_torque)
            live_torque_valid = bool(live_valid) if live_valid is not None else live_torque_valid
            live_torque_lat_accel_factor = self._first_present(
                safe_optional_float(live_torque, "latAccelFactorFiltered"),
                live_torque_lat_accel_factor,
            )
            if live_torque_lat_accel_factor is None:
                live_torque_lat_accel_factor = safe_optional_float(live_torque, "latAccelFactor")
            live_torque_friction = self._first_present(
                safe_optional_float(live_torque, "frictionCoefficientFiltered"),
                live_torque_friction,
            )
            if live_torque_friction is None:
                live_torque_friction = safe_optional_float(live_torque, "frictionCoefficient")

        live_steer_ratio = cached.live_steer_ratio if cached is not None else None
        if self._service_alive("vehicleParameters"):
            live_steer_ratio = self._first_present(
                safe_optional_float(self._service_data("vehicleParameters"), "steerRatio"),
                live_steer_ratio,
            )

        info = LiveDebugInfo(
            live_delay_calibration_percent=live_delay_calibration_percent,
            live_delay_lateral_s=live_delay_lateral_s,
            live_torque_calibration_percent=live_torque_calibration_percent,
            live_torque_valid=live_torque_valid,
            live_torque_lat_accel_factor=live_torque_lat_accel_factor,
            live_torque_friction=live_torque_friction,
            live_steer_ratio=live_steer_ratio,
            custom_steer_ratio=self._custom_steer_ratio,
            steer_actuator_delay_s=steer_actuator_delay_s,
        )
        values = (
            info.live_delay_calibration_percent,
            info.live_delay_lateral_s,
            info.live_torque_calibration_percent,
            info.live_torque_valid,
            info.live_torque_lat_accel_factor,
            info.live_torque_friction,
            info.live_steer_ratio,
            info.custom_steer_ratio,
            info.steer_actuator_delay_s,
        )
        return info if any(value is not None for value in values) else None

    def _refresh_debug_params(self) -> None:
        now = time.monotonic()
        if now < self._next_debug_param_read_t:
            return
        self._next_debug_param_read_t = now + 1.0
        if self.params is None:
            return
        self._custom_steer_ratio = self._finite_param_float("CustomSR", 0.1)
        self._steer_actuator_delay_param_s = self._finite_param_float("SteerActuatorDelay", 0.01)
        self._show_plot_mode = self._param_int(SHOW_PLOT_MODE_PARAM, 0)
        self._cached_live_debug = self._read_cached_live_debug()

    def _debug_plot_snapshot(self) -> DebugPlotSnapshot | None:
        self._refresh_debug_params()
        mode = self._show_plot_mode
        if mode <= 0:
            return None
        values, title = self._make_debug_plot_data(mode)
        return DebugPlotSnapshot(mode=mode, title=title, values=values)

    def _make_debug_plot_data(self, show_plot_mode: int) -> tuple[tuple[float, float, float], str]:
        car_state = self._service_data("carState")
        long_plan = self._service_data("longitudinalPlan")
        car_control = self._service_data("carControl")
        controls_state = self._service_data("controlsState")
        model = self._service_data("modelV2")
        radar = self._service_data("radarState")
        live_params = self._service_data("vehicleParameters")

        a_ego = self._finite_attr(car_state, "aEgo")
        v_ego = self._finite_attr(car_state, "vEgo")
        accel_target = self._finite_index(safe_get(long_plan, "accels"), 0)
        speed_target = self._finite_index(safe_get(long_plan, "speeds"), 0)
        accel_out = self._finite_path(car_control, "actuators.accel")

        if show_plot_mode == 1:
            return (a_ego, accel_target, accel_out), "1.Accel (Y:a_ego, G:a_target, O:a_out)"

        if show_plot_mode == 2:
            return (speed_target, v_ego, a_ego), "2.Speed/Accel(Y:speed_0, G:v_ego, O:a_ego)"

        if show_plot_mode == 3:
            position = safe_get(model, "position")
            velocity = safe_get(model, "velocity")
            pos_32 = self._finite_index(safe_get(position, "x"), 32) if position is not None else 0.0
            vel_32 = self._finite_index(safe_get(velocity, "x"), 32) if velocity is not None else 0.0
            vel_0 = self._finite_index(safe_get(velocity, "x"), 0) if velocity is not None else 0.0
            return (pos_32, vel_32, vel_0), "3.Model(Y:pos_32, G:vel_32, O:vel_0)"

        if show_plot_mode == 4:
            lead = safe_get(radar, "leadOne")
            return (
                accel_target,
                self._finite_attr(lead, "aLeadK"),
                self._finite_attr(lead, "vRel"),
            ), "4.Lead(Y:accel, G:a_leadK, O:v_rel)"

        if show_plot_mode == 5:
            lead = safe_get(radar, "leadOne")
            return (
                a_ego,
                self._finite_attr(lead, "aLead"),
                self._finite_attr(lead, "jLead"),
            ), "5.Lead(Y:a_ego, G:a_lead, O:j_lead)"

        if show_plot_mode == 6:
            torque_state = self._path_value(controls_state, "lateralControlState.torqueState")
            return (
                self._finite_attr(torque_state, "actualLateralAccel") * 10.0,
                self._finite_attr(torque_state, "desiredLateralAccel") * 10.0,
                self._finite_attr(torque_state, "output") * 10.0,
            ), "6.Steer(Y:actual, G:desire, O:output) *10"

        if show_plot_mode == 7:
            return (
                self._finite_attr(car_state, "steeringAngleDeg"),
                self._finite_path(car_control, "actuators.steeringAngleDeg"),
                self._finite_attr(live_params, "angleOffsetDeg") * 10.0,
            ), "7.SteerA(Y:Actual, G:Target, O:Offset*10)"

        if show_plot_mode == 8:
            curvature = self._finite_path(car_control, "actuators.curvature") * 10000.0
            return (curvature, curvature, curvature), "8.Curvature(*10000)"

        return (0.0, 0.0, 0.0), "no data"

    def _read_cached_live_debug(self) -> LiveDebugInfo | None:
        if self.params is None:
            return None

        live_delay = self._event_service_from_param("LiveDelay", "lateralDelay", "liveDelay")
        live_torque = self._event_service_from_param(
            "LiveTorqueParameters", "lateralTorqueParameters", "liveTorqueParameters"
        )
        live_parameters = self._event_service_from_param("LiveParametersV2", "vehicleParameters", "liveParameters")
        live_steer_ratio = None
        if live_parameters is not None:
            live_steer_ratio = safe_optional_float(live_parameters, "steerRatio")
        if live_steer_ratio is None:
            live_steer_ratio = self._legacy_live_parameters_steer_ratio()

        live_torque_valid = None
        if live_torque is not None:
            live_valid = torque_params_valid(live_torque)
            live_torque_valid = bool(live_valid) if live_valid is not None else None

        info = LiveDebugInfo(
            live_delay_calibration_percent=safe_optional_float(live_delay, "calPerc") if live_delay is not None else None,
            live_delay_lateral_s=safe_optional_float(live_delay, "lateralDelay") if live_delay is not None else None,
            live_torque_calibration_percent=(
                safe_optional_float(live_torque, "calPerc") if live_torque is not None else None
            ),
            live_torque_valid=live_torque_valid,
            live_torque_lat_accel_factor=(
                safe_optional_float(live_torque, "latAccelFactorFiltered") if live_torque is not None else None
            ),
            live_torque_friction=(
                safe_optional_float(live_torque, "frictionCoefficientFiltered") if live_torque is not None else None
            ),
            live_steer_ratio=live_steer_ratio,
        )
        values = (
            info.live_delay_calibration_percent,
            info.live_delay_lateral_s,
            info.live_torque_calibration_percent,
            info.live_torque_valid,
            info.live_torque_lat_accel_factor,
            info.live_torque_friction,
            info.live_steer_ratio,
        )
        return info if any(value is not None for value in values) else None

    def _event_service_from_param(self, param_key: str, *service_names: str) -> Any | None:
        if self.params is None or self.log is None:
            return None
        try:
            data = self.params.get(param_key)
        except Exception:
            return None
        if not data:
            return None
        try:
            event = self.messaging.log_from_bytes(data, self.log.Event)
        except Exception:
            return None
        which = None
        try:
            which = event.which()
        except Exception:
            pass
        for service_name in service_names:
            if which is not None and which != service_name:
                continue
            value = safe_get(event, service_name)
            if value is not None:
                return value
        return None

    def _legacy_live_parameters_steer_ratio(self) -> float | None:
        if self.params is None:
            return None
        try:
            data = self.params.get("LiveParameters")
        except Exception:
            return None
        if not data:
            return None
        if isinstance(data, dict):
            return finite_float(data.get("steerRatio"))
        try:
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8")
            parsed = json.loads(data)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        return finite_float(parsed.get("steerRatio"))

    def _effective_steer_actuator_delay(self, live_delay_lateral_s: float | None) -> float | None:
        if self._steer_actuator_delay_param_s is not None and self._steer_actuator_delay_param_s > 0.0:
            return self._steer_actuator_delay_param_s
        if live_delay_lateral_s is not None:
            return live_delay_lateral_s
        return self._steer_actuator_delay_param_s

    @staticmethod
    def _first_present(value: Any | None, fallback: Any | None) -> Any | None:
        return value if value is not None else fallback

    def _finite_param_float(self, key: str, scale: float) -> float | None:
        if self.params is None:
            return None
        try:
            value = float(read_float_param(self.params, key)) * scale
        except Exception:
            return None
        return value if math.isfinite(value) else None

    def _param_int(self, key: str, default: int) -> int:
        if self.params is None:
            return default
        try:
            value = int(read_int_param(self.params, key))
        except Exception:
            return default
        return value if value >= 0 else default

    def _sm_key(self, service: str) -> str:
        if service in self.services:
            return service
        legacy = LIVE_SERVICE_LEGACY_NAMES.get(service)
        return legacy if legacy is not None and legacy in self.services else service

    def _service_data(self, service: str) -> Any | None:
        try:
            return self.sm[self._sm_key(service)]
        except Exception:
            return None

    @staticmethod
    def _path_value(obj: Any | None, path: str) -> Any | None:
        current = obj
        for name in path.split("."):
            if current is None:
                return None
            current = safe_get(current, name)
        return current

    def _finite_path(self, obj: Any | None, path: str, default: float = 0.0) -> float:
        return self._finite_value(self._path_value(obj, path), default)

    def _finite_attr(self, obj: Any | None, name: str, default: float = 0.0) -> float:
        return self._finite_value(safe_get(obj, name), default)

    def _finite_index(self, values: Any | None, index: int, default: float = 0.0) -> float:
        if values is None or index < 0:
            return default
        try:
            value = values[index]
        except Exception:
            return default
        return self._finite_value(value, default)

    @staticmethod
    def _finite_value(value: Any | None, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) else default

    def _update_current_speed(self) -> None:
        if not self._service_alive("carState"):
            return
        try:
            self.parser.current_speed_kph = clamp(float(self.sm["carState"].vEgo) * 3.6, 0.0, MAX_SPEED_KPH)
        except Exception:
            return

    def _service_time(self, service: str) -> float:
        try:
            mono_time = self.sm.logMonoTime.get(service, 0)
        except AttributeError:
            mono_time = 0
        return float(mono_time) / 1_000_000_000.0 if mono_time else time.monotonic()

    def _service_alive(self, service: str) -> bool:
        try:
            return bool(self.sm.alive.get(self._sm_key(service), False))
        except AttributeError:
            return False

    def _service_updated(self, service: str) -> bool:
        try:
            return bool(self.sm.updated.get(service, False))
        except AttributeError:
            return False

    def _service_valid(self, service: str) -> bool:
        try:
            return bool(self.sm.valid.get(service, True))
        except AttributeError:
            return True


def standby_state() -> ClusterUiState:
    return ClusterUiState(
        speed_kph=0.0,
        accel_mps2=0.0,
        steering=0.0,
        speed_limit_kph=None,
        speed_limit_source=None,
        cruise_kph=None,
        cruise_display_state="off",
        gear_text=None,
        cruise_gap=None,
        lfa_active=None,
        left_signal=False,
        right_signal=False,
        left_blindspot=False,
        right_blindspot=False,
        lane_change=None,
        lane_change_phase="idle",
        lane_change_progress=0.0,
        highlight_lane=None,
        highlight_lane_offset=None,
        ego_lane_offset=0.0,
        road_view_lane_position=0.0,
        camera_lane_center_offset_m=None,
        lane_width_m=DEFAULT_LANE_WIDTH_M,
        steering_angle_deg=None,
        surround_yaw_deg=0.0,
        surround_pitch_deg=0.0,
        surround_view_active=False,
        lanes=(
            LaneMarking(-0.5, BLUE, "solid", width=7),
            LaneMarking(0.5, BLUE, "solid", width=7),
        ),
    )
