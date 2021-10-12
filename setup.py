# -*- coding: utf-8 -*-

from setuptools import setup

with open("README.md", "r", encoding="utf-8") as f:
    readme = f.read()

INSTALL_REQUIRE = [
    "feast>=0.13.0",
    "psycopg2-binary>=2.8.3",
]

DEV_REQUIRE = [
    "flake8",
    "black==19.10b0",
    "isort>=5",
    "mypy==0.790",
    "build==0.7.0",
    "twine==3.4.2",
]

setup(
    name="feast-postgres",
    version="0.1.0",
    author="Gunnar Sv Sigurbjörnsson",
    author_email="gunnar.sigurbjornsson@gmail.com",
    description="PostgreSQL online and offline store for Feast",
    long_description=readme,
    long_description_content_type="text/markdown",
    python_requires=">=3.7.0",
    url="https://github.com/nossrannug/feast-postgres",
    project_urls={
        "Bug Tracker": "https://github.com/nossrannug/feast-postgres/issues",
    },
    license='Apache License, Version 2.0',
    packages=["feast_postgres"],
    install_requires=INSTALL_REQUIRE,
    extras_require={
        "dev": DEV_REQUIRE,
    },
    keywords=("feast featurestore postgres offlinestore onlinestore"),
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
    ],
)
