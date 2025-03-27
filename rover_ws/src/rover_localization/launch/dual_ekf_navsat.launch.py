# Modified from the GPS demo - Nelson Durrant, Feb 2025
from launch import LaunchDescription
from ament_index_python.packages import get_package_share_directory
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch.actions import DeclareLaunchArgument
import launch_ros.actions
import os
import launch.actions
from launch.conditions import UnlessCondition, IfCondition

# IMPORTANT! We're just using the built-in ZED localization package for now to manage the map->odom and
# odom->base_link transformations. It consistently performed better than this custom dual-EKF implementation
# using the robot_localization package in testing. We'll leave the launch params here for future reference.


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")
    declare_use_sim_time_cmd = DeclareLaunchArgument(
        "use_sim_time", default_value="False", description="Use simulation time"
    )

    loc_dir = get_package_share_directory("rover_localization")
    sim_rl_params_file = os.path.join(
        loc_dir, "config", "sim_dual_ekf_navsat_params.yaml"
    )
    rl_params_file = os.path.join(loc_dir, "config", "dual_ekf_navsat_params.yaml")

    return LaunchDescription(
        [
            declare_use_sim_time_cmd,
            launch.actions.DeclareLaunchArgument(
                "output_final_position", default_value="false"
            ),
            launch.actions.DeclareLaunchArgument(
                "output_location", default_value="~/dual_ekf_navsat_example_debug.txt"
            ),
            # launch_ros.actions.Node(
            #     package="robot_localization",
            #     executable="ekf_node",
            #     name="ekf_filter_node_odom",
            #     output="screen",
            #     parameters=[rl_params_file, {"use_sim_time": use_sim_time}],
            #     remappings=[("odometry/filtered", "odometry/local")],
            #     condition=UnlessCondition(use_sim_time),
            # ),
            launch_ros.actions.Node(
                package="robot_localization",
                executable="ekf_node",
                name="ekf_filter_node_odom",
                output="screen",
                parameters=[sim_rl_params_file, {"use_sim_time": use_sim_time}],
                remappings=[("odometry/filtered", "odometry/local")],
                condition=IfCondition(use_sim_time),
            ),
            # launch_ros.actions.Node(
            #     package="robot_localization",
            #     executable="ekf_node",
            #     name="ekf_filter_node_map",
            #     output="screen",
            #     parameters=[rl_params_file, {"use_sim_time": use_sim_time}],
            #     remappings=[("odometry/filtered", "odometry/global")],
            #     condition=UnlessCondition(use_sim_time),
            # ),
            # launch_ros.actions.Node(
            #     package="robot_localization",
            #     executable="ekf_node",
            #     name="ekf_filter_node_map",
            #     output="screen",
            #     parameters=[rl_params_file, {"use_sim_time": use_sim_time}],
            #     remappings=[("odometry/filtered", "odometry/global")],
            #     condition=UnlessCondition(use_sim_time),
            # ),
            launch_ros.actions.Node(
                package="robot_localization",
                executable="ekf_node",
                name="ekf_filter_node_map",
                output="screen",
                parameters=[sim_rl_params_file, {"use_sim_time": use_sim_time}],
                remappings=[("odometry/filtered", "odometry/global")],
                condition=IfCondition(use_sim_time),
            ),
            # launch_ros.actions.Node(
            #     package="robot_localization",
            #     executable="navsat_transform_node",
            #     name="navsat_transform",
            #     output="screen",
            #     parameters=[rl_params_file, {"use_sim_time": use_sim_time}],
            #     remappings=[
            #         ("imu/data", "zed/zed_node/imu/data"),
            #         ("gps/fix", "gps/fix"),
            #         ("gps/filtered", "gps/filtered"),
            #         ("odometry/gps", "odometry/gps"),
            #         ("odometry/filtered", "odometry/global"),
            #     ],
            #     condition=UnlessCondition(use_sim_time),
            # ),
            launch_ros.actions.Node(
                package="robot_localization",
                executable="navsat_transform_node",
                name="navsat_transform",
                output="screen",
                parameters=[sim_rl_params_file, {"use_sim_time": use_sim_time}],
                remappings=[
                    ("imu/data", "imu/data"),
                    ("gps/fix", "gps/fix"),
                    ("gps/filtered", "gps/filtered"),
                    ("odometry/gps", "odometry/gps"),
                    ("odometry/filtered", "odometry/global"),
                ],
                condition=IfCondition(use_sim_time),
            ),
            # Added PVT to NSF conversion node
            launch_ros.actions.Node(
                package="rover_localization",
                executable="pvt_to_nsf",
                output="screen",
                condition=UnlessCondition(use_sim_time),
            ),
            # Added GEO to NSF conversion node
            launch_ros.actions.Node(
                package="rover_localization",
                executable="geo_to_nsf",
                output="screen",
                condition=UnlessCondition(use_sim_time),
            ),
            # Added IMU and magnetometer filter node
            # launch_ros.actions.Node(
            #     package='imu_filter_madgwick',
            #     executable='imu_filter_madgwick_node',
            #     name='imu_filter_madgwick',
            #     output='screen',
            #     remappings=[
            #         ('imu/data_raw', 'zed/zed_node/imu/data_raw'),
            #         ('imu/mag', 'zed/zed_node/imu/mag')
            #     ],
            #     parameters=[rl_params_file],
            #     condition=UnlessCondition(use_sim_time),
            # ),
        ]
    )
