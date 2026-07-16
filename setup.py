from setuptools import setup, find_packages

setup(
    name="sonilo-mcp",
    version="0.5.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "sonilo-mcp = sonilo_mcp.api:main",
        ],
    },
    author="Sonilo",
    author_email="support@sonilo.com",
    description="MCP server for Sonilo AI music generation API",
    url="https://github.com/sonilo-ai/sonilo-api-dashboard",
    python_requires=">=3.10",
    long_description=open("README.md").read() if __import__("os").path.exists("README.md") else "",
    long_description_content_type="text/markdown",
)
