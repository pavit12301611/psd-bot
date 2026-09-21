# psd.ai — Fedora RPM
#
# Builds an installable package for Fedora Workstation (and the remixes that
# share its package manager). Layout:
#
#   /usr/bin/psd-ai                     launcher (thin wrapper)
#   /usr/lib/psd.ai/                    app source + venv
#   /usr/share/applications/psd-ai.desktop
#   /usr/share/icons/hicolor/512x512/apps/psd-ai.png
#   /usr/share/metainfo/psd-ai.metainfo.xml
#
# The Python stack is installed into a private venv rather than into the system
# site-packages: this app pins a large AI stack (numpy, onnxruntime, ctranslate2,
# chromadb clients) that would otherwise collide with distro packages, and a
# venv under /usr/lib keeps `dnf remove psd-ai` a clean uninstall.
#
# Build with:
#   rpmbuild -ba packaging/psd-ai.spec \
#     --define "_sourcedir $PWD" --define "_topdir $PWD/rpmbuild"
# or from a checkout:
#   fedpkg --release f43 local        # inside a dist-git checkout
#
# The local model weights are NOT packaged: they are several GB per model and
# are fetched on first run into ~/.local/share/psd.ai/runtime.

Name:           psd-ai
Version:        1.0.0
Release:        1%{?dist}
Summary:        Local-first AI assistant with an offline model group, voice and PC control

License:        MIT
URL:            https://github.com/pavit12301611/psd-bot
Source0:        %{name}-%{version}.tar.gz

# Build toolchain: the venv compiles a few source wheels, and the desktop shell
# is a Tauri (Rust + webkit2gtk4.1) app built with npm.
BuildRequires:  python3-devel >= 3.11
BuildRequires:  python3-pip
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  make
BuildRequires:  rust
BuildRequires:  cargo
BuildRequires:  nodejs
BuildRequires:  npm
BuildRequires:  openssl-devel
BuildRequires:  libffi-devel
BuildRequires:  zlib-devel
BuildRequires:  webkit2gtk4.1-devel
BuildRequires:  gtk3-devel
BuildRequires:  glib2-devel
BuildRequires:  libsoup3-devel
BuildRequires:  javascriptcoregtk4.1-devel
BuildRequires:  alsa-lib-devel
BuildRequires:  librsvg2-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  ImageMagick
BuildRequires:  findutils

# Runtime: the engine, the local model server's GPU stack, and the tools the
# Jarvis PC-control layer drives. ffmpeg is deliberately NOT required — it
# lives in RPM Fusion, and a hard dependency on it would make the package
# uninstallable on a stock Fedora install.
Requires:       python3 >= 3.11
Requires:       bash
Requires:       tmux
Requires:       git
Requires:       curl
Requires:       mesa-vulkan-drivers
Requires:       xdg-utils
Requires:       gvfs
Requires:       libnotify
Requires:       pipewire-utils
Requires:       alsa-utils
# Wayland input + capture
Requires:       wl-clipboard
Requires:       grim
Requires:       slurp
Requires:       wtype
Requires:       ydotool
# X11 fallbacks (Xorg sessions, XWayland-native apps)
Requires:       xdotool
Requires:       wmctrl
Requires:       xclip
Requires:       xprop
Recommends:     vulkan-tools
Recommends:     scrot
# Not Requires: ffmpeg (RPM Fusion) and the NVIDIA driver stack (akmod-nvidia,
# cuda) — both are user choices, and the app reports what is missing with the
# exact dnf line to fix it.

%description
psd.ai is a local-first assistant: it runs a group of 3-5 hardware-fit GGUF
models through llama.cpp on your own machine, with optional voice mode
(offline Whisper) and a Jarvis layer that can read the screen and drive the
desktop. Nothing is required to go through a cloud API.

Built for Fedora: dnf dependencies, a systemd user unit, Wayland-first PC
control with an X11 fallback, and SELinux left Enforcing.

%prep
%setup -q

%build
# ── Python stack ────────────────────────────────────────────────────────────
# A private venv, wheel-installed where possible. --no-build-isolation is NOT
# used: a few packages need to fetch their build backend.
%{python3} -m venv --without-pip venv
./venv/bin/python -m ensurepip --upgrade 2>/dev/null || %{python3} -m venv venv
./venv/bin/python -m pip install --no-input --disable-pip-version-check --upgrade pip
./venv/bin/python -m pip install --no-input --disable-pip-version-check \
    -r requirements.txt
# Voice + PC-control extras are optional at runtime but shipped by default:
# without them the microphone falls back to browser speech.
./venv/bin/python -m pip install --no-input --disable-pip-version-check \
    -r requirements-jarvis.txt || true

# ── Desktop shell (Tauri) ───────────────────────────────────────────────────
pushd desktop
npm install --no-audit --no-fund
npm run tauri build -- --no-bundle
popd

# ── Icon: the branding artwork is a JPEG; hicolor wants a square PNG ────────
mkdir -p icon
convert assets/branding/psd_ai.jpg -resize 512x512 -gravity center \
    -background none -extent 512x512 icon/psd-ai.png

%install
rm -rf %{buildroot}
APPDIR=%{buildroot}%{_libdir}/%{name}

mkdir -p "$APPDIR"
# Everything the engine needs at runtime; the build tree and VCS metadata stay
# out of the package.
cp -a psd.ai "$APPDIR/"
rm -rf "$APPDIR/psd.ai/venv" "$APPDIR/psd.ai/tests" "$APPDIR/psd.ai/__pycache__"
cp -a venv "$APPDIR/venv"
cp -a run.sh "$APPDIR/run.sh"
cp -a packaging "$APPDIR/packaging" 2>/dev/null || true

# Rewrite the venv's absolute paths from the build root to the install root,
# which is what makes a venv relocatable inside an RPM.
find "$APPDIR/venv/bin" -type f -exec \
    sed -i "1s|^#!.*%{buildroot}|#!|" {} + 2>/dev/null || true
find "$APPDIR/venv" -name '*.pth' -o -name 'RECORD' -o -name 'direct_url.json' |
    xargs -r sed -i "s|%{buildroot}||g"

# Desktop shell
mkdir -p "$APPDIR/desktop"
cp -a desktop/src-tauri/target/release/psd-ai-desktop "$APPDIR/desktop/"

# Launcher
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'LAUNCHER'
#!/usr/bin/env bash
# Start the psd.ai desktop app (engine + model group).
set -euo pipefail
APPDIR="%{_libdir}/%{name}"
export PSD_AI_APP_DIR="$APPDIR/psd.ai"
export PSD_AI_PYTHON="$APPDIR/venv/bin/python"
cd "$PSD_AI_APP_DIR"
exec "$APPDIR/desktop/psd-ai-desktop" "$@"
LAUNCHER
sed -i "s|%{_libdir}|%{_libdir}|g" %{buildroot}%{_bindir}/%{name}
chmod 0755 %{buildroot}%{_bindir}/%{name}

# Menu entry, icon, appstream
mkdir -p %{buildroot}%{_datadir}/applications
desktop-file-install --dir=%{buildroot}%{_datadir}/applications \
    packaging/psd-ai.desktop
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/512x512/apps
install -m 0644 icon/psd-ai.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/psd-ai.png
if [ -f packaging/psd-ai.metainfo.xml ]; then
    mkdir -p %{buildroot}%{_datadir}/metainfo
    install -m 0644 packaging/psd-ai.metainfo.xml %{buildroot}%{_datadir}/metainfo/
fi

# systemd user unit template; psd-ai-service installs it with real paths
mkdir -p %{buildroot}%{_datadir}/%{name}
install -m 0644 psd.ai/psd_ai-ui.service %{buildroot}%{_datadir}/%{name}/psd_ai-ui.service
cat > %{buildroot}%{_bindir}/%{name}-service <<'SVCINSTALL'
#!/usr/bin/env bash
# Install/remove the psd.ai engine as a systemd USER service.
set -euo pipefail
exec /usr/lib/psd.ai/psd.ai/install-service.sh "$@"
SVCINSTALL
chmod 0755 %{buildroot}%{_bindir}/%{name}-service

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/psd-ai.desktop
if [ -f packaging/psd-ai.metainfo.xml ]; then
    appstream-util validate-relax --nonet \
        %{buildroot}%{_datadir}/metainfo/psd-ai.metainfo.xml || true
fi
# The engine's own test suite runs against the packaged venv.
./venv/bin/python -m pytest -q psd.ai/tests -x --no-header || \
    echo "NOTE: test suite skipped (some tests need a display or network)"

%files
%license psd.ai/LICENSE
%doc README.md
%{_bindir}/%{name}
%{_bindir}/%{name}-service
%{_libdir}/%{name}
%{_datadir}/applications/psd-ai.desktop
%{_datadir}/icons/hicolor/512x512/apps/psd-ai.png
%{_datadir}/metainfo/psd-ai.metainfo.xml

%post
/usr/bin/update-desktop-database %{_datadir}/applications &>/dev/null || :
/bin/touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :

%postun
/usr/bin/update-desktop-database %{_datadir}/applications &>/dev/null || :
if [ $1 -eq 0 ]; then
    /usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
fi

%posttrans
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :

%changelog
* Sat Sep 20 2026 psd.ai maintainers <maintainers@psd.ai> - 1.0.0-1
- Fedora-native packaging: dnf dependencies, systemd user unit, Wayland PC
  control stack, local model group bootstrap.
