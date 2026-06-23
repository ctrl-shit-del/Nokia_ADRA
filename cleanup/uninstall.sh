#!/bin/bash

echo "================================================"
echo "    ADRA Test Environment Cleanup Utility"
echo "================================================"

# NS-3 Cleanup
if [ -d "/tmp/ns-3-dev" ]; then
    echo "=> Removing NS-3 development directory (/tmp/ns-3-dev)..."
    rm -rf /tmp/ns-3-dev
    echo "   Done."
else
    echo "=> NS-3 directory not found. Skipping."
fi

# ROS2 Cleanup
echo "=> Attempting to remove ROS2 Humble packages..."
if dpkg -l | grep -q ros-humble-ros-base; then
    sudo apt-get purge -y "ros-humble-*" > /dev/null 2>&1
    sudo apt-get autoremove -y > /dev/null 2>&1
    echo "   ROS2 Humble packages purged."
else
    echo "   ROS2 Humble is not installed. Skipping."
fi

# ROS2 APT Sources
if [ -f "/etc/apt/sources.list.d/ros2.list" ]; then
    echo "=> Removing ROS2 APT source lists..."
    sudo rm -f /etc/apt/sources.list.d/ros2.list
    sudo rm -f /usr/share/keyrings/ros-archive-keyring.gpg
    sudo apt-get update > /dev/null 2>&1
    echo "   Done."
fi

echo "================================================"
echo " Cleanup Complete! You can now re-run the Agent."
echo "================================================"
