"""Check that a build of the index installs with uv and with pip and simulates.

The check installs libroadrunner as the README tells a user to, in a new
environment of a python managed by uv, and runs a simulation with it:

- `uv`: a project with libroadrunner pinned to the index by `tool.uv.sources`,
  locked and synced with `uv sync`;
- `pip`: `pip install --extra-index-url <index> "libroadrunner>=0.0.dev0"`.

The installed version must be `--version`, the build which was published, so a
check after a publication also shows that the index serves the newest build.
With `--wait`, the check first waits until the index lists the version (GitHub
Pages caches its pages for some minutes).

    python check_install.py --installer uv --python 3.14 --version 2.11.1.dev655
    python check_install.py --installer pip --python 3.15 --version 2.11.1.dev655 \\
        --index file:///path/to/site/simple/
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

INDEX = "https://matthiaskoenig.github.io/roadrunner-wheels/simple/"

# a species which decays with rate k: S1(t) = 10 exp(-k t)
SBML = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version2/core" level="3" version="2">
  <model id="decay">
    <listOfCompartments>
      <compartment id="c" size="1" constant="true"/>
    </listOfCompartments>
    <listOfSpecies>
      <species id="S1" compartment="c" initialAmount="10" hasOnlySubstanceUnits="true"
               boundaryCondition="false" constant="false"/>
    </listOfSpecies>
    <listOfParameters>
      <parameter id="k" value="0.1" constant="true"/>
    </listOfParameters>
    <listOfReactions>
      <reaction id="J0" reversible="false">
        <listOfReactants>
          <speciesReference species="S1" stoichiometry="1" constant="true"/>
        </listOfReactants>
        <kineticLaw>
          <math xmlns="http://www.w3.org/1998/Math/MathML">
            <apply><times/><ci>k</ci><ci>S1</ci></apply>
          </math>
        </kineticLaw>
      </reaction>
    </listOfReactions>
  </model>
</sbml>
"""


def smoke(version: str) -> None:
    """Check the installed version and simulate the decay (in the environment)."""
    import math

    import roadrunner

    installed = importlib.metadata.version("libroadrunner")
    if installed != version:
        raise SystemExit(f"installed libroadrunner {installed}, expected {version}")
    r = roadrunner.RoadRunner(SBML)
    result = r.simulate(0, 10, 11, ["time", "S1"])
    expected = 10 * math.exp(-1)
    if not math.isclose(result[-1, 1], expected, rel_tol=1e-5):
        raise SystemExit(f"S1(10) = {result[-1, 1]}, expected {expected}")
    print(
        f"libroadrunner {installed} on python {sys.version.split()[0]} "
        f"({sys.platform}): S1(10) = {result[-1, 1]:.6f}"
    )


def wait_for(index: str, version: str, seconds: float) -> None:
    """Wait until the page of libroadrunner of the index lists `version`."""
    url = index.rstrip("/") + "/libroadrunner/"
    deadline = time.monotonic() + seconds
    while True:
        try:
            request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=30) as response:
                if f"libroadrunner-{version}-" in response.read().decode():
                    return
        except urllib.error.URLError as err:
            print(f"{url}: {err}")
        if time.monotonic() > deadline:
            raise SystemExit(f"{url} does not list {version} after {seconds} s")
        time.sleep(20)


def run(*args: str | Path, cwd: Path | None = None) -> None:
    """Run a command, failing the check when it fails."""
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=cwd)


def venv_python(venv: Path) -> Path:
    """The python of a virtual environment."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def check(installer: str, python: str, version: str, index: str) -> None:
    """Install libroadrunner of the index with `installer` and run `smoke`."""
    env_vars = {"UV_NO_CACHE": "1", "UV_PYTHON_PREFERENCE": "only-managed"}
    os.environ.update(env_vars)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if installer == "uv":
            (work / "pyproject.toml").write_text(
                "[project]\n"
                'name = "check"\n'
                'version = "0.1.0"\n'
                f'requires-python = ">={python}"\n'
                'dependencies = ["libroadrunner>=2.11.0"]\n\n'
                "[tool.uv]\npackage = false\n\n"
                "[tool.uv.sources]\n"
                'libroadrunner = { index = "roadrunner-dev" }\n\n'
                "[[tool.uv.index]]\n"
                'name = "roadrunner-dev"\n'
                f'url = "{index}"\n'
                "explicit = true\n"
            )
            run("uv", "sync", "--python", python, cwd=work)
            venv = work / ".venv"
        else:
            venv = work / "venv"
            run("uv", "venv", "--seed", "--python", python, venv)
            run(
                venv_python(venv),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--extra-index-url",
                index,
                "libroadrunner>=0.0.dev0",
            )
        script = Path(__file__).resolve()
        run(venv_python(venv), "-I", script, "--smoke", "--version", version, cwd=work)


def main() -> None:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="the version which must be installed")
    parser.add_argument("--installer", choices=["uv", "pip"])
    parser.add_argument("--python", default="3.14")
    parser.add_argument("--index", default=INDEX)
    parser.add_argument("--wait", type=float, default=0, help="seconds to wait for the index")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.smoke:
        smoke(args.version)
        return
    if not args.installer:
        parser.error("--installer is required")
    if args.wait and args.index.startswith("http"):
        wait_for(args.index, args.version, args.wait)
    check(args.installer, args.python, args.version, args.index)


if __name__ == "__main__":
    main()
