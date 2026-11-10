# libRoadRunner develop wheels

A package index of the python wheels of the branch `develop` of [libRoadRunner](https://github.com/sys-bio/roadrunner) (`libroadrunner` and `rrplugins`), so that a project can be developed and tested against the next release of libRoadRunner before it is on PyPI.

```
https://matthiaskoenig.github.io/roadrunner-wheels/simple/
```

Every push to `develop` of sys-bio/roadrunner builds wheels for linux (manylinux_2_28 x86_64), macOS (arm64, x86_64) and windows (x86_64), one `abi3` wheel per platform for python 3.10 and newer, as artifacts which expire after 90 days. The workflow [`sync`](.github/workflows/sync.yml) runs daily, takes the wheels of the latest successful build, gives them a version of their own and keeps them as the assets of a [release](https://github.com/matthiaskoenig/roadrunner-wheels/releases) of this repository, from which it writes the index to GitHub Pages.

## Versions

All builds of `develop` have the version of the branch, e.g. `2.11.1`. A build here is `<version>.dev<run>`, with `<run>` the number of the run of `BuildRoadRunner` which built it, or `<version>.post0.dev<run>` once `<version>` is released on PyPI and `develop` was not bumped yet. These are development releases: they sort after every earlier build and before the next release, and pip and uv never install them unless asked to. The release notes of a build name the commit of roadrunner it was built from. Nothing but the version is changed: the version in the metadata, the name of the `.dist-info` directory, `RECORD`, and the tags of `WHEEL` set to the tags of the file name.

## Use with uv

The released packages keep depending on the libroadrunner of PyPI; only the development environment (the lock) takes the develop wheels. Declare `libroadrunner` as a dependency of the project and pin it to the index:

```toml
[project]
dependencies = [
    "libroadrunner>=2.11.0",
]

[tool.uv.sources]
libroadrunner = { index = "roadrunner-dev" }

[[tool.uv.index]]
name = "roadrunner-dev"
url = "https://matthiaskoenig.github.io/roadrunner-wheels/simple/"
explicit = true
```

`tool.uv.sources` applies to the direct dependencies of a project only, so a project which gets libroadrunner through another package (e.g. sbmlsim) lists it among its own dependencies. The sources are not part of a built package: a wheel built with `uv build` depends on `libroadrunner>=2.11.0` from PyPI. `uv sync` installs the build of the lock, `uv lock --upgrade-package libroadrunner` moves it to the latest build, and `uv pip install --no-sources .` installs the project with the release.

## Use with pip

```bash
pip install --extra-index-url https://matthiaskoenig.github.io/roadrunner-wheels/simple/ "libroadrunner>=0.0.dev0"
```

The specifier with a development release lets pip take one for libroadrunner only (`--pre` would take them for every package).

## Commands

`sync.py` uses the standard library only and `gh` with a token in `GH_TOKEN`:

```bash
python sync.py publish [--run RUN_ID] [--dry-run]   # publish the latest build (or a run)
python sync.py index --out site                       # the index of the releases
python sync.py reversion WHEEL VERSION --out DIR      # give a wheel another version
python sync.py local-index DIR --out site             # an index of local wheels, file:// for tests
```

The script is MIT licensed ([LICENSE](LICENSE)). The wheels are the builds of sys-bio/roadrunner, under the licenses of libRoadRunner (Apache 2.0), which they include. This repository is not affiliated with the libRoadRunner project; a released libRoadRunner should always be preferred for anything but development.
