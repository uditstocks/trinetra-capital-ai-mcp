"""The deployed image must contain everything the server imports.

Two deploys were burned by this class of bug and neither was catchable by any
other test: the code was correct, the tests were green, and the container was
missing a file.

  1. `mcp>=1.2.0` had no upper bound, so a fresh install picked up mcp 2.0.0,
     which removed the module we import.
  2. `trinetra_web/` was added after the Dockerfile was written and never copied
     into the image.

These check the packaging itself, not the code.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def first_party_packages() -> set[str]:
    """Top-level packages in the repo that the server could import."""
    return {
        path.name for path in ROOT.iterdir()
        if path.is_dir()
        and (path / "__init__.py").exists()
        and not path.name.startswith((".", "test"))
    }


def test_dockerfile_copies_every_first_party_package():
    copied = set(re.findall(r"^COPY\s+(\w+)/", DOCKERFILE, re.M))
    missing = first_party_packages() - copied
    assert not missing, (
        f"{sorted(missing)} exist in the repo but are not COPYed into the image. "
        "The container will crash on import at startup."
    )


def test_dockerfile_copies_what_migrations_need():
    """alembic upgrade runs at release; without these the deploy dies mid-migration."""
    assert re.search(r"^COPY\s+migrations/", DOCKERFILE, re.M), "migrations/ not copied"
    assert re.search(r"^COPY\s+alembic\.ini", DOCKERFILE, re.M), "alembic.ini not copied"


def test_pyproject_lists_every_first_party_package():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([\w.]+)"', pyproject.split("packages = [")[1].split("]")[0]))
    top_level = {name.split(".")[0] for name in listed}
    missing = first_party_packages() - top_level
    assert not missing, f"{sorted(missing)} missing from pyproject packages"

    # A listed package that does not exist breaks `pip install .` just as badly.
    for name in listed:
        assert (ROOT / pathlib.Path(*name.split("."))).is_dir(), (
            f"pyproject lists {name!r}, which is not a package"
        )


# --------------------------------------------------------------------------- #
# dependency pinning
# --------------------------------------------------------------------------- #
REQUIREMENTS = ("requirements-mcp.txt", "requirements-server.txt")


def _requirement_lines(filename: str) -> list[str]:
    text = (ROOT / filename).read_text(encoding="utf-8")
    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("#", "-r"))
    ]


@pytest.mark.parametrize("filename", REQUIREMENTS)
def test_every_server_dependency_has_an_upper_bound(filename):
    """An unbounded dependency means the deployed code is not the tested code.

    mcp 2.0 removed mcp.server.fastmcp; `mcp>=1.2.0` happily installed it and the
    container could not start.
    """
    unbounded = [
        line for line in _requirement_lines(filename)
        if "<" not in line and "==" not in line and "~=" not in line
    ]
    assert not unbounded, (
        f"{filename}: no upper bound on {unbounded}. A major release would deploy "
        "untested code."
    )


def test_mcp_is_pinned_below_the_version_that_removed_fastmcp():
    line = next(l for l in _requirement_lines("requirements-mcp.txt") if l.startswith("mcp"))
    assert "<2" in line, f"mcp must stay below 2.0 (got {line!r}) — 2.0 has no fastmcp"


def test_the_server_image_does_not_ship_the_llm_stack():
    """The bare requirements.txt is the legacy CLI's. Installing it would put
    LangChain and the NVIDIA/Groq clients in a server that never calls an LLM."""
    instructions = [
        line for line in DOCKERFILE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    offenders = [
        line for line in instructions
        if re.search(r"(?<![-\w])requirements\.txt", line)
    ]
    assert not offenders, f"the Dockerfile pulls in the CLI's dependencies: {offenders}"
