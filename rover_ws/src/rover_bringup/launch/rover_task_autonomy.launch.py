# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.actions import IncludeLaunchDescription, DeclareLaunchArgument
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.conditions import IfCondition
from nav2_common.launch import RewrittenYaml


def generate_launch_description():
    # Get the launch directory
    bringup_dir = get_package_share_directory('nav2_bringup')
    nav_dir = get_package_share_directory("rover_navigation")
    gz_dir = get_package_share_directory("rover_gazebo")
    description_dir = get_package_share_directory("rover_description")
    nav_launch_dir = os.path.join(nav_dir, 'launch')
    gz_launch_dir = os.path.join(gz_dir, 'launch')
    description_launch_dir = os.path.join(description_dir, 'launch')
    nav_params_dir = os.path.join(nav_dir, "config")
    nav2_params = os.path.join(nav_params_dir, "nav2_no_map_params.yaml")
    configured_params = RewrittenYaml(
        source_file=nav2_params, root_key="", param_rewrites="", convert_types=True
    )

    use_rviz = LaunchConfiguration('use_rviz')
    use_mapviz = LaunchConfiguration('use_mapviz')
    sim_mode = LaunchConfiguration('sim_mode')

    declare_use_rviz_cmd = DeclareLaunchArgument(
        'use_rviz',
        default_value='False',
        description='Whether to start RVIZ')

    declare_use_mapviz_cmd = DeclareLaunchArgument(
        'use_mapviz',
        default_value='False',
        description='Whether to start mapviz')
    
    declare_sim_mode_cmd = DeclareLaunchArgument(
        'sim_mode',
        default_value='false',
        description='Whether to start in simulation mode')
    
    description_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(description_launch_dir, 'robot_state_publisher.launch.py')),
        launch_arguments={
            "use_sim_time": sim_mode,
        }.items(),
    )

    gazebo_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(gz_launch_dir, 'gazebo_gps_world.launch.py')),
        condition=IfCondition(sim_mode),
    )

    robot_localization_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav_launch_dir, 'dual_ekf_navsat.launch.py')),
        launch_arguments={
            "use_sim_time": "True",
        }.items(),
    )

    navigation2_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(bringup_dir, "launch", "navigation_launch.py")
        ),
        launch_arguments={
            "use_sim_time": sim_mode,
            "params_file": configured_params,
            "autostart": "True",
        }.items(),
    )

    rviz_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(bringup_dir, "launch", 'rviz_launch.py')),
        condition=IfCondition(use_rviz),
        launch_arguments={
            "use_sim_time": sim_mode,
        }.items(),
    )

    mapviz_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav_launch_dir, 'mapviz.launch.py')),
        condition=IfCondition(use_mapviz),
        launch_arguments={
            "use_sim_time": sim_mode,
        }.items(),
    )

    aruco_opencv_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav_launch_dir, 'aruco_opencv.launch.py')),
        launch_arguments={
            "use_sim_time": sim_mode,
        }.items(),
    )

    # TODO: Add ublox and LiDAR launch files to launch when not sim_mode

    task_exec_cmd = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav_launch_dir, 'task_executor.launch.py')),
        launch_arguments={
            "use_sim_time": sim_mode,
        }.items(),
    )

    # Create the launch description and populate
    ld = LaunchDescription()
    ld.add_action(declare_sim_mode_cmd)

    # robot state publisher launch
    ld.add_action(description_cmd)

    # simulation launch
    ld.add_action(gazebo_cmd)

    # robot localization launch
    ld.add_action(robot_localization_cmd)

    # navigation2 launch
    ld.add_action(navigation2_cmd)

    # viz launch
    ld.add_action(declare_use_rviz_cmd)
    ld.add_action(rviz_cmd)
    ld.add_action(declare_use_mapviz_cmd)
    ld.add_action(mapviz_cmd)

    # custom launch
    ld.add_action(aruco_opencv_cmd)
    ld.add_action(task_exec_cmd)

    return ld
