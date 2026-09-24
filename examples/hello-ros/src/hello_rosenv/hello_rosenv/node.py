import os

import rclpy
from rclpy.node import Node


def main():
    rclpy.init()
    node = Node("hello_rosenv")
    try:
        node.get_logger().info(f"Hello from ROS 2 {os.environ['ROS_DISTRO']}!")
    finally:
        node.destroy_node()
        rclpy.shutdown()
