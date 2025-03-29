# Created by Nelson Durrant, Feb 2025
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
import launch_ros.actions
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():

    use_sim_time = LaunchConfiguration("use_sim_time")
    declare_use_sim_time_cmd = DeclareLaunchArgument(
        "use_sim_time", default_value="False", description="Use simulation time"
    )
    # cam_config_path = os.path.join(
    #     get_package_share_directory("rover_perception"),
    #     "config",
    #     "aruco_cam_params.yaml",
    # )

    # try:
    #     # Path to the symlink
    #     udev_path = "/dev/rover/cameras/autonomyWebCam"

    #     # Read the symlink to get the relative path it points to (e.g., ../../video8)
    #     relative_target = os.readlink(udev_path)

    #     # Extract the final component of the relative path (e.g., 'video8')
    #     final_device_name = os.path.basename(relative_target)

    #     # Construct the absolute path in /dev folder (e.g., /dev/video8)
    #     absolute_target = os.path.join("/dev", final_device_name)

    #     device = {"video_device": absolute_target}

    #     # Print the final absolute device path
    #     print(f"Autonomy Web Cam using: {absolute_target}")

    # except OSError as e:
    #     print(f"Could not resolve symlink: {e}")
    #     print("This is expected if you're running in simulation mode")

    return LaunchDescription(
        [
            declare_use_sim_time_cmd,
            # launch_ros.actions.Node(
            #     package="usb_cam",
            #     executable="usb_cam_node_exe",
            #     namespace="aruco_cam",
            #     output="screen",
            #     parameters=[device, cam_config_path],
            # ),
            launch_ros.actions.Node(
                # https://github.com/fictionlab/ros_aruco_opencv
                package="aruco_opencv",
                executable="aruco_tracker_autostart",
                output="screen",
                parameters=[
                    {
                        "cam_base_topic": "aruco_cam/image_raw",
                        "marker_size": 0.2,
                        "use_sim_time": use_sim_time,
                    }
                ],
                remappings=[
                    ("/aruco_cam/image_raw", "/zed/zed_node/rgb/image_rect_color"),
                    (
                        "/aruco_cam/camera_info",
                        "/zed/zed_node/rgb/camera_info",
                    ),
                ],
            ),
        ]
    )
