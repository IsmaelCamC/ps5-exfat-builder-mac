#!/usr/bin/env python3
"""
PS5 exFAT Image Builder — macOS Native Image Engine
===================================================
Replaces make_image.bat and New-OsfExfatImage.ps1 using native macOS
hdiutil (DiskImage framework) and newfs_exfat.

Maintains 1:1 stdout marker compatibility with exfat_builder.py's
progress parser:
  [1/4] Container creation
  [2/4] exFAT formatting (512b sector size)
  [3/4] Mount & copy with live file / percentage progress
  [4/4] Cache flush & dismount
"""

import sys
import os
import shutil
import subprocess
import time
import tempfile
import re
import signal

def _log(msg: str):
    sys.stdout.write(msg + '\n')
    sys.stdout.flush()

def format_size(bytes_val: int) -> str:
    gb = bytes_val / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.1f} MB"

def build_image(image_path: str, src_dir: str, cluster_size: str = "",
                sector_size: str = "512", copy_threads: int = 1,
                exclude_hidden: bool = False, img_override_gb: str = "") -> int:
    image_path = os.path.abspath(image_path)
    src_dir = os.path.abspath(src_dir)

    _log(f'[INFO] Source folder: "{src_dir}"')
    _log(f'[INFO] Output image:  "{image_path}"')

    # ── Source validation ──
    if not os.path.isdir(src_dir):
        _log(f'[ERROR] Source folder does not exist: "{src_dir}"')
        return 3

    # Check eboot.bin
    has_eboot = False
    for fn in os.listdir(src_dir):
        if fn.lower() == 'eboot.bin':
            has_eboot = True
            break
    if not has_eboot:
        # Check subdirectories if needed (e.g. nested dump)
        for root, dirs, files in os.walk(src_dir):
            if any(f.lower() == 'eboot.bin' for f in files):
                has_eboot = True
                break
    if not has_eboot:
        _log(f'[ERROR] eboot.bin not found in source folder "{src_dir}".')
        return 4

    _log('[INFO] eboot.bin found, proceeding to macOS native build.')

    # Ensure output directory exists
    os.makedirs(os.path.dirname(image_path), exist_ok=True)

    # ── Enumerate files & calculate total payload size ──
    _log('[INFO] Enumerating files in source folder...')
    file_list = []
    total_bytes = 0
    _excl_exts = ('.exfat', '.ffpkg', '.ffpfs', '.ffpfsc')

    for root, dirs, files in os.walk(src_dir):
        if exclude_hidden:
            dirs[:] = [d for d in dirs if not d.startswith('.')]
        dirs[:] = [d for d in dirs if d.lower() != 'decrypted']
        for fn in files:
            if exclude_hidden and fn.startswith('.'):
                continue
            if fn.lower().endswith(_excl_exts):
                continue
            p = os.path.join(root, fn)
            try:
                sz = os.path.getsize(p)
                total_bytes += sz
                rel = os.path.relpath(p, src_dir)
                file_list.append((p, rel, sz))
            except Exception:
                pass

    total_files = len(file_list)
    _log(f'[INFO] Source contains {total_files} files, {format_size(total_bytes)} total.')

    # ── Determine container size ──
    override_gb = 0.0
    if img_override_gb:
        try:
            override_gb = float(str(img_override_gb).strip())
        except ValueError:
            pass

    if override_gb > 0:
        container_bytes = int(override_gb * 1024 * 1024 * 1024)
        _log(f'[INFO] Using size override: {override_gb:.2f} GB')
    else:
        # Minimum safe padding: 5% + 64MB for FAT/root table structures, at least 64MB extra
        overhead = max(int(total_bytes * 0.05), 64 * 1024 * 1024) + 64 * 1024 * 1024
        container_bytes = total_bytes + overhead
        # Round up to 1 MB boundary
        container_bytes = ((container_bytes + 1048575) // 1048576) * 1048576
        # Minimum 50 MB
        container_bytes = max(container_bytes, 50 * 1024 * 1024)

    # ── Step 1: Create Image File ──
    _log(f'[1/4] Creating blank image container: {format_size(container_bytes)}')
    if os.path.exists(image_path):
        try:
            os.remove(image_path)
        except Exception as e:
            _log(f'[WARN] Could not remove existing file: {e}')

    try:
        with open(image_path, 'wb') as f:
            f.seek(container_bytes - 1)
            f.write(b'\0')
    except Exception as e:
        _log(f'[ERROR] Failed to allocate image file: {e}')
        return 10

    # ── Step 2: Format exFAT ──
    _log('[2/4] Formatting exFAT filesystem (512b sector size)...')
    attach_cmd = ['hdiutil', 'attach', '-imagekey', 'diskimage-class=CRawDiskImage', '-nomount', image_path]
    res = subprocess.run(attach_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        _log(f'[ERROR] hdiutil attach failed: {res.stderr}')
        return 11

    dev_node = None
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if parts and parts[0].startswith('/dev/disk'):
            dev_node = parts[0]
            break

    if not dev_node:
        _log(f'[ERROR] Could not parse disk node from hdiutil output: {res.stdout}')
        return 12

    try:
        newfs_args = ['newfs_exfat', '-v', 'PS5GAME', '-S', '512']

        # Cluster size mapping if specified
        if cluster_size and cluster_size.lower() != 'auto':
            m = re.match(r'^(\d+)\s*([KkMm])?', cluster_size)
            if m:
                val = int(m.group(1))
                unit = (m.group(2) or 'k').lower()
                b_size = val * 1024 if unit == 'k' else val * 1024 * 1024
                newfs_args.extend(['-b', str(b_size)])

        newfs_args.append(dev_node)
        fmt_res = subprocess.run(newfs_args, capture_output=True, text=True)
        if fmt_res.returncode != 0:
            _log(f'[ERROR] newfs_exfat failed: {fmt_res.stderr} {fmt_res.stdout}')
            return 13
        _log('[INFO] exFAT filesystem initialized successfully.')
    finally:
        subprocess.run(['hdiutil', 'detach', dev_node], capture_output=True)

    # ── Step 3: Mount and Copy Files ──
    mnt_dir = tempfile.mkdtemp(prefix='ps5_exfat_mnt_')
    _log(f'[3/4] Mounting image at: {mnt_dir}')
    mount_res = subprocess.run(
        ['hdiutil', 'attach', '-imagekey', 'diskimage-class=CRawDiskImage', '-mountpoint', mnt_dir, image_path],
        capture_output=True, text=True
    )
    if mount_res.returncode != 0:
        _log(f'[ERROR] Failed to mount image: {mount_res.stderr}')
        shutil.rmtree(mnt_dir, ignore_errors=True)
        return 14

    # Crucial line for GUI status parser to bind live drive polling:
    _log(f'logical volume on {mnt_dir}')
    _log(f'[3/4] Copying files to image container...')

    # Prevent creation of ._* AppleDouble files during copy
    os.environ['COPYFILE_DISABLE'] = '1'

    copied_files = 0
    copied_bytes = 0
    last_log_time = time.time()

    def _cleanup_mac_mount():
        _log(f'[4/4] Dismounting volume {mnt_dir}...')
        for _ in range(6):
            d_res = subprocess.run(['hdiutil', 'detach', mnt_dir, '-force'], capture_output=True, text=True)
            if d_res.returncode == 0:
                break
            time.sleep(1.0)
        shutil.rmtree(mnt_dir, ignore_errors=True)

    try:
        for src_path, rel_path, sz in file_list:
            dst_path = os.path.join(mnt_dir, rel_path)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)
            copied_files += 1
            copied_bytes += sz

            # Live per-file report matching robocopy tab format
            _log(f'   {copied_files}\t{rel_path}')

            now = time.time()
            if now - last_log_time > 2.0 or copied_files == total_files:
                pct = int((copied_bytes / total_bytes * 100)) if total_bytes > 0 else 100
                _log(f'{pct}%')
                last_log_time = now

        # End of copy summary line: triggers _mark_copy_done
        _log(f'Files :   {copied_files}      {copied_files}')
        _log('[Info] Post-copy check...')

        # Clean any stray macOS metadata files that might have been created
        for r, dirs, files in os.walk(mnt_dir):
            for fn in files:
                if fn.startswith('._') or fn == '.DS_Store':
                    try:
                        os.remove(os.path.join(r, fn))
                    except Exception:
                        pass
            for dn in list(dirs):
                if dn in ('.fseventsd', '.Spotlight-V100', '.Trashes'):
                    try:
                        shutil.rmtree(os.path.join(r, dn), ignore_errors=True)
                    except Exception:
                        pass

        _log('Flushing volume write cache...')
        subprocess.run(['sync'])
        _log('Volume cache flushed')

    except Exception as ex:
        _log(f'[ERROR] File copy aborted: {ex}')
        _cleanup_mac_mount()
        return 15

    _cleanup_mac_mount()
    _log(f'[OK] Done: "{image_path}"')
    return 0

def main(args=None):
    if args is None:
        args = sys.argv[1:]

    if len(args) < 2:
        _log("Usage: make_image_mac.py <image_path> <src_dir> [osf_path] [cluster_size] [sector_size] [threads] [retries] [retry_wait] [exclude_hidden] [img_size_gb]")
        return 1

    img_path = args[0]
    src_dir = args[1]
    cluster = args[3] if len(args) > 3 else ""
    sector = args[4] if len(args) > 4 else "512"
    threads = int(args[5]) if len(args) > 5 and args[5].isdigit() else 1
    excl = (args[8] == '1') if len(args) > 8 else False
    override = args[9] if len(args) > 9 else os.environ.get("EXFAT_IMGSIZEGB", "")

    return build_image(
        image_path=img_path,
        src_dir=src_dir,
        cluster_size=cluster,
        sector_size=sector,
        copy_threads=threads,
        exclude_hidden=excl,
        img_override_gb=override
    )

if __name__ == '__main__':
    sys.exit(main())
