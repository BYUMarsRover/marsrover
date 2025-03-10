#!/bin/bash
# Created by Braden Meyers, Feb 2025
#
# Launches the ZED over SSH using the 'zed_launch' tmux session

function printInfo {
  	# print blue
  	echo -e "\033[0m\033[36m[INFO] $1\033[0m"
}

function printWarning {
  	# print yellow
  	echo -e "\033[0m\033[33m[WARNING] $1\033[0m"
}

function printError {
  	# print red
  	echo -e "\033[0m\033[31m[ERROR] $1\033[0m"
}

ROVER_IP_ADDRESS=192.168.1.120

# Check for an SSH connection to the rover
if ! ssh marsrover@$ROVER_IP_ADDRESS "echo" &> /dev/null
then
    printError "No available SSH connection to the rover's computer"
    echo "Here's some debugging suggestions:"
    echo "  - Ensure the rover is powered on"
    echo "  - Ensure the rover is connected with a static IP address"

    exit
fi

# Send tmux commands to the rover over SSH
printInfo "Setting up the ZED tmux session..."
ssh marsrover@$ROVER_IP_ADDRESS "tmux new-session -d -s zed_launch; \
    tmux set-option -g default-terminal "screen-256color"; \
    tmux set -g mouse on; \
    tmux send-keys -t zed_launch.0 'clear' Enter; \
    tmux send-keys -t zed_launch.0 'export ROS_DISCOVERY_SERVER=127.0.0.1:11811' Enter; \
    tmux send-keys -t zed_launch.0 'source /opt/ros/foxy/setup.bash' Enter; \
    tmux send-keys -t zed_launch.0 'cd ~/foxy_ws && source install/setup.bash' Enter; \
    tmux send-keys -t zed_launch.0 'ros2 launch object_detection object_detection_launch.py'" # NO ENTER

# Attach to the 'zed_launch' tmux session to view the output
ssh -t -X marsrover@$ROVER_IP_ADDRESS "tmux attach -t zed_launch"

# Kill the tmux session on exit
ssh marsrover@$ROVER_IP_ADDRESS "tmux kill-session -t zed_launch"

# TODO: Get the ZED running in a Docker container too, add the code to this repo
