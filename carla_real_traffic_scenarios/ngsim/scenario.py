import hashlib
import logging
import random
from typing import Optional

import carla
import numpy as np

from carla_real_traffic_scenarios import DT
from carla_real_traffic_scenarios.early_stop import EarlyStopMonitor
from carla_real_traffic_scenarios.ngsim import FRAMES_BEFORE_MANUVEUR, FRAMES_AFTER_MANUVEUR, NGSimDataset, DatasetMode
from carla_real_traffic_scenarios.ngsim.ngsim_recording import NGSimRecording, LaneChangeInstant, PIXELS_TO_METERS
from carla_real_traffic_scenarios.reward import RewardType
from carla_real_traffic_scenarios.scenario import ScenarioStepResult, Scenario, ChauffeurCommand
from carla_real_traffic_scenarios.utils.carla import RealTrafficVehiclesInCarla, setup_carla_settings
from carla_real_traffic_scenarios.utils.collections import find_first_matching
from carla_real_traffic_scenarios.utils.geometry import normalize_angle
from carla_real_traffic_scenarios.utils.transforms import distance_between_on_plane

CROSSTRACK_ERROR_TOLERANCE = 0.3
YAW_DEG_ERRORS_TOLERANCE = 10
TARGET_LANE_ALIGNMENT_FRAMES = 10

LOGGER = logging.getLogger(__name__)


class NGSimLaneChangeScenario(Scenario):
    """
    Possible improvements:
    - include bikes in CARLA to model NGSim motorcycles
    """

    def __init__(self, ngsim_dataset: NGSimDataset, *, dataset_mode: DatasetMode, data_dir, reward_type: RewardType,
                 client: carla.Client):
        super().__init__(client)

        setup_carla_settings(client, synchronous=True, time_delta_s=DT)

        self._ngsim_recording = NGSimRecording(
            data_dir=data_dir, ngsim_dataset=ngsim_dataset,
        )
        self._ngsim_dataset = ngsim_dataset
        self._ngsim_vehicles_in_carla = None
        self._target_alignment_counter: int
        self._dataset_mode = dataset_mode
        self._early_stop_monitor: Optional[EarlyStopMonitor] = None

        def determine_split(lane_change_instant: LaneChangeInstant) -> DatasetMode:
            split_frac = 0.8
            hash_num = int(hashlib.sha1(str(lane_change_instant).encode('utf-8')).hexdigest(), 16)
            if (hash_num % 100) / 100 < split_frac:
                return DatasetMode.TRAIN
            else:
                return DatasetMode.VALIDATION

        self._lane_change_instants = [
            lci for lci in self._ngsim_recording.lane_change_instants if determine_split(lci) == dataset_mode
        ]
        LOGGER.info(
            f"Got {len(self._lane_change_instants)} lane change subscenarios "
            f"in {ngsim_dataset.name}_{dataset_mode.name}")
        self._reward_type = reward_type

    def reset(self, vehicle: carla.Vehicle):
        if self._ngsim_vehicles_in_carla:
            self._ngsim_vehicles_in_carla.close()

        self._ngsim_vehicles_in_carla = RealTrafficVehiclesInCarla(self._client, self._world)

        if self._early_stop_monitor:
            self._early_stop_monitor.close()

        timeout_s = (FRAMES_BEFORE_MANUVEUR + FRAMES_AFTER_MANUVEUR) * DT
        self._early_stop_monitor = EarlyStopMonitor(vehicle, timeout_s=timeout_s)

        self._lane_change: LaneChangeInstant = random.choice(self._lane_change_instants)

        self._target_alignment_counter = 0
        self._previous_chauffeur_command = self._lane_change.chauffeur_command
        self._previous_progress = 0
        self._total_distance_m = None
        self._checkpoints_distance_m = None

        frame_manuveur_start = max(self._lane_change.frame_start - FRAMES_BEFORE_MANUVEUR, 0)
        self._ngsim_recording.reset(timeslot=self._lane_change.timeslot, frame=frame_manuveur_start - 1)
        ngsim_vehicles = self._ngsim_recording.step()

        agent_ngsim_vehicle = find_first_matching(ngsim_vehicles, lambda v: v.id == self._lane_change.vehicle_id)
        other_ngsim_vehicles = [v for v in ngsim_vehicles if v.id != self._lane_change.vehicle_id]

        t = agent_ngsim_vehicle.transform
        vehicle.set_transform(t.as_carla_transform())
        v = t.orientation * agent_ngsim_vehicle.speed * PIXELS_TO_METERS
        vehicle.set_velocity(v.to_vector3(0).as_carla_vector3d())  # meters per second,

        self._start_lane_waypoint = self._world_map.get_waypoint(t.as_carla_transform().location)
        self._target_lane_waypoint = {
            ChauffeurCommand.CHANGE_LANE_LEFT: self._start_lane_waypoint.get_left_lane,
            ChauffeurCommand.CHANGE_LANE_RIGHT: self._start_lane_waypoint.get_right_lane,
        }[self._lane_change.chauffeur_command]()

        self._ngsim_vehicles_in_carla.step(other_ngsim_vehicles)

    def step(self, ego_vehicle: carla.Vehicle) -> ScenarioStepResult:
        ngsim_vehicles = self._ngsim_recording.step()
        other_ngsim_vehicles = [v for v in ngsim_vehicles if v.id != self._lane_change.vehicle_id]
        self._ngsim_vehicles_in_carla.step(other_ngsim_vehicles)

        ego_transform = ego_vehicle.get_transform()
        waypoint = self._world_map.get_waypoint(ego_transform.location)

        on_start_lane = waypoint.lane_id == self._start_lane_waypoint.lane_id
        on_target_lane = waypoint.lane_id == self._target_lane_waypoint.lane_id

        not_on_expected_lanes = not (on_start_lane or on_target_lane)
        chauffeur_command = self._lane_change.chauffeur_command if on_start_lane else ChauffeurCommand.LANE_FOLLOW
        scenario_finished_with_success = on_target_lane & self._is_lane_aligned(ego_transform, waypoint)

        early_stop = not scenario_finished_with_success and \
                     (self._early_stop_monitor(ego_transform) | not_on_expected_lanes)

        done = scenario_finished_with_success | early_stop
        reward = int(self._reward_type == RewardType.DENSE) * self._get_progress_change(ego_transform)
        reward += int(scenario_finished_with_success)
        reward += int(early_stop) * -1

        self._previous_chauffeur_command = chauffeur_command
        info = {
            'ngsim_dataset': {
                'road': self._ngsim_dataset.name,
                'timeslice': self._lane_change.timeslot.file_suffix,
                'frame': self._ngsim_recording.frame,
                'dataset_mode': self._dataset_mode.name
            },
            'reward_type': self._reward_type.name,
            'target_alignment_counter': self._target_alignment_counter,
        }
        return ScenarioStepResult(chauffeur_command, reward, done, info)

    def _is_lane_aligned(self, ego_transform, waypoint):
        crosstrack_error = distance_between_on_plane(ego_transform.location, waypoint.transform.location)
        yaw_error = normalize_angle(np.deg2rad(ego_transform.rotation.yaw - waypoint.transform.rotation.yaw))
        aligned_with_target_lane = crosstrack_error < CROSSTRACK_ERROR_TOLERANCE and \
                                   yaw_error < np.deg2rad(YAW_DEG_ERRORS_TOLERANCE)
        if aligned_with_target_lane:
            self._target_alignment_counter += 1
        else:
            self._target_alignment_counter = 0
        return self._target_alignment_counter == TARGET_LANE_ALIGNMENT_FRAMES

    def close(self):
        if self._early_stop_monitor:
            self._early_stop_monitor.close()
            self._early_stop_monitor = None

        if self._ngsim_vehicles_in_carla:
            self._ngsim_vehicles_in_carla.close()
            self._ngsim_vehicles_in_carla = None

        self._lane_change_instants = []
        self._lane_change = None

        del self._ngsim_recording
        self._ngsim_recording = None

    def _get_progress_change(self, ego_transform: carla.Transform):

        current_location = ego_transform.location
        current_waypoint = self._world_map.get_waypoint(current_location)
        on_start_lane = current_waypoint.lane_id == self._start_lane_waypoint.lane_id
        on_target_lane = current_waypoint.lane_id == self._target_lane_waypoint.lane_id

        checkpoints_number = 10
        if self._total_distance_m is None:
            target_lane_location = self._target_lane_waypoint.transform.location
            self._total_distance_m = current_location.distance(target_lane_location)
            self._checkpoints_distance_m = self._total_distance_m / checkpoints_number

        def _calc_progress_change(start_waypoint, target_waypoint, current_location):
            start_location = start_waypoint.transform.location
            target_location = target_waypoint.transform.location
            distance_from_start = current_location.distance(start_location)
            distance_from_target = current_location.distance(target_location)

            distance_traveled_m = self._total_distance_m - distance_from_target
            checkpoints_passed_number = int(distance_traveled_m / self._checkpoints_distance_m)

            # zero if passed target centerline
            passed_target_centerline = distance_from_start > self._total_distance_m and \
                                       distance_from_start > distance_from_target
            progress = int(not passed_target_centerline) * (checkpoints_passed_number / checkpoints_number)

            progress_change = progress - self._previous_progress
            self._previous_progress = progress
            return progress_change

        progress_change = 0
        if on_start_lane:
            start_waypoint = current_waypoint
            target_waypoint = {
                ChauffeurCommand.CHANGE_LANE_LEFT: start_waypoint.get_left_lane,
                ChauffeurCommand.CHANGE_LANE_RIGHT: start_waypoint.get_right_lane,
            }[self._lane_change.chauffeur_command]()
            progress_change = _calc_progress_change(start_waypoint, target_waypoint, current_location)
        elif on_target_lane:
            target_waypoint = current_waypoint
            start_waypoint = {
                ChauffeurCommand.CHANGE_LANE_LEFT: target_waypoint.get_right_lane,
                ChauffeurCommand.CHANGE_LANE_RIGHT: target_waypoint.get_left_lane,
            }[self._lane_change.chauffeur_command]()
            progress_change = _calc_progress_change(start_waypoint, target_waypoint, current_location)

        return progress_change
