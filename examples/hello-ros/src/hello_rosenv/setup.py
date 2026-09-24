from setuptools import setup

setup(
    name="hello_rosenv",
    version="0.1.0",
    packages=["hello_rosenv"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/hello_rosenv"]),
        ("share/hello_rosenv", ["package.xml"]),
    ],
    entry_points={"console_scripts": ["hello = hello_rosenv.node:main"]},
    tests_require=["pytest"],
)
