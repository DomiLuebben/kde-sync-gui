# Maintainer: Domi <domi@local>
pkgname=kde-sync-gui
pkgver=1.4.0
pkgrel=1
pkgdesc="A modern PyQt6 GUI for KDE Plasma Backup & Sync via any synced cloud folder (Nextcloud, Google Drive, OneDrive, Dropbox, Syncthing, ...)"
arch=('any')
url="https://github.com/DomiLuebben/kde-sync-gui"
license=('GPL')
depends=('python-pyqt6' 'qt6-base' 'bash' 'tar')
source=("main.py"
        "config_manager.py"
        "style.qss"
        "kde_sync.sh"
        "kde-sync-gui.desktop")
sha256sums=('SKIP' 'SKIP' 'SKIP' 'SKIP' 'SKIP')

check() {
    QT_QPA_PLATFORM=offscreen python3 -m unittest discover "${startdir}/tests"
}

package() {
    # 1. Install code files and resources to /opt/kde-sync-gui
    install -d "${pkgdir}/opt/kde-sync-gui"
    install -m755 "${srcdir}/main.py" "${pkgdir}/opt/kde-sync-gui/main.py"
    install -m644 "${srcdir}/config_manager.py" "${pkgdir}/opt/kde-sync-gui/config_manager.py"
    install -m644 "${srcdir}/style.qss" "${pkgdir}/opt/kde-sync-gui/style.qss"
    install -m755 "${srcdir}/kde_sync.sh" "${pkgdir}/opt/kde-sync-gui/kde_sync.sh"
    
    # Install the autostart desktop entry template, adjusting the Exec line for global path
    sed 's|Exec=.*|Exec=kde-sync-gui --minimized|' "${srcdir}/kde-sync-gui.desktop" > "${pkgdir}/opt/kde-sync-gui/kde-sync-gui.desktop"
    chmod 644 "${pkgdir}/opt/kde-sync-gui/kde-sync-gui.desktop"

    # 2. Install executable wrapper to /usr/bin/kde-sync-gui
    install -d "${pkgdir}/usr/bin"
    cat <<EOF > "${pkgdir}/usr/bin/kde-sync-gui"
#!/bin/bash
exec python3 /opt/kde-sync-gui/main.py "\$@"
EOF
    chmod 755 "${pkgdir}/usr/bin/kde-sync-gui"

    # 3. Install main system desktop entry (to show up in Kickoff/applications menus)
    install -d "${pkgdir}/usr/share/applications"
    cat <<EOF > "${pkgdir}/usr/share/applications/kde-sync-gui.desktop"
[Desktop Entry]
Type=Application
Exec=kde-sync-gui
Icon=preferences-desktop-wallpaper
Name=KDE Plasma Sync
Comment=KDE Plasma Backup & Sync GUI Tool
Categories=System;Utility;
Terminal=false
StartupNotify=false
EOF
    chmod 644 "${pkgdir}/usr/share/applications/kde-sync-gui.desktop"
}
