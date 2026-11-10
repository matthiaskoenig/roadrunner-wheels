"""Publish the wheels of the develop branch of libRoadRunner as a package index.

Every push to the branch `develop` of sys-bio/roadrunner builds the python
wheels of libroadrunner and rrplugins as artifacts of its workflow
`BuildRoadRunner`, all of them with the version of the branch, e.g. `2.11.1`,
and they expire after 90 days. This script gives the wheels of the latest
successful build a version of their own and keeps them as the assets of a
release of this repository, from which it writes a static package index
(PEP 503, with the core metadata of PEP 658), served by GitHub Pages.

The version of a build is `<version>.dev<run number>`, or
`<version>.post0.dev<run number>` once `<version>` is released on PyPI, a
development release which sorts after every earlier build and, as long as the
version of `develop` is not released, before the release. A development
release is never installed without being asked for (`--pre`, a specifier with
a pre-release or an index which has nothing else).

The wheels are the ones roadrunner built, with the version in their metadata,
the name of their `.dist-info` and their `RECORD` changed, and the tags of the
file `WHEEL` set to the ones of the name of the wheel.

Commands (standard library only, `gh` on the path with a token in `GH_TOKEN`):

    python sync.py publish [--run RUN_ID] [--dry-run]
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
WORKFLOW = "main.yml"
BRANCH = "develop"
PACKAGES = ("libroadrunner", "rrplugins")
# the artifacts of the wheels which an index serves: manylinux and not the
# wheels of a single distribution (`ubuntu-26`), which are no portable wheels
ARTIFACT = re.compile(r"^roadrunner-(?P<version>[^-]+)-python-wheels-(manylinux|macos|windows)")
# a build is published only with a wheel of every package for every platform
PLATFORMS = ("manylinux", "macosx_*_arm64", "macosx_*_x86_64", "win_amd64")
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "matthiaskoenig/roadrunner-wheels")
PAGES_URL = "https://matthiaskoenig.github.io/roadrunner-wheels"


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


def build_version(version: str, run_number: int, released: bool) -> str:
    """The version of the build `run_number` of the branch at `version`."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        raise ValueError(f"not a final release version: {version!r}")
    return f"{version}.post0.dev{run_number}" if released else f"{version}.dev{run_number}"


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


def write_index(files: list[IndexFile], out: Path) -> None:
    """Write the simple index of `files` into `out/simple` and a front page."""
    projects: dict[str, list[IndexFile]] = {}
    for f in files:
        projects.setdefault(WheelName.parse(f.filename).name.replace("_", "-"), []).append(f)
    simple = out / "simple"
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
    readme = Path(__file__).with_name("README.md")
    latest = {
        name: max(project_files, key=lambda f: f.filename).filename
        for name, project_files in projects.items()
    }
    (out / "index.html").write_text(
        '<!DOCTYPE html>\n<html>\n  <head>\n    <meta charset="utf-8">\n'
        "    <title>libRoadRunner develop wheels</title>\n  </head>\n  <body>\n"
        "    <h1>libRoadRunner develop wheels</h1>\n"
        f"    <p>Package index: <code>{PAGES_URL}/simple/</code></p>\n"
        f"    <p>Latest: {html.escape(', '.join(sorted(latest.values())))}</p>\n"
        f'    <p>See <a href="https://github.com/{REPOSITORY}">{REPOSITORY}</a>'
        " for how to use it.</p>\n"
        "  </body>\n</html>\n"
    )
    if readme.exists():
        shutil.copy(readme, out / "README.md")
    (out / ".nojekyll").write_text("")


def release_files() -> list[IndexFile]:
    """The wheels of every release of this repository with their hashes."""
    files = []
    for release in gh_items(f"repos/{REPOSITORY}/releases?per_page=100"):
        if release["draft"]:
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


def latest_run() -> dict:
    """The latest successful run of the workflow of a push to the branch."""
    runs = gh_api(
        f"repos/{UPSTREAM}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={BRANCH}&event=push&status=success&per_page=1"
    )["workflow_runs"]
    if not runs:
        raise SystemExit(f"no successful run of {WORKFLOW} on {BRANCH}")
    return runs[0]


def existing_tags() -> set[str]:
    """The tags of the releases of this repository."""
    return {r["tag_name"] for r in gh_items(f"repos/{REPOSITORY}/releases?per_page=100")}


def wheel_artifacts(run_id: int) -> dict[str, str]:
    """The wheel artifacts of a run, by name, with the version of the branch."""
    path = f"repos/{UPSTREAM}/actions/runs/{run_id}/artifacts?per_page=100"
    found = {}
    for artifact in gh_items(path, "artifacts"):
        match = ARTIFACT.match(artifact["name"])
        if match and not artifact["expired"]:
            found[artifact["name"]] = match["version"]
    if len(set(found.values())) != 1:
        raise SystemExit(f"run {run_id} has no wheel artifacts of one version: {found}")
    return found


def download_wheels(run_id: int, names: list[str], directory: Path) -> list[Path]:
    """Download wheel artifacts of a run, one wheel per package and platform."""
    for name in names:
        gh("run", "download", str(run_id), "-R", UPSTREAM, "-n", name, "-D", str(directory / name))
    wheels = [w for w in directory.rglob("*.whl") if WheelName.parse(w.name).name in PACKAGES]
    _check_complete(wheels)
    return wheels


def _check_complete(wheels: list[Path]) -> None:
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
    if missing:
        raise SystemExit(f"the build has no wheel for {missing}")
    versions = {WheelName.parse(w.name).version for w in wheels}
    if len(versions) != 1:
        raise SystemExit(f"the wheels of a build have several versions: {versions}")
    if len(found) != len(wheels):
        raise SystemExit("the build has several wheels of one package and platform")


def github_output(name: str, value: str) -> None:
    """Set an output of the step of a workflow (outside of one, print it)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def publish(run_id: int | None, dry_run: bool) -> None:
    """Publish the wheels of a run (the latest successful one by default)."""
    run = gh_api(f"repos/{UPSTREAM}/actions/runs/{run_id}") if run_id else latest_run()
    if (run["head_branch"], run["event"], run["conclusion"]) != (BRANCH, "push", "success"):
        raise SystemExit(f"run {run['id']} is no successful push to {BRANCH}")
    artifacts = wheel_artifacts(run["id"])
    base = next(iter(artifacts.values()))
    version = build_version(base, run["run_number"], released_on_pypi("libroadrunner", base))
    tag = f"v{version}"
    github_output("version", version)
    if tag in existing_tags():
        print(f"{tag} is published already")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wheels = download_wheels(run["id"], sorted(artifacts), tmp_path / "artifacts")
        if WheelName.parse(wheels[0].name).version != base:
            raise SystemExit(f"the wheels of run {run['id']} are not of version {base}")
        out = tmp_path / "wheels"
        assets = []
        for wheel in wheels:
            new = reversion(wheel, version, out)
            meta = new.with_name(new.name + ".metadata")
            meta.write_bytes(core_metadata(new))
            assets += [new, meta]
        notes = (
            f"The wheels of [{UPSTREAM}@{run['head_sha'][:10]}]"
            f"(https://github.com/{UPSTREAM}/commit/{run['head_sha']}) on `{BRANCH}`, "
            f"built by [run {run['run_number']}]({run['html_url']}), "
            f"version `{base}` renamed to `{version}`.\n\n"
            f"Install with `--extra-index-url {PAGES_URL}/simple/`."
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
    p.add_argument("--run", type=int, help="the id of a run instead of the latest")
    p.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("index", help="write the index of the releases")
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
        publish(args.run, args.dry_run)
    elif args.command == "index":
        write_index(release_files(), args.out)
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
        write_index(files, args.out)


if __name__ == "__main__":
    sys.exit(main())
