import rclpy
from rclpy.node import Node


def test_ros_node_uses_requested_namespace():
    context = rclpy.Context()
    context.init()
    node = Node("workspace_test", namespace="/rosenv_example", context=context)
    try:
        assert node.get_fully_qualified_name() == "/rosenv_example/workspace_test"
    finally:
        node.destroy_node()
        context.shutdown()
