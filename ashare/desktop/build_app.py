"""Build/install the native client using Apple's local toolchain, no external packages."""
import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = 'OpenPike Stocks.app'
IDENTIFIER = 'com.openpike.stocks'


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or '构建步骤失败')


def build(install=False):
    if os.uname().sysname != 'Darwin' or os.uname().machine != 'arm64':
        raise RuntimeError('此版本面向本机 Apple Silicon Mac')
    parent = ROOT / '.local/ashare/desktop-build'
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='build-', dir=parent))
    bundle = stage / NAME
    contents = bundle / 'Contents'
    executable = contents / 'MacOS/OpenPikeStocks'
    resources = contents / 'Resources'
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    source = ROOT / 'ashare/desktop/OpenPikeStocks.swift'
    run('/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', 'arm64-apple-macosx14.0',
        '-framework', 'AppKit', '-framework', 'WebKit', source, '-o', executable)
    iconset = stage / 'OpenPikeStocks.iconset'
    iconset.mkdir()
    logo = ROOT / 'ashare/static/brand/openpike-stocks-logo.png'
    shutil.copyfile(logo, resources / 'brand.png')
    for size in (16, 32, 128, 256, 512):
        run('/usr/bin/sips', '-z', size, size, logo, '--out', iconset / f'icon_{size}x{size}.png')
        run('/usr/bin/sips', '-z', size * 2, size * 2, logo, '--out', iconset / f'icon_{size}x{size}@2x.png')
    run('/usr/bin/iconutil', '-c', 'icns', iconset, '-o', resources / 'OpenPikeStocks.icns')
    info = {'CFBundleDevelopmentRegion': 'zh_CN', 'CFBundleIdentifier': IDENTIFIER,
            'CFBundleName': 'OpenPike Stocks', 'CFBundleDisplayName': 'OpenPike Stocks',
            'CFBundleExecutable': 'OpenPikeStocks', 'CFBundlePackageType': 'APPL',
            'CFBundleIconFile': 'OpenPikeStocks.icns', 'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1', 'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
            'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
            'NSHumanReadableCopyright': 'OpenPike Stocks · 本机研究工具',
            'OpenPikeWorkspaceRoot': str(ROOT)}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    (contents / 'PkgInfo').write_bytes(b'APPL????')
    shutil.copyfile(ROOT / 'ashare/desktop/README.md', resources / '本机应用说明.md')
    # Generated icon conversions can inherit Finder metadata from the source PNG.
    # Remove that metadata only from this newly generated staging bundle.
    run('/usr/bin/xattr', '-cr', bundle)
    run('/usr/bin/codesign', '--force', '--sign', '-', bundle)
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', bundle)
    destination = Path('/Applications') / NAME
    shortcut = Path.home() / 'Desktop' / NAME
    if install:
        # Never overwrite a different app or an existing user Desktop item.
        if destination.exists():
            existing = plistlib.loads((destination / 'Contents/Info.plist').read_bytes())
            if existing.get('CFBundleIdentifier') != IDENTIFIER or existing.get('OpenPikeWorkspaceRoot') != str(ROOT):
                raise RuntimeError('应用目录存在其他同名应用，未覆盖')
        if shortcut.exists() or shortcut.is_symlink():
            if not shortcut.is_symlink() or shortcut.resolve() != destination:
                raise RuntimeError('桌面已有其他同名项目，未覆盖')
        if destination.exists():
            backup = stage / 'previous-OpenPike Stocks.app'
            destination.rename(backup)
            try:
                shutil.copytree(bundle, destination)
            except BaseException:
                if destination.exists(): shutil.rmtree(destination)
                backup.rename(destination)
                raise
        else:
            shutil.copytree(bundle, destination)
        if not shortcut.is_symlink(): shortcut.symlink_to(destination, target_is_directory=True)
        run('/usr/bin/codesign', '--verify', '--deep', '--strict', destination)
    report = {'built': str(bundle), 'installed': str(destination) if install else None,
              'desktop_entry': str(shortcut) if install else None,
              'architecture': 'arm64', 'minimum_macos': '14.0', 'workspace': str(ROOT),
              'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'native_client': 'AppKit + WKWebView', 'local_data_reused': True,
              'portable': False, 'signed': 'local ad hoc'}
    (ROOT / '.local/ashare/desktop-build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    print(json.dumps(build(parser.parse_args().install), ensure_ascii=False, indent=2))
