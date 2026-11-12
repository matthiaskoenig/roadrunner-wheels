"""Publish the wheels of the develop branch of libRoadRunner as a package index.

Every push to the branch `develop` of sys-bio/roadrunner builds the python
wheels of libroadrunner and rrplugins as artifacts of its workflow
`BuildRoadRunner`, all of them with the version of the branch, e.g. `2.11.1`,
and they expire after 90 days. This script gives the wheels of the latest
successful build a version of their own and keeps them as the assets of a
release of this repository, from which it writes a static package index
(PEP 503, with the core metadata of PEP 658), served by GitHub Pages.

The builds come in two channels, each with an index of its own: `develop`, the
branch of sys-bio/roadrunner, and `patched`, the branch `develop-patched` of
the fork matthiaskoenig/roadrunner, which is that same develop with the
patches we carry on top (the pull requests of ours which are not merged yet)
and is built by a dispatch of `BuildRoadRunner` on the fork. A channel has its
own releases (the prefix of their tags) and its own index, so a wheel of the
fork is never mistaken for a build of upstream. Our own projects pin
libroadrunner to the index of `patched`, so every simulation of ours runs on
the patches.

The version of a build of `patched` is the one of the build of `develop` of
upstream the patches sit on with the local version segment `+patched.<run of
the fork>`, e.g. `2.11.1.dev655+patched.3`: the run numbers of the fork are a
sequence of its own which starts at 1, and taking them would sort a build of
patches below every build of upstream and below the floor of a project which
requires one, while the number of the build of upstream sorts it right above
that build and below every later one (`base_run_number`, `--base-run`). A
build of `patched` is published although its run failed and although it has
no wheel for every platform (the macos-intel jobs of the fork fail), with the
platforms it built and both stated in its release notes, since the patches
are tested by the project which uses them.

The version of a build of `develop` is `<version>.dev<run number>`, or
`<version>.post0.dev<run number>` once `<version>` is released on PyPI, a
development release which sorts after every earlier build and, as long as the
version of `develop` is not released, before the release. A development
release is never installed without being asked for (`--pre`, a specifier with
a pre-release or an index which has nothing else).

The wheels are the ones roadrunner built, with the version in their metadata,
the name of their `.dist-info` and their `RECORD` changed, and the tags of the
file `WHEEL` set to the ones of the name of the wheel.

Commands (standard library only, `gh` on the path with a token in `GH_TOKEN`):

    python sync.py publish [--channel develop|patched] [--run RUN_ID]
        [--base-run RUN_NUMBER] [--dry-run]
    python sync.py index --out SITE
    python sync.py reversion WHEEL VERSION --out DIR
    python sync.py local-index WHEELS_DIR --out SITE
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from pathlib import Path

UPSTREAM = "sys-bio/roadrunner"
FORK = "matthiaskoenig/roadrunner"
WORKFLOW = "main.yml"
BRANCH = "develop"
#: the branch of the fork: develop with the patches we carry on top
PATCHED_BRANCH = "develop-patched"
PACKAGES = ("libroadrunner", "rrplugins")
# the artifacts of the wheels which an index serves: manylinux and not the
# wheels of a single distribution (`ubuntu-26`), which are no portable wheels
ARTIFACT = re.compile(r"^roadrunner-(?P<version>[^-]+)-python-wheels-(manylinux|macos|windows)")
# a build is published only with a wheel of every package for every platform
PLATFORMS = ("manylinux", "macosx_*_arm64", "macosx_*_x86_64", "win_amd64")
# the runner on which the check of the workflow installs the wheel of a
# platform: a build without the wheel of a platform, which a channel of our
# patches publishes, is not checked on its runner (the output `runners`)
RUNNERS = {
    "manylinux": "ubuntu-latest",
    "macosx_*_arm64": "macos-latest",
    "macosx_*_x86_64": "macos-15-intel",
    "win_amd64": "windows-latest",
}
# the builds of `develop` of upstream which are searched for the one a build
# of patches sits on (`base_run_number`)
BASE_RUNS = 30
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "matthiaskoenig/roadrunner-wheels")
PAGES_URL = "https://matthiaskoenig.github.io/roadrunner-wheels"


@dataclass(frozen=True)
class Channel:
    """A branch whose builds this repository publishes as an index of its own."""

    #: the name of the channel, the argument of `--channel`
    name: str
    #: the repository the build runs in
    repo: str
    #: the branch which is built
    branch: str
    #: the event of the run which is published
    event: str
    #: the local version segment of every build, `None` for none
    local: str | None
    #: the directory of the index below the site
    index: str
    #: the prefix of the tag of a release of this channel
    tag_prefix: str
    #: what the channel is, for the index and the release notes
    description: str
    #: whether the run must have concluded `success`: a build of our patches
    #: is published when a job of a platform failed as well (the macos-intel
    #: jobs of the fork do), with the platforms it built
    require_success: bool = True
    #: whether the build must have a wheel of every package and platform
    require_complete: bool = True


CHANNELS: dict[str, Channel] = {
    channel.name: channel
    for channel in (
        Channel(
            name="develop",
            repo=UPSTREAM,
            branch=BRANCH,
            event="push",
            local=None,
            index="simple",
            tag_prefix="v",
            description=f"the branch `{BRANCH}` of {UPSTREAM}",
        ),
        Channel(
            name="patched",
            repo=FORK,
            branch=PATCHED_BRANCH,
            event="workflow_dispatch",
            local="patched",
            index="simple-patched",
            tag_prefix="patched-v",
            description=(
                f"the branch `{PATCHED_BRANCH}` of {FORK}, i.e. the branch "
                f"`{BRANCH}` of {UPSTREAM} with the patches we carry on top"
            ),
            require_success=False,
            require_complete=False,
        ),
    )
}


def gh(*args: str) -> str:
    """Run `gh` and return its output."""
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def gh_api(path: str) -> dict:
    """Return the JSON of a GET of the API of GitHub."""
    return json.loads(gh("api", path))


def gh_items(path: str, key: str | None = None) -> list[dict]:
    """Return the items of every page of a list of the API of GitHub."""
    pages = json.loads(gh("api", "--paginate", "--slurp", path))
    return [item for page in pages for item in (page[key] if key else page)]


# --- versions --------------------------------------------------------------


def released_on_pypi(package: str, version: str) -> bool:
    """Whether `version` of `package` is a release on PyPI."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30):
            return True
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise


def build_version(
    version: str, run_number: int, released: bool, local: str | None = None
) -> str:
    """The version of the build `run_number` of the branch at `version`.

    A channel with a local version segment (`patched`) appends it, so its
    builds are never confused with the builds of upstream and an index which
    serves both keeps them apart.
    """
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        raise ValueError(f"not a final release version: {version!r}")
    base = f"{version}.post0.dev{run_number}" if released else f"{version}.dev{run_number}"
    return f"{base}+{local}" if local else base


# --- wheels ----------------------------------------------------------------


@dataclass(frozen=True)
class WheelName:
    """The parts of the file name of a wheel (without a build tag)."""

    name: str
    version: str
    python: str
    abi: str
    platform: str

    @classmethod
    def parse(cls, filename: str) -> WheelName:
        """Parse `name-version-python-abi-platform.whl`."""
        parts = filename.removesuffix(".whl").split("-")
        if len(parts) != 5 or not filename.endswith(".whl"):
            raise ValueError(f"not a wheel without a build tag: {filename}")
        return cls(*parts)

    @property
    def filename(self) -> str:
        """The file name."""
        return f"{self.name}-{self.version}-{self.python}-{self.abi}-{self.platform}.whl"

    @property
    def tags(self) -> list[str]:
        """The expanded tags, as the file `WHEEL` lists them."""
        return [
            f"{p}-{a}-{pl}"
            for p in self.python.split(".")
            for a in self.abi.split(".")
            for pl in self.platform.split(".")
        ]


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def reversion(wheel: Path, version: str, out: Path) -> Path:
    """Write `wheel` with `version` into `out` and return the new wheel.

    The version of the core metadata, the names of the `.dist-info` and
    `.data` directories and the hashes of `RECORD` are changed, the tags of
    `WHEEL` are the ones of the file name; every other entry is kept as it is.
    """
    old = WheelName.parse(wheel.name)
    new = WheelName(old.name, version, old.python, old.abi, old.platform)
    prefixes = {
        f"{old.name}-{old.version}.dist-info/": f"{new.name}-{version}.dist-info/",
        f"{old.name}-{old.version}.data/": f"{new.name}-{version}.data/",
    }
    dist_info = f"{new.name}-{version}.dist-info/"
    target = out / new.filename
    out.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(wheel) as zin, zipfile.ZipFile(target, "w") as zout:
        for info in zin.infolist():
            name = info.filename
            for before, after in prefixes.items():
                if name.startswith(before):
                    name = after + name[len(before) :]
            if name == dist_info + "RECORD":
                continue
            data = zin.read(info)
            if name == dist_info + "METADATA":
                data, n = re.subn(
                    rb"^Version: .*$", f"Version: {version}".encode(), data, count=1, flags=re.M
                )
                if n != 1:
                    raise ValueError(f"no version in the metadata of {wheel.name}")
            elif name == dist_info + "WHEEL":
                lines = data.decode().splitlines()
                lines = [ln for ln in lines if ln and not ln.startswith("Tag:")]
                lines += [f"Tag: {tag}" for tag in new.tags]
                data = ("\n".join(lines) + "\n").encode()
            copy = zipfile.ZipInfo(name, date_time=info.date_time)
            copy.external_attr = info.external_attr
            copy.create_system = info.create_system
            copy.compress_type = info.compress_type
            zout.writestr(copy, data)
            if not name.endswith("/"):
                rows.append((name, _record_hash(data), str(len(data))))
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(rows)
        writer.writerow((dist_info + "RECORD", "", ""))
        record = zipfile.ZipInfo(dist_info + "RECORD", date_time=(2020, 1, 1, 0, 0, 0))
        record.external_attr = 0o644 << 16
        record.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(record, buffer.getvalue())
    return target


def core_metadata(wheel: Path) -> bytes:
    """The file `METADATA` of a wheel."""
    w = WheelName.parse(wheel.name)
    with zipfile.ZipFile(wheel) as z:
        return z.read(f"{w.name}-{w.version}.dist-info/METADATA")


def requires_python(metadata: bytes) -> str | None:
    """The `Requires-Python` of core metadata."""
    return BytesHeaderParser().parsebytes(metadata).get("Requires-Python")


# --- the index -------------------------------------------------------------


@dataclass(frozen=True)
class IndexFile:
    """A wheel of the index."""

    filename: str
    url: str
    sha256: str
    metadata_sha256: str | None
    requires_python: str | None


def _project_page(name: str, files: list[IndexFile]) -> str:
    links = []
    for f in sorted(files, key=lambda f: f.filename):
        attrs = [f'href="{html.escape(f.url)}#sha256={f.sha256}"']
        if f.requires_python:
            attrs.append(f'data-requires-python="{html.escape(f.requires_python)}"')
        if f.metadata_sha256:
            attrs.append(f'data-dist-info-metadata="sha256={f.metadata_sha256}"')
            attrs.append(f'data-core-metadata="sha256={f.metadata_sha256}"')
        links.append(f"    <a {' '.join(attrs)}>{html.escape(f.filename)}</a><br>")
    return (
        "<!DOCTYPE html>\n<html>\n  <head>\n"
        '    <meta name="pypi:repository-version" content="1.1">\n'
        f"    <title>Links for {name}</title>\n  </head>\n  <body>\n"
        f"    <h1>Links for {name}</h1>\n" + "\n".join(links) + "\n  </body>\n</html>\n"
    )


def write_index(
    files: list[IndexFile], out: Path, channel: Channel | None = None
) -> dict[str, str]:
    """Write the simple index of `files` into the directory of the channel.

    Args:
        files: the wheels of the channel.
        out: the directory of the site.
        channel: the channel, the one of upstream by default.

    Returns:
        The newest wheel of every package of the channel, by package.
    """
    channel = channel or CHANNELS["develop"]
    projects: dict[str, list[IndexFile]] = {}
    for f in files:
        projects.setdefault(WheelName.parse(f.filename).name.replace("_", "-"), []).append(f)
    simple = out / channel.index
    simple.mkdir(parents=True, exist_ok=True)
    for name, project_files in projects.items():
        (simple / name).mkdir(exist_ok=True)
        (simple / name / "index.html").write_text(_project_page(name, project_files))
    entries = "\n".join(f'    <a href="{n}/">{n}</a><br>' for n in sorted(projects))
    (simple / "index.html").write_text(
        "<!DOCTYPE html>\n<html>\n  <head>\n"
        '    <meta name="pypi:repository-version" content="1.1">\n'
        "    <title>Simple index</title>\n  </head>\n  <body>\n"
        f"{entries}\n  </body>\n</html>\n"
    )
    return {
        name: max(project_files, key=lambda f: f.filename).filename
        for name, project_files in projects.items()
    }


def write_front_page(latest: dict[str, dict[str, str]], out: Path) -> None:
    """Write the front page of the site, the channels with their newest wheels.

    Args:
        latest: the newest wheel of every package, by channel and package.
        out: the directory of the site.
    """
    sections = []
    for name, wheels in latest.items():
        channel = CHANNELS[name]
        # the backticks of the description are markdown, for the release notes
        description = html.escape(channel.description.replace("`", ""))
        newest = html.escape(", ".join(sorted(wheels.values()))) or "no build yet"
        sections.append(
            f"    <h2>{html.escape(name)}</h2>\n"
            f"    <p>{description}</p>\n"
            f"    <p>Package index: <code>{PAGES_URL}/{channel.index}/</code></p>\n"
            f"    <p>Latest: {newest}</p>\n"
        )
    readme = Path(__file__).with_name("README.md")
    (out / "index.html").write_text(
        '<!DOCTYPE html>\n<html>\n  <head>\n    <meta charset="utf-8">\n'
        "    <title>libRoadRunner develop wheels</title>\n  </head>\n  <body>\n"
        "    <h1>libRoadRunner develop wheels</h1>\n"
        + "".join(sections)
        + f'    <p>See <a href="https://github.com/{REPOSITORY}">{REPOSITORY}</a>'
        " for how to use it.</p>\n"
        "  </body>\n</html>\n"
    )
    if readme.exists():
        shutil.copy(readme, out / "README.md")
    (out / ".nojekyll").write_text("")


def release_files(channel: Channel | None = None) -> list[IndexFile]:
    """The wheels of every release of a channel with their hashes.

    Args:
        channel: the channel, whose releases are those whose tag carries its
            prefix; the one of upstream by default.
    """
    channel = channel or CHANNELS["develop"]
    other = sorted(
        (c.tag_prefix for c in CHANNELS.values() if c is not channel), key=len, reverse=True
    )
    files = []
    for release in gh_items(f"repos/{REPOSITORY}/releases?per_page=100"):
        if release["draft"]:
            continue
        tag = release["tag_name"]
        if not tag.startswith(channel.tag_prefix) or any(
            tag.startswith(prefix) for prefix in other
        ):
            continue
        assets = {a["name"]: a for a in release["assets"]}
        for name, asset in assets.items():
            if not name.endswith(".whl"):
                continue
            meta = assets.get(name + ".metadata")
            meta_bytes = _download(meta["browser_download_url"]) if meta else None
            files.append(
                IndexFile(
                    filename=name,
                    url=asset["browser_download_url"],
                    sha256=_asset_sha256(asset),
                    metadata_sha256=hashlib.sha256(meta_bytes).hexdigest() if meta_bytes else None,
                    requires_python=requires_python(meta_bytes) if meta_bytes else None,
                )
            )
    return files


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def _asset_sha256(asset: dict) -> str:
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.removeprefix("sha256:")
    return hashlib.sha256(_download(asset["browser_download_url"])).hexdigest()


# --- publishing ------------------------------------------------------------


def latest_run(channel: Channel) -> dict:
    """The latest run of the workflow of the branch of a channel.

    The latest successful one, or, for a channel which does not require a
    successful run (`patched`), the latest completed one.
    """
    status = "success" if channel.require_success else "completed"
    runs = gh_api(
        f"repos/{channel.repo}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={channel.branch}&event={channel.event}&status={status}&per_page=1"
    )["workflow_runs"]
    if not runs:
        raise SystemExit(f"no {status} run of {WORKFLOW} on {channel.branch}")
    return runs[0]


def existing_tags() -> set[str]:
    """The tags of the releases of this repository."""
    return {r["tag_name"] for r in gh_items(f"repos/{REPOSITORY}/releases?per_page=100")}


def wheel_artifacts(channel: Channel, run_id: int) -> dict[str, str]:
    """The wheel artifacts of a run, by name, with the version of the branch."""
    path = f"repos/{channel.repo}/actions/runs/{run_id}/artifacts?per_page=100"
    found = {}
    for artifact in gh_items(path, "artifacts"):
        match = ARTIFACT.match(artifact["name"])
        if match and not artifact["expired"]:
            found[artifact["name"]] = match["version"]
    if len(set(found.values())) != 1:
        raise SystemExit(f"run {run_id} has no wheel artifacts of one version: {found}")
    return found


def download_wheels(
    channel: Channel, run_id: int, names: list[str], directory: Path
) -> tuple[list[Path], list[str]]:
    """Download wheel artifacts of a run, one wheel per package and platform.

    Returns:
        The wheels and the packages and platforms the build has no wheel for,
        which is empty unless the channel publishes an incomplete build.
    """
    for name in names:
        gh(
            "run",
            "download",
            str(run_id),
            "-R",
            channel.repo,
            "-n",
            name,
            "-D",
            str(directory / name),
        )
    wheels = [w for w in directory.rglob("*.whl") if WheelName.parse(w.name).name in PACKAGES]
    missing = _check_complete(wheels, channel.require_complete)
    return wheels, missing


def _check_complete(wheels: list[Path], required: bool = True) -> list[str]:
    """Check that a build has a wheel of every package and platform.

    Args:
        wheels: the wheels of the build.
        required: whether a missing wheel is an error; a channel of our
            patches publishes the platforms which built.

    Returns:
        The packages and platforms which are missing, `package/platform`.

    Raises:
        SystemExit: when a wheel is missing and `required`, and always for
            wheels of several versions or two wheels of one platform.
    """
    from fnmatch import fnmatch

    found = {(WheelName.parse(w.name).name, WheelName.parse(w.name).platform) for w in wheels}
    missing = [
        (package, platform)
        for package in PACKAGES
        for platform in PLATFORMS
        if not any(
            p == package and any(fnmatch(pl, platform + "*") for pl in plats.split("."))
            for p, plats in found
        )
    ]
    names = [f"{package}/{platform}" for package, platform in missing]
    if missing and required:
        raise SystemExit(f"the build has no wheel for {names}")
    if missing:
        print(f"the build has no wheel for {names}, publishing the rest")
    versions = {WheelName.parse(w.name).version for w in wheels}
    if len(versions) != 1:
        raise SystemExit(f"the wheels of a build have several versions: {versions}")
    if len(found) != len(wheels):
        raise SystemExit("the build has several wheels of one package and platform")
    return names


def github_output(name: str, value: str) -> None:
    """Set an output of the step of a workflow (outside of one, print it)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def base_run_number(channel: Channel, head_sha: str, base_run: int | None = None) -> int:
    """The number of the build of upstream a build of patches sits on.

    The run numbers of a fork are a sequence of its own which starts at 1, so
    the number of the run which built the patches would sort the build below
    every build of upstream and below the floor of a project which requires
    one. The version of a build of patches takes the number of the build of
    `develop` of upstream the patches sit on instead: the build of the merge
    base of the branch, or the newest build of `develop` which the branch
    contains, and the run of the fork goes into the local version segment
    (`build_number`). The version then sorts right above that build of
    upstream and below every later one, so a resolution which asks for the
    newest build never takes a build of patches, while a project which pins
    the version gets it and the floor of upstream is satisfied.

    Args:
        channel: the channel of the build, a channel of a fork.
        head_sha: the commit which was built.
        base_run: the number to use instead of looking it up.

    Returns:
        The run number of the build of upstream.

    Raises:
        SystemExit: when no build of `develop` which the branch contains is
            among the `BASE_RUNS` newest ones.
    """
    if base_run is not None:
        return base_run
    owner = channel.repo.split("/")[0]
    comparison = gh_api(f"repos/{UPSTREAM}/compare/{BRANCH}...{owner}:{head_sha}")
    merge_base = comparison["merge_base_commit"]["sha"]
    runs = gh_api(
        f"repos/{UPSTREAM}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={BRANCH}&event=push&status=success&per_page={BASE_RUNS}"
    )["workflow_runs"]
    for run in runs:
        if run["head_sha"] == merge_base:
            return run["run_number"]
    # the merge base was not built: the newest build it contains, i.e. the
    # newest commit of `develop` with a build which is an ancestor of it
    for run in runs:
        status = gh_api(f"repos/{UPSTREAM}/compare/{run['head_sha']}...{merge_base}")
        if status.get("status") in ("identical", "ahead"):
            return run["run_number"]
    raise SystemExit(
        f"no build of `{BRANCH}` among the {BASE_RUNS} newest ones which "
        f"{merge_base[:10]} contains, give its number with --base-run"
    )


def build_number(
    channel: Channel, run: dict, base_run: int | None = None
) -> tuple[int, str | None]:
    """The development number and the local version segment of a build.

    The number of the run for a channel of upstream, and for a channel of a
    fork the number of the build of upstream the patches sit on with the run
    of the fork in the local segment, e.g. `2.11.1.dev655+patched.3`, see
    `base_run_number`.

    Args:
        channel: the channel of the build.
        run: the run which built it.
        base_run: the number of the build of upstream to use, for a fork.

    Returns:
        The number of the development release and the local segment.
    """
    if channel.repo == UPSTREAM:
        return run["run_number"], channel.local
    number = base_run_number(channel, run["head_sha"], base_run)
    run_number = run["run_number"]
    local = f"{channel.local}.{run_number}" if channel.local else str(run_number)
    return number, local


def patch_commits(channel: Channel, head_sha: str) -> list[str]:
    """The commits of a channel on top of the branch of upstream.

    Returns:
        The first line of the message of every commit which the build carries
        on top of `develop` of upstream, empty for the channel of upstream and
        when the comparison is not available.
    """
    if channel.repo == UPSTREAM:
        return []
    owner = channel.repo.split("/")[0]
    try:
        comparison = gh_api(f"repos/{UPSTREAM}/compare/{BRANCH}...{owner}:{head_sha}")
    except subprocess.CalledProcessError:
        return []
    return [c["commit"]["message"].split("\n")[0] for c in comparison.get("commits", [])]


def publish(
    channel: Channel, run_id: int | None, dry_run: bool, base_run: int | None = None
) -> None:
    """Publish the wheels of a run of a channel.

    Args:
        channel: the channel to publish.
        run_id: the run to publish, the latest one of the channel by default.
        dry_run: print what would be published and publish nothing.
        base_run: the number of the build of upstream a build of patches sits
            on, looked up by `base_run_number` by default.

    Raises:
        SystemExit: when the run is not one of the channel, when it did not
            succeed and the channel requires it, or when its wheels are not
            the ones of the version of the branch.
    """
    run = (
        gh_api(f"repos/{channel.repo}/actions/runs/{run_id}")
        if run_id
        else latest_run(channel)
    )
    if (run["head_branch"], run["event"]) != (channel.branch, channel.event):
        raise SystemExit(
            f"run {run['id']} is no {channel.event} of {channel.branch}"
        )
    if run["conclusion"] != "success":
        if channel.require_success:
            raise SystemExit(
                f"run {run['id']} of {channel.branch} concluded {run['conclusion']}"
            )
        print(
            f"run {run['id']} concluded {run['conclusion']}, "
            "publishing the wheels it built"
        )
    artifacts = wheel_artifacts(channel, run["id"])
    base = next(iter(artifacts.values()))
    number, local = build_number(channel, run, base_run)
    version = build_version(
        base,
        number,
        released_on_pypi("libroadrunner", base),
        local,
    )
    tag = f"{channel.tag_prefix}{version}"
    github_output("version", version)
    github_output("index", channel.index)
    if tag in existing_tags():
        print(f"{tag} is published already")
        # the build was checked when it was published
        github_output("runners", " ".join(RUNNERS.values()))
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wheels, missing = download_wheels(
            channel, run["id"], sorted(artifacts), tmp_path / "artifacts"
        )
        if WheelName.parse(wheels[0].name).version != base:
            raise SystemExit(f"the wheels of run {run['id']} are not of version {base}")
        out = tmp_path / "wheels"
        assets = []
        for wheel in wheels:
            new = reversion(wheel, version, out)
            meta = new.with_name(new.name + ".metadata")
            meta.write_bytes(core_metadata(new))
            assets += [new, meta]
        incomplete = (
            f"\n\nThe build has no wheel for {', '.join(missing)}: the jobs "
            "of these platforms did not build one."
            if missing
            else ""
        )
        patches = patch_commits(channel, run["head_sha"])
        carried = (
            "\n\nThe patches on top of `develop` of upstream:\n"
            + "\n".join(f"- {message}" for message in patches)
            if patches
            else ""
        )
        concluded = (
            "" if run["conclusion"] == "success" else f" (concluded `{run['conclusion']}`)"
        )
        notes = (
            f"The wheels of [{channel.repo}@{run['head_sha'][:10]}]"
            f"(https://github.com/{channel.repo}/commit/{run['head_sha']}) on "
            f"`{channel.branch}`, built by [run {run['run_number']}]"
            f"({run['html_url']}){concluded}, "
            f"version `{base}` renamed to `{version}`.\n\n"
            f"The channel `{channel.name}` is {channel.description}.\n\n"
            f"Install with `--extra-index-url {PAGES_URL}/{channel.index}/`."
            f"{carried}{incomplete}"
        )
        # the runners of the platforms the build has a wheel for
        github_output(
            "runners",
            " ".join(
                runner
                for platform, runner in RUNNERS.items()
                if not any(name.endswith(f"/{platform}") for name in missing)
            ),
        )
        print(f"publishing {tag}: {', '.join(sorted(a.name for a in assets))}")
        if dry_run:
            return
        gh(
            "release",
            "create",
            tag,
            "-R",
            REPOSITORY,
            "--title",
            version,
            "--notes",
            notes,
            *map(str, assets),
        )


def main() -> None:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("publish", help="publish the latest successful build")
    p.add_argument(
        "--channel",
        choices=sorted(CHANNELS),
        default="develop",
        help="the channel to publish, the branch of upstream by default",
    )
    p.add_argument("--run", type=int, help="the id of a run instead of the latest")
    p.add_argument(
        "--base-run",
        type=int,
        help=(
            "the number of the build of develop of upstream a build of "
            "patches sits on, looked up by default"
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("index", help="write the index of the releases of every channel")
    i.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("reversion", help="give a wheel another version")
    r.add_argument("wheel", type=Path)
    r.add_argument("version")
    r.add_argument("--out", type=Path, required=True)
    li = sub.add_parser("local-index", help="write an index of local wheels, for tests")
    li.add_argument("wheels", type=Path)
    li.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "publish":
        publish(CHANNELS[args.channel], args.run, args.dry_run, args.base_run)
    elif args.command == "index":
        latest = {
            name: write_index(release_files(channel), args.out, channel)
            for name, channel in CHANNELS.items()
        }
        write_front_page(latest, args.out)
    elif args.command == "reversion":
        print(reversion(args.wheel, args.version, args.out))
    elif args.command == "local-index":
        files = []
        for wheel in sorted(args.wheels.glob("*.whl")):
            meta = core_metadata(wheel)
            target = args.out / "files" / wheel.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(wheel, target)
            target.with_name(wheel.name + ".metadata").write_bytes(meta)
            files.append(
                IndexFile(
                    filename=wheel.name,
                    url=f"../../files/{wheel.name}",
                    sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    metadata_sha256=hashlib.sha256(meta).hexdigest(),
                    requires_python=requires_python(meta),
                )
            )
        write_front_page({"develop": write_index(files, args.out)}, args.out)


if __name__ == "__main__":
    sys.exit(main())
