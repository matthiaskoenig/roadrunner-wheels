# libRoadRunner develop wheels

A package index of the python wheels of the branch `develop` of [libRoadRunner](https://github.com/sys-bio/roadrunner) (`libroadrunner` and `rrplugins`), so that a project can be developed and tested against the next release of libRoadRunner before it is on PyPI.

```
https://matthiaskoenig.github.io/roadrunner-wheels/simple/
```

Every push to `develop` of sys-bio/roadrunner builds wheels for linux (manylinux_2_28 x86_64), macOS (arm64, x86_64) and windows (x86_64), one `abi3` wheel per platform for python 3.10 and newer, as artifacts which expire after 90 days. The workflow [`sync`](.github/workflows/sync.yml) runs daily, takes the wheels of the latest successful build, gives them a version of their own and keeps them as the assets of a [release](https://github.com/matthiaskoenig/roadrunner-wheels/releases) of this repository, from which it writes the index to GitHub Pages.

## Channels

The builds come in two channels, each with an index of its own:

| channel | what it is | index |
| --- | --- | --- |
| `develop` | the branch `develop` of [sys-bio/roadrunner](https://github.com/sys-bio/roadrunner), published daily | `https://matthiaskoenig.github.io/roadrunner-wheels/simple/` |
| `patched` | the branch `develop-patched` of the fork [matthiaskoenig/roadrunner](https://github.com/matthiaskoenig/roadrunner), i.e. that same `develop` with the patches we carry on top, published on demand | `https://matthiaskoenig.github.io/roadrunner-wheels/simple-patched/` |

The patches are the pull requests of ours which upstream has not merged yet. A project which needs one of them takes libroadrunner from the index of `patched` instead of the one of `develop`, with the same lines as below; everything else is the same, and the releases of the two channels are kept apart by the prefix of their tags (`v` and `patched-v`).

To publish a build of the patches: rebase `develop-patched` of the fork on `develop` of upstream with the patches on top, run `BuildRoadRunner` on it (Actions, "Run workflow", the branch `develop-patched`), and then dispatch [`sync`](.github/workflows/sync.yml) here with the channel `patched`. The build of the fork fails in its macos-intel jobs, which is why a build of `patched` is published although its run failed and although it has no macos-intel wheel: its release notes name the conclusion of the run, the platforms which are missing and the patches the build carries, and the check of the workflow skips the platforms without a wheel.

## Versions

All builds of `develop` have the version of the branch, e.g. `2.11.1`. A build of the channel `develop` is `<version>.dev<run>`, with `<run>` the number of the run of `BuildRoadRunner` which built it, or `<version>.post0.dev<run>` once `<version>` is released on PyPI and `develop` was not bumped yet. A build of the channel `patched` is the version of the build of `develop` the patches sit on with the local segment `+patched.<run of the fork>`, e.g. `2.11.1.dev655+patched.3`: the run numbers of the fork are a sequence of its own which starts at 1, so the number of the build of upstream is taken instead, which sorts a build of patches right above that build of upstream and below every later one. A resolution which asks for the newest build therefore never takes a build of patches by accident, while `libroadrunner>=2.11.1.dev655` is satisfied by one and a project which pins the version gets exactly it. These are development releases: they sort after every earlier build and before the next release, and pip and uv never install them unless asked to. The release notes of a build name the commit of roadrunner it was built from. Nothing but the version is changed: the version in the metadata, the name of the `.dist-info` directory, `RECORD`, and the tags of `WHEEL` set to the tags of the file name.

## Checks

The last job of `sync` installs the build it published from the live index, as below, on linux, macOS (arm64 and x86_64) and windows with python 3.14 and 3.15, once with uv (`uv sync` of a project with the source) and once with pip (`--extra-index-url`), and simulates a small model with it (`check_install.py`). The installed version must be the published one, so a failure shows a broken index as well as an index which does not serve the newest build. GitHub Pages caches its pages for some minutes, the check waits for the index to list the build first.

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

Replace `simple` with `simple-patched` in the url to take the builds of the channel `patched`. `tool.uv.sources` applies to the direct dependencies of a project only, so a project which gets libroadrunner through another package (e.g. sbmlsim) lists it among its own dependencies. The sources are not part of a built package: a wheel built with `uv build` depends on `libroadrunner>=2.11.0` from PyPI. `uv sync` installs the build of the lock, `uv lock --upgrade-package libroadrunner` moves it to the latest build, and `uv pip install --no-sources .` installs the project with the release.

## Use with pip

```bash
pip install --extra-index-url https://matthiaskoenig.github.io/roadrunner-wheels/simple/ "libroadrunner>=0.0.dev0"
```

The specifier with a development release lets pip take one for libroadrunner only (`--pre` would take them for every package).

## Commands

`sync.py` uses the standard library only and `gh` with a token in `GH_TOKEN`:

```bash
python sync.py publish [--channel develop|patched] [--run RUN_ID] [--base-run RUN_NUMBER] [--dry-run]
python sync.py index --out site                       # the index of the releases of every channel
python sync.py reversion WHEEL VERSION --out DIR      # give a wheel another version
python sync.py local-index DIR --out site             # an index of local wheels, file:// for tests
python check_install.py --installer uv|pip --python 3.14 --version VERSION [--index URL] [--wait SECONDS]
```

The script is MIT licensed ([LICENSE](LICENSE)). The wheels are the builds of sys-bio/roadrunner, under the licenses of libRoadRunner (Apache 2.0), which they include. This repository is not affiliated with the libRoadRunner project; a released libRoadRunner should always be preferred for anything but development.
