"""
PS5 exFAT Image Builder — macOS Compatibility & Native Disk Utilities
======================================================================
Provides native macOS disk image mounting, unmounting, tree-mirroring,
verification, and system integration without requiring OSFMount or Dokan.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import time

IS_MACOS = (sys.platform == 'darwin')

def patch_system_for_mac():
    """Apply global macOS runtime patches for cross-platform compatibility."""
    if not IS_MACOS:
        return

    # 1. Prevent creation of macOS ._* AppleDouble files on FAT/exFAT
    os.environ['COPYFILE_DISABLE'] = '1'

    # 2. Provide os.startfile fallback for macOS using native 'open'
    if not hasattr(os, 'startfile'):
        def _startfile(path, operation=None):
            try:
                subprocess.Popen(['open', path])
            except Exception as e:
                pass
        os.startfile = _startfile

def mount_image(img_path: str, read_only: bool = True, mountpoint: str = None) -> tuple[str, str]:
    """
    Mount a raw .exfat disk image using native macOS DiskImage framework.
    Returns (mountpoint, dev_node).
    """
    if not mountpoint:
        mountpoint = tempfile.mkdtemp(prefix='ps5_img_mnt_')

    os.makedirs(mountpoint, exist_ok=True)
    cmd = ['hdiutil', 'attach', '-imagekey', 'diskimage-class=CRawDiskImage',
           '-mountpoint', mountpoint, img_path]
    if read_only:
        cmd.append('-readonly')

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        shutil.rmtree(mountpoint, ignore_errors=True)
        raise RuntimeError(f"hdiutil mount failed: {res.stderr or res.stdout}")

    dev_node = None
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if parts and parts[0].startswith('/dev/disk'):
            dev_node = parts[0]
            break

    return mountpoint, dev_node or mountpoint

def unmount_image(target: str, timeout: int = 15) -> bool:
    """Detach a mounted image using target mountpoint or dev_node."""
    if not target:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run(['hdiutil', 'detach', target, '-force'],
                             capture_output=True, text=True)
        if res.returncode == 0:
            if os.path.isdir(target) and 'ps5_' in os.path.basename(target):
                shutil.rmtree(target, ignore_errors=True)
            return True
        time.sleep(1.0)
    return False

def mount_exfat_tree_to_tmp(img_path: str) -> tuple[str, callable]:
    """
    macOS drop-in replacement for Windows OSFMount-to-drive-letter logic.
    Mounts img_path, copies contents into a temporary directory, dismounts image,
    and returns (tmp_dir, cleanup_callback).
    """
    tmp = tempfile.mkdtemp(prefix='exfat_bp_mac_src_')
    mnt, _ = mount_image(img_path, read_only=True)

    def _cleanup():
        unmount_image(mnt)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(mnt, ignore_errors=True)

    try:
        for root, dirs, files in os.walk(mnt):
            rel = os.path.relpath(root, mnt)
            if rel == '.':
                rel = ''
            for fn in files:
                if fn.startswith('._') or fn == '.DS_Store':
                    continue
                src = os.path.join(root, fn)
                dst = os.path.join(tmp, rel, fn) if rel else os.path.join(tmp, fn)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                except Exception:
                    pass
        unmount_image(mnt)
        return tmp, _cleanup
    except Exception:
        _cleanup()
        raise

def verify_exfat_image(img_path: str) -> tuple[bool, str]:
    """Verify built .exfat image on macOS by mounting read-only and checking structure."""
    if not os.path.isfile(img_path):
        return False, f"Output file not found: {img_path}"

    sz = os.path.getsize(img_path)
    if sz == 0:
        return False, "Output file is 0 bytes"

    mnt, _ = mount_image(img_path, read_only=True)
    try:
        has_eboot = any(f.lower() == 'eboot.bin' for f in os.listdir(mnt))
        if not has_eboot:
            for root, dirs, files in os.walk(mnt):
                if any(f.lower() == 'eboot.bin' for f in files):
                    has_eboot = True
                    break

        file_count = sum(len(files) for _, _, files in os.walk(mnt)
                         if not any(f.startswith('._') for f in files))
        if not has_eboot:
            return False, "eboot.bin not found inside image"
        return True, f"Verified: eboot.bin found, {file_count} files OK"
    finally:
        unmount_image(mnt)

def setup_mac_shortcuts(widget):
    """Bind standard macOS keyboard shortcuts (<Command-c>, <Command-v>, etc.) to a Tkinter widget."""
    if not IS_MACOS:
        return
    try:
        widget.bind_all('<Command-c>', lambda e: e.widget.event_generate('<<Copy>>'))
        widget.bind_all('<Command-v>', lambda e: e.widget.event_generate('<<Paste>>'))
        widget.bind_all('<Command-x>', lambda e: e.widget.event_generate('<<Cut>>'))
        widget.bind_all('<Command-a>', lambda e: e.widget.event_generate('<<SelectAll>>'))
    except Exception:
        pass
