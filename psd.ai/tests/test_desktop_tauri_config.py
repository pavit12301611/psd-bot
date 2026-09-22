"""desktop/src-tauri config - schema validity and binary-name consistency.

A real Fedora 44 run died in `tauri build` twice over things only visible
when every file is checked against the others:

  * `bundle.homepageUrl` is not a Tauri v2 key (it is `homepage`) - the CLI
    refuses to even start the build with "Additional properties are not
    allowed";
  * `mainBinaryName` RENAMES the built binary, so run.sh, the RPM spec and
    the .desktop WM class - all probing `psd-ai-desktop` - would never find
    it and would rebuild from scratch on every launch.

These tests pin the config to the Tauri v2 schema keys we use and keep the
binary name identical across tauri.conf.json, Cargo.toml, run.sh and the
packaging spec.
"""

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TAURI_DIR = REPO_ROOT / "desktop" / "src-tauri"
CONF = TAURI_DIR / "tauri.conf.json"
CARGO = TAURI_DIR / "Cargo.toml"
RUN_SH = REPO_ROOT / "run.sh"
SPEC = REPO_ROOT / "packaging" / "psd-ai.spec"
BINARY = "psd-ai-desktop"


@pytest.fixture(scope="module")
def conf():
    return json.loads(CONF.read_text(encoding="utf-8"))


def test_config_targets_the_tauri_v2_schema(conf):
    assert conf["$schema"] == "https://schema.tauri.app/config/2"
    assert conf["identifier"] == "ai.psd.desktop"


def test_no_invalid_v1_style_bundle_keys(conf):
    """homepageUrl broke a real install; these v1-isms must not creep back."""
    bundle = conf["bundle"]
    for banned in ("homepageUrl", "publisherName"):
        assert banned not in bundle
    assert bundle["homepage"].startswith("https://")
    # every bundle key we ship is a real Tauri v2 BundleConfig field
    allowed = {
        "active", "targets", "category", "shortDescription", "longDescription",
        "homepage", "license", "publisher", "icon", "linux",
    }
    unknown = set(bundle) - allowed
    assert not unknown, f"unvalidated bundle keys: {unknown}"


def test_no_main_binary_name_override(conf):
    """The binary must keep cargo's name so run.sh/RPM can find it."""
    assert "mainBinaryName" not in conf


def test_binary_name_is_consistent_everywhere(conf):
    cargo = CARGO.read_text(encoding="utf-8")
    assert re.search(rf'^name\s*=\s*"{BINARY}"', cargo, re.M)

    run_sh = RUN_SH.read_text(encoding="utf-8")
    assert f"target/release/{BINARY}" in run_sh

    spec = SPEC.read_text(encoding="utf-8")
    assert f"target/release/{BINARY}" in spec

    desktop_entry = (REPO_ROOT / "packaging" / "psd-ai.desktop").read_text(encoding="utf-8")
    assert f"StartupWMClass={BINARY}" in desktop_entry


def test_bundle_icons_exist_on_disk(conf):
    for icon in conf["bundle"]["icon"]:
        assert (TAURI_DIR / icon).is_file(), f"missing icon: {icon}"


def test_linux_bundle_targets_are_rpm_and_appimage(conf):
    assert set(conf["bundle"]["targets"]) == {"rpm", "appimage"}
    linux = conf["bundle"]["linux"]
    assert "rpm" in linux and "deb" in linux and "appimage" in linux
    assert any("python3" in dep for dep in linux["rpm"]["depends"])


def test_capabilities_grant_only_what_the_frontend_uses(conf):
    caps = json.loads((TAURI_DIR / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert caps["windows"] == ["main"]
    assert "core:default" in caps["permissions"]
    assert "opener:default" in caps["permissions"]
    # window controls used by the custom title bar (decorations: false)
    for perm in ("core:window:allow-start-dragging", "core:window:allow-minimize",
                 "core:window:allow-toggle-maximize", "core:window:allow-close"):
        assert perm in caps["permissions"]
    # the window label in the config must match the capability
    assert conf["app"]["windows"][0]["label"] == "main"
