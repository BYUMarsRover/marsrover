# Created by Nelson Durrant, Mar 2025
import os

from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import PathJoinSubstitution
from launch.actions import IncludeLaunchDescription
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():

    use_sim_time = LaunchConfiguration("use_sim_time")
    declare_use_sim_time_cmd = DeclareLaunchArgument(
        "use_sim_time", default_value="False", description="Use simulation time"
    )

    # Get the launch directory
    description_dir = get_package_share_directory("rover_description")
    sim_urdf = os.path.join(description_dir, "urdf", "turtlebot3_waffle_gps.urdf")
    with open(sim_urdf, "r") as infp:
        sim_robot_description = infp.read()

    # https://github.com/ros/urdf_launch
    start_robot_state_publisher_cmd = IncludeLaunchDescription(
        PathJoinSubstitution(
            [FindPackageShare("urdf_launch"), "launch", "description.launch.py"]
        ),
        # Uncomment the below (and comment out the above) to see the robot model displayed in RViz
        # PathJoinSubstitution(
        #     [FindPackageShare("urdf_launch"), "launch", "display.launch.py"]
        # ),
        launch_arguments={
            "urdf_package": "rover_description",
            "urdf_package_path": PathJoinSubstitution(["urdf", "rover.urdf.xacro"]),
        }.items(),
        condition=UnlessCondition(use_sim_time),
    )

    sim_start_robot_state_publisher_cmd = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        name="robot_state_publisher",
        output="both",
        parameters=[
            {"robot_description": sim_robot_description, "use_sim_time": use_sim_time}
        ],
        condition=IfCondition(use_sim_time),
    )

    zed_lidar_calibrate_cmd = Node(
        package="rover_description",
        executable="zed_lidar_calibrate",
        name="zed_lidar_calibrate",
        output="screen",
        condition=UnlessCondition(use_sim_time),
    )

    # Create the launch description and populate
    ld = LaunchDescription()

    # robot state publisher launch
    ld.add_action(declare_use_sim_time_cmd)
    ld.add_action(start_robot_state_publisher_cmd)
    ld.add_action(sim_start_robot_state_publisher_cmd)
    # ld.add_action(zed_lidar_calibrate_cmd)

    return ld
