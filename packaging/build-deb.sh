#!/usr/bin/env bash
# Build dist/system-update_<version>_all.deb from this source tree.
#
#   packaging/build-deb.sh
#   DEB_MAINTAINER="Name <email>" packaging/build-deb.sh
set -euo pipefail
umask 022

cd "$(dirname "$0")/.."

APP_ID=io.github.wakedog.SystemUpdate
PACKAGE=system-update
VERSION=$(python3 -c 'import system_update; print(system_update.__version__)')
name=$(git config user.name 2>/dev/null || true)
email=$(git config user.email 2>/dev/null || true)
MAINTAINER=${DEB_MAINTAINER:-"${name:-Bryan} <${email:-${USER:-bryan}@localhost}>"}

STAGE=build/deb/${PACKAGE}_${VERSION}_all
OUTPUT=dist/${PACKAGE}_${VERSION}_all.deb
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" dist

# Application code, as a private module directory.
while IFS= read -r -d '' file; do
    install -D -m 0644 "$file" "$STAGE/usr/share/$PACKAGE/$file"
done < <(find system_update -type f \( -name '*.py' -o -name '*.css' \) -not -path '*/__pycache__/*' -print0)

# The privileged helper lives where the polkit policy expects it.
install -D -m 0755 system_update/helper.py "$STAGE/usr/libexec/$PACKAGE/system-update-helper"

install -d -m 0755 "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/$PACKAGE" <<'EOF'
#!/usr/bin/python3 -I
import sys

sys.path.insert(0, "/usr/share/system-update")

from system_update.main import main

sys.exit(main())
EOF
chmod 0755 "$STAGE/usr/bin/$PACKAGE"

install -D -m 0644 "data/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
install -D -m 0644 "data/$APP_ID.metainfo.xml" "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
install -D -m 0644 "data/$APP_ID.policy" "$STAGE/usr/share/polkit-1/actions/$APP_ID.policy"
install -D -m 0644 "data/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -D -m 0644 "data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
    "$STAGE/usr/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
install -D -m 0644 data/system-update.bash-completion "$STAGE/usr/share/bash-completion/completions/$PACKAGE"
install -d -m 0755 "$STAGE/usr/share/man/man1" "$STAGE/usr/share/doc/$PACKAGE"
gzip -9n < data/system-update.1 > "$STAGE/usr/share/man/man1/$PACKAGE.1.gz"

{
    echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    echo "Upstream-Name: $PACKAGE"
    echo
    echo "Files: *"
    echo "Copyright: 2026 wakedog"
    echo "License: MIT"
    sed -e '1,/^Permission/{/^Permission/!d}' -e 's/^$/./' -e 's/^/ /' LICENSE
} > "$STAGE/usr/share/doc/$PACKAGE/copyright"

cat <<EOF | gzip -9n > "$STAGE/usr/share/doc/$PACKAGE/changelog.gz"
$PACKAGE ($VERSION) unstable; urgency=medium

  * First release as a desktop app, rebuilt from the system-update.sh script.

 -- $MAINTAINER  $(date -R)
EOF
chmod 0644 "$STAGE/usr/share/man/man1/$PACKAGE.1.gz" "$STAGE/usr/share/doc/$PACKAGE/"*

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
Depends: python3 (>= 3.10), python3-gi (>= 3.42), gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.5), pkexec | policykit-1, apt, systemd
Suggests: snapd, flatpak, fwupd, needrestart
Section: admin
Priority: optional
Homepage: https://github.com/wakedog/system-update
Description: keep Ubuntu, snaps, Flatpak apps and firmware up to date
 System Update installs updates for system packages (APT), snaps, Flatpak
 apps and device firmware (fwupd) in one run, then removes unused packages,
 cached downloads, old snap revisions and unused runtimes, and trims the
 system journal.
 .
 It includes a GTK 4 desktop app with live progress and a command-line
 interface that accepts the options of the older system-update.sh script.
 Updates are installed by a small helper with a fixed set of tasks,
 authorized through polkit.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then
    py3compile -p system-update /usr/share/system-update
fi
EOF

cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p system-update
else
    find /usr/share/system-update -type d -name __pycache__ -prune -exec rm -rf {} +
fi
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = purge ]; then
    rm -rf /var/lib/system-update
fi
EOF
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

dpkg-deb --build --root-owner-group "$STAGE" "$OUTPUT" >/dev/null
echo "Built $OUTPUT"
