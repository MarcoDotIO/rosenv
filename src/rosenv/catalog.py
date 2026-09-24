"""Official Docker Library ROS images, verified 2026-09-23.

Source: https://github.com/docker-library/official-images/blob/master/library/ros
Tags are resolved to immutable local image IDs when an environment is created.
"""

DISTROS = {
    "humble": {"ubuntu": "22.04", "codename": "jammy", "eol": "2027-05"},
    "jazzy": {"ubuntu": "24.04", "codename": "noble", "eol": "2029-05"},
    "kilted": {"ubuntu": "24.04", "codename": "noble", "eol": "2026-12"},
    "lyrical": {"ubuntu": "26.04", "codename": "resolute", "eol": "2031-05"},
    "rolling": {"ubuntu": "26.04", "codename": "resolute", "eol": "rolling"},
}


def image_for(distro: str, variant: str = "ros-base") -> str:
    return f"docker.io/library/ros:{distro}-{variant}-{DISTROS[distro]['codename']}"
