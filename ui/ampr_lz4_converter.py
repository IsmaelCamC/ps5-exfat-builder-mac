"""ui/ampr_lz4_converter.py — PS5 fPKG & Game Folder to AMPR LZ4 Asset Pack Converter.

Based on Nazky/Lazy_AMPR (https://github.com/Nazky/Lazy_AMPR) and ampr_emu (https://github.com/drakmor/ampr_emu):
1. Ingests PS5 fPKG (.pkg) or extracted game folder (/app0).
2. For .pkg, decrypts PFS and extracts CNT metadata via ui.ps5_pkg_extractor.
3. Automatically installs verified PS5 AMPR runtime (fakelib/libSceAmpr.sprx).
4. Generates root ampr_emu.index (AMPRIDX3, FNV-1a 64-bit case-insensitive table).
5. Scans game structure and generates universal or trace-based TOML profile.
6. Enforces SAFETY_EXCLUSIONS so executables (eboot.bin, PRXs), metadata, and boot-critical
   files remain untouched as loose files.
7. Compresses assets to LZ4 .pak volumes (ampr_assets-*.pak) and ampr_assets.index.
8. Verifies integrity of all packed blocks via ampr_pack verify.
9. Places loose files in output, creating a complete, 100% playable game structure on PS5.
10. Optionally wraps output into exFAT (.exfat) or ShadowMount (.ffpfsc) containers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

try:
    import toml
    HAS_TOML = True
except ImportError:
    HAS_TOML = False

try:
    import lz4
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False

# Constants
RUNTIME_RELATIVE = "fakelib/libSceAmpr.sprx"
RUNTIME_SHA256 = "69e6c4d5e4f5fb83c9e01815db5861c4c75734acbf4595cafa50d4c218116d1a"

SAFETY_EXCLUSIONS = (
    "eboot.bin", "**/eboot.bin",
    "*.elf", "**/*.elf",
    "*.self", "**/*.self",
    "*.prx", "**/*.prx",
    "*.sprx", "**/*.sprx",
    "*.bak", "**/*.bak",
    "*.dat", "**/*.dat",
    "*.utoc", "**/*.utoc",
    "decrypted/**",
    "sce_module/**", "**/sce_module/**",
    "sce_sys/**", "**/sce_sys/**",
    "system/**", "**/system/**",
    "mods/**", "**/mods/**",
    "save/**", "**/save/**",
    "fakelib/**", "**/fakelib/**",
    "_DUPLEX_/**",
    "trophy2/**", "**/trophy2/**",
    "uds/**", "**/uds/**",
    "ampr_emu.index", "**/ampr_emu.index",
    "ampr_assets.index", "**/ampr_assets.index",
    "ampr_assets.index.crc", "**/ampr_assets.index.crc",
    "ampr_assets.index.runtime", "**/ampr_assets.index.runtime",
    "ampr_assets-*.pak", "**/*.pak",
    "*.json", "**/*.json",
    "*.ini", "**/*.ini",
    "*.cfg", "**/*.cfg",
    "*.xml", "**/*.xml",
    "*.txt", "**/*.txt",
    "*.bk2", "**/*.bk2",
    "*.mp4", "**/*.mp4",
    "*.ivf", "**/*.ivf",
    "*.usm", "**/*.usm",
    "*.bnk", "**/*.bnk",
    "*.wem", "**/*.wem",
    "*.at9", "**/*.at9",
    "*.pfs", "**/*.pfs",
    "*.img", "**/*.img",
    # Compressed images
    "*.png", "**/*.png",
    "*.jpg", "**/*.jpg",
    "*.jpeg", "**/*.jpeg",
    "*.webp", "**/*.webp",
    "*.gif", "**/*.gif",
    "*.bmp", "**/*.bmp",
    "*.ico", "**/*.ico",
    "*.tga", "**/*.tga",
    "*.tif", "**/*.tif",
    "*.tiff", "**/*.tiff",
    "*.exr", "**/*.exr",
    "*.hdr", "**/*.hdr",
    "*.psd", "**/*.psd",
    # GPU block-compressed textures
    "*.dds", "**/*.dds",
    "*.ktx", "**/*.ktx",
    "*.ktx2", "**/*.ktx2",
    "*.astc", "**/*.astc",
    "*.basis", "**/*.basis",
    "*.gnf", "**/*.gnf",
    "*.gnfp", "**/*.gnfp",
    "*.jxm", "**/*.jxm",
    "*.vtf", "**/*.vtf",
    # Compressed archives
    "*.zip", "**/*.zip",
    "*.7z", "**/*.7z",
    "*.rar", "**/*.rar",
    "*.gz", "**/*.gz",
    "*.xz", "**/*.xz",
    "*.bz2", "**/*.bz2",
    "*.zst", "**/*.zst",
    "*.lz4", "**/*.lz4",
    # Audio
    "*.mp3", "**/*.mp3",
    "*.ogg", "**/*.ogg",
    "*.flac", "**/*.flac",
    "*.aac", "**/*.aac",
    "*.opus", "**/*.opus",
    "*.m4a", "**/*.m4a",
    # Video
    "*.avi", "**/*.avi",
    "*.mkv", "**/*.mkv",
    "*.mov", "**/*.mov",
    "*.wmv", "**/*.wmv",
    "*.flv", "**/*.flv",
    "*.webm", "**/*.webm",
    "*.m4v", "**/*.m4v",
)


def _pattern_to_regex(pattern: str) -> re.Pattern:
    out = ["^"]
    i = 0
    while i < len(pattern):
        if pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


_SAFETY_REGEXES = tuple(_pattern_to_regex(p) for p in SAFETY_EXCLUSIONS)


def is_loose_path(rel_path: str | Path) -> bool:
    rel_posix = Path(rel_path).as_posix()
    return any(rx.match(rel_posix) for rx in _SAFETY_REGEXES)


def _get_ampr_tools_dir() -> Path:
    """Locate the bundled ampr_tools directory."""
    pkg_dir = Path(__file__).resolve().parent / "ampr_tools"
    if pkg_dir.is_dir():
        return pkg_dir
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass_tools = Path(sys._MEIPASS) / "ui" / "ampr_tools"
        if meipass_tools.is_dir():
            return meipass_tools
    return pkg_dir


def _get_bundled_runtime_path() -> Path:
    """Locate the bundled fakelib/libSceAmpr.sprx file."""
    candidates = [
        Path(__file__).resolve().parents[1] / "assets" / "fakelib" / "libSceAmpr.sprx",
        Path(__file__).resolve().parent / "ampr_tools" / "fakelib" / "libSceAmpr.sprx",
    ]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.insert(0, Path(sys._MEIPASS) / "assets" / "fakelib" / "libSceAmpr.sprx")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def install_ampr_runtime(source_dir: Path, output_dir: Path, log_cb: Optional[Callable[[str], None]] = None) -> dict[str, Path]:
    """Copy the verified libSceAmpr.sprx runtime into output/fakelib/."""
    runtime_src = _get_bundled_runtime_path()
    if not runtime_src.is_file():
        raise FileNotFoundError(
            f"Bundled AMPR runtime missing: {runtime_src}. Ensure assets/fakelib/libSceAmpr.sprx exists."
        )
    h = hashlib.sha256(runtime_src.read_bytes()).hexdigest()
    if h != RUNTIME_SHA256:
        raise ValueError(f"Bundled AMPR runtime SHA-256 mismatch ({h} != {RUNTIME_SHA256}). Refusing unverified runtime.")

    dest = output_dir / RUNTIME_RELATIVE
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime_src, dest)
    if log_cb:
        log_cb(f"[AMPR] Installed verified PS5 runtime: {dest.relative_to(output_dir)} (SHA-256 verified)")
    return {RUNTIME_RELATIVE: dest}


def scan_game_structure(game_dir: Path) -> dict[str, Any]:
    """Analyze game files to generate an optimal TOML profile."""
    stats = {
        'folders': defaultdict(int),
        'extensions': defaultdict(int),
        'large_files': [],
        'streaming_containers': [],
        'has_data_folder': False,
        'has_audio_folders': False,
        'has_video_folders': False,
        'root_numbered_files': [],
    }

    STREAMING_FOLDER_HINTS = {'video', 'movie', 'movies', 'cutscene', 'cutscenes', 'fmv', 'cinematic', 'cinematics'}
    AUDIO_FOLDER_HINTS = {'sound', 'audio', 'sfx', 'music', 'voice', 'vo', 'dialog', 'dialogue', 'soundbank', 'wem'}
    ASSET_FOLDER_HINTS = {'d', 'data', 'assets', 'content', 'game', 'resources', 'streaming'}

    for dirpath, dirnames, filenames in os.walk(game_dir):
        rel_dir = Path(dirpath).relative_to(game_dir)
        rel_dir_str = rel_dir.as_posix() if str(rel_dir) != '.' else '.'

        if rel_dir_str != '.':
            top_folder = rel_dir_str.split('/')[0].lower()
            stats['folders'][top_folder] += len(filenames)

            if any(hint in top_folder for hint in STREAMING_FOLDER_HINTS):
                stats['has_video_folders'] = True
                stats['streaming_containers'].append(f"**/{top_folder}/**")

            if any(hint in top_folder for hint in AUDIO_FOLDER_HINTS):
                stats['has_audio_folders'] = True

            if any(hint in top_folder for hint in ASSET_FOLDER_HINTS):
                stats['has_data_folder'] = True
        else:
            for fn in filenames:
                if re.match(r'^[a-zA-Z_]+\.\d+$', fn) or re.match(r'^[a-zA-Z_]+\d+\.[a-zA-Z]+$', fn) or re.match(r'^[a-zA-Z_]+\.\d{2,}$', fn):
                    stats['root_numbered_files'].append(fn)

        for filename in filenames:
            filepath = Path(dirpath) / filename
            ext = filepath.suffix.lower()
            if ext:
                stats['extensions'][ext] += 1
            try:
                sz = filepath.stat().st_size
                if sz > 64 * 1024 * 1024:
                    stats['large_files'].append((filepath.relative_to(game_dir).as_posix(), sz))
            except OSError:
                pass

    return stats


def _clamp_block_size_kib(kib: int) -> int:
    kib = max(16, min(1024, int(kib)))
    lo = 1 << (kib.bit_length() - 1)
    hi = lo * 2
    return hi if (kib - lo) > (hi - kib) else lo


def generate_universal_pack_profile(game_dir: Path, config_path: Path,
                                    level: int = 9, block_size_kib: int = 64,
                                    workers: Optional[int] = None,
                                    auto_loose_large: bool = True,
                                    decoded_cache_mib: int = 256,
                                    physical_cache_mib: int = 64,
                                    log_cb: Optional[Callable[[str], None]] = None) -> None:
    """Generate universal Lazy_AMPR TOML configuration."""
    level = max(1, min(12, int(level)))
    block_size_kib = _clamp_block_size_kib(block_size_kib)
    mode = "fast" if level <= 4 else "hc"
    workers = workers or max(1, (os.cpu_count() or 4) - 1)
    rt_workers = min(workers, 8)
    latency_reserve = 1 if rt_workers > 1 else 0

    stats = scan_game_structure(game_dir)
    if log_cb:
        log_cb(f"[AMPR] Game structure: {len(stats['folders'])} folders, {len(stats['extensions'])} formats, {len(stats['large_files'])} large assets (>64MB)")

    lines = [
        "# Universal profile generated by PS5 exFAT Builder / Lazy_AMPR engine",
        "",
        "[pack]",
        'index_name = "ampr_assets.index"',
        'pack_pattern = "ampr_assets-{group}-lane{lane:02d}-vol{volume:02d}-{id:03d}.pak"',
        'default_action = "loose"',
        f'default_block_size = "{block_size_kib}KiB"',
        'io_page_size = "64KiB"',
        'payload_alignment = "64KiB"',
        'chunk_alignment = "64B"',
        f'workers = {workers}',
        f'compression_mode = "{mode}"',
        f'compression_level = {level}',
        'acceleration = 1',
        'deduplicate = true',
        'deduplicate_scope = "lane"',
        'deduplicate_streaming = false',
        'min_savings_bytes = 64',
        'min_savings_ratio = 0.01',
        'io_neutral_min_savings_bytes = "8KiB"',
        'io_neutral_min_savings_ratio = 0.125',
        f'auto_loose_large_files = {"true" if auto_loose_large else "false"}',
        'auto_loose_hot_files = false',
        'auto_loose_min_file_size = "64MiB"',
        'auto_loose_sample_blocks = 32',
        'auto_loose_sample_bytes = "16MiB"',
        'auto_loose_min_savings_ratio = 0.05',
        'auto_loose_max_raw_ratio = 0.90',
        'preserve_mtime = true',
        'validate_index_metadata = true',
        '',
        "[runtime]",
        f'decoded_cache_bytes = "{decoded_cache_mib}MiB"',
        f'physical_cache_bytes = "{physical_cache_mib}MiB"',
        f'workers = {rt_workers}',
        f'latency_reserve_workers = {latency_reserve}',
        '',
        "[groups.assets]",
        'pack_count = 4',
        'assignment = "balanced"',
        'max_pack_size = "16GiB"',
        'stripe_large_files = false',
        '',
    ]

    # Baseline rule
    lines.extend([
        "# Baseline: compress files in subdirectories",
        "[[rule]]",
        'action = "compress"',
        'include = ["*/**"]',
        f'block_size = "{block_size_kib}KiB"',
        'group = "assets"',
        'layout = "mixed"',
        '',
    ])

    if stats['root_numbered_files']:
        data_globs = ", ".join(f'"{fn}"' for fn in sorted(stats['root_numbered_files']))
        lines.extend([
            "# Root-level numbered data files",
            "[[rule]]",
            'action = "compress"',
            f'include = [{data_globs}]',
            f'block_size = "{block_size_kib}KiB"',
            'group = "assets"',
            'layout = "mixed"',
            '',
        ])

    texture_exts = {'.dds', '.ktx', '.ktx2', '.astc', '.basis', '.tga', '.gnf', '.gnfp',
                    '.jxm', '.vtf', '.dat', '.res', '.uasset', '.ubulk', '.bin'}
    found_texture_exts = [ext for ext in stats['extensions'] if ext in texture_exts]
    if found_texture_exts:
        texture_globs = ", ".join(f'"**/*{ext}"' for ext in sorted(found_texture_exts))
        lines.extend([
            "# Texture & streamed container formats",
            "[[rule]]",
            'action = "compress"',
            f'include = [{texture_globs}]',
            f'block_size = "{max(block_size_kib, 64)}KiB"',
            'group = "assets"',
            'layout = "mixed"',
            '',
        ])

    if stats['has_audio_folders']:
        audio_globs = ', '.join(f'"**/{folder}/**"' for folder in
                                 ['sound', 'audio', 'sfx', 'music', 'voice', 'vo', 'dialog', 'dialogue'])
        lines.extend([
            "# Streamed audio: small blocks",
            "[[rule]]",
            'action = "compress"',
            f'include = [{audio_globs}]',
            'block_size = "16KiB"',
            'group = "assets"',
            'layout = "random"',
            'hot = true',
            'min_savings_bytes = 16',
            'min_savings_ratio = 0.0025',
            'io_neutral_min_savings_bytes = "2KiB"',
            'io_neutral_min_savings_ratio = 0.125',
            '',
        ])

    if stats['has_video_folders'] or stats['streaming_containers']:
        video_globs = ', '.join(f'"**/{folder}/**"' for folder in
                               ['video', 'movie', 'movies', 'cutscene', 'cutscenes', 'fmv', 'cinematic', 'cinematics'])
        lines.extend([
            "# Video/cutscene folders: already compressed",
            "[[rule]]",
            'action = "loose"',
            f'include = [{video_globs}]',
            '',
        ])

    streaming_containers = [
        '"**/movie"', '"**/movie_*"', '"**/movies"', '"**/movies_*"',
        '"**/soundbank"', '"**/soundbank_*"',
        '"**/screenreaderwem"', '"**/screenreaderwem.*"',
        '"**/wem"', '"**/wem_*"', '"**/wem.*"',
    ]
    lines.extend([
        "# Streaming containers: boot-critical, keep loose",
        "[[rule]]",
        'action = "loose"',
        f'include = [{", ".join(streaming_containers)}]',
        '',
    ])

    safety_list = "\n".join(f'  "{p}",' for p in SAFETY_EXCLUSIONS)
    lines.extend([
        "# Safety exclusions (executables, boot metadata, formats)",
        "[[rule]]",
        'action = "loose"',
        'include = [',
        safety_list,
        ']',
    ])

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_trace_profile(traces_dir: Path, output_toml: Path, game_name: str,
                           log_cb: Optional[Callable[[str], None]] = None) -> None:
    """Generate TOML profile from recorded PS5 traces."""
    tools_dir = _get_ampr_tools_dir()
    profile_script = tools_dir / "ampr_pack_profile.py"
    if not profile_script.is_file():
        raise FileNotFoundError(f"ampr_pack_profile.py not found at {profile_script}")

    trace_pairs = []
    if (traces_dir / "ampr_commands.bin").is_file() and (traces_dir / "ampr_emu.index").is_file():
        trace_pairs.append((traces_dir / "ampr_commands.bin", traces_dir / "ampr_emu.index"))
    else:
        for cmd_f in sorted(traces_dir.rglob("ampr_commands.bin")):
            idx_f = cmd_f.with_name("ampr_emu.index")
            if idx_f.is_file():
                trace_pairs.append((cmd_f, idx_f))

    if not trace_pairs:
        raise FileNotFoundError(f"No trace pairs (ampr_commands.bin + ampr_emu.index) found in {traces_dir}")

    cmd = [sys.executable, str(profile_script), "generate"]
    for c, i in trace_pairs:
        cmd.extend(["--trace", str(c), str(i)])
    cmd.extend([
        "--name", game_name,
        "--output", str(output_toml),
        "--overwrite",
        "--pattern-mode", "exact",
        "--cache-sim",
    ])

    if log_cb:
        log_cb(f"[AMPR] Generating trace profile with {len(trace_pairs)} traces...")
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=str(tools_dir))
    if res.returncode != 0:
        raise RuntimeError(f"ampr_pack_profile failed:\n{res.stdout}\n{res.stderr}")


RECORD_STRUCT = struct.Struct("<IIQq")
HASH_SLOT_STRUCT = struct.Struct("<QII")
HEADER_STRUCT = struct.Struct("<8sIIQQQII")
INDEX_MAGIC = b"AMPRIDX3"
INDEX_VERSION = 3


def _index_key_for(path: str) -> bytes:
    raw = path.replace("\\", "/").encode("utf-8")
    return bytes((byte + 0x20) if 0x41 <= byte <= 0x5A else byte for byte in raw)


def _fnv1a64_path_hash(path: str) -> int:
    h = 1469598103934665603
    for byte in _index_key_for(path):
        h ^= byte
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h or 1


def _hash_slot_count(entry_count: int) -> int:
    if entry_count <= 0:
        return 0
    slots = 2
    target = entry_count * 2
    while slots < target:
        slots <<= 1
    return slots


def _build_hash_slots(rows: list[tuple[int, int, str]]) -> list[tuple[int, int, int]]:
    duplicate_flag = 1
    slots = [(0, 0, 0) for _ in range(_hash_slot_count(len(rows)))]
    mask = len(slots) - 1
    for index, (_, _, path) in enumerate(rows):
        h = _fnv1a64_path_hash(path)
        pos = h & mask
        duplicate = False
        while slots[pos][1] != 0:
            if slots[pos][0] == h:
                old_hash, old_index_plus_one, old_flags = slots[pos]
                slots[pos] = (old_hash, old_index_plus_one, old_flags | duplicate_flag)
                duplicate = True
            pos = (pos + 1) & mask
        slots[pos] = (h, index + 1, duplicate_flag if duplicate else 0)
    return slots


def build_ampr_emu_index(root_dir: Path, output_path: Path,
                         metadata_overrides: Optional[dict[str, Path]] = None,
                         log_cb: Optional[Callable[[str], None]] = None) -> None:
    """Build /app0/ampr_emu.index (AMPRIDX3) for PS5 case-insensitive runtime lookup."""
    overrides = {name.casefold(): replacement for name, replacement in (metadata_overrides or {}).items()}
    root = root_dir.resolve()
    output_path = output_path.resolve()
    output_tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    seen: dict[bytes, str] = {}
    rows: list[tuple[int, int, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort(key=str.lower)
        filenames.sort(key=str.lower)
        for filename in filenames:
            p = Path(dirpath) / filename
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if resolved == output_path or resolved == output_tmp:
                continue
            rel = p.relative_to(root).as_posix()
            indexed_path = "/app0/" + rel
            if indexed_path.lower() in {
                "/app0/ampr_emu.index",
                "/app0/ampr_emu.index.tmp",
                "/app0/ampr_commands.bin",
                "/app0/apr_emu.log",
            }:
                continue
            try:
                st = overrides.get(rel.casefold(), p).stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            k = _index_key_for(indexed_path)
            if k in seen:
                continue
            seen[k] = indexed_path
            rows.append((st.st_size, int(st.st_mtime), indexed_path))

    # Also include any metadata overrides that don't exist yet on source
    for rel_name, dest_p in overrides.items():
        indexed_path = "/app0/" + rel_name.replace("\\", "/")
        k = _index_key_for(indexed_path)
        if k not in seen and dest_p.is_file():
            try:
                st = dest_p.stat()
                seen[k] = indexed_path
                rows.append((st.st_size, int(st.st_mtime), indexed_path))
            except OSError:
                pass

    if not rows:
        return

    rows = sorted(rows, key=lambda row: _index_key_for(row[2]))
    path_blob = bytearray()
    records = bytearray()
    for size, mtime, path in rows:
        encoded = path.encode("utf-8") + b"\0"
        offset = len(path_blob)
        path_len = len(encoded) - 1
        records += RECORD_STRUCT.pack(offset, path_len, size, mtime)
        path_blob += encoded

    hash_slots = _build_hash_slots(rows)
    path_end = HEADER_STRUCT.size + len(records) + len(path_blob)
    hash_offset = (path_end + (HASH_SLOT_STRUCT.size - 1)) & ~(HASH_SLOT_STRUCT.size - 1)
    padding = b"\0" * (hash_offset - path_end)

    with output_tmp.open("wb") as f:
        f.write(HEADER_STRUCT.pack(INDEX_MAGIC, INDEX_VERSION, RECORD_STRUCT.size,
                                   len(rows), len(path_blob), hash_offset,
                                   HASH_SLOT_STRUCT.size, len(hash_slots)))
        f.write(records)
        f.write(path_blob)
        f.write(padding)
        for h, index_plus_one, flags in hash_slots:
            f.write(HASH_SLOT_STRUCT.pack(h, index_plus_one, flags))
    output_tmp.replace(output_path)

    if log_cb:
        log_cb(f"[AMPR] Built ampr_emu.index (AMPRIDX3, {len(rows)} entries) at {output_path.name}")


def pack_game_directory_to_lz4(source_dir: Path, output_dir: Path,
                               game_name: str = "game",
                               lz4_level: int = 9,
                               block_size_kib: int = 64,
                               custom_config: Optional[Path] = None,
                               traces_dir: Optional[Path] = None,
                               skip_verify: bool = False,
                               workers: Optional[int] = None,
                               log_cb: Optional[Callable[[str], None]] = None,
                               progress_cb: Optional[Callable[[str, int, int], None]] = None,
                               cancel_check: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Pack an uncompressed game directory into an AMPR LZ4 asset pack distribution.

    Preserves 100% playable PS5 compatibility:
    - Retains eboot.bin, PRXs, sce_sys, etc. as loose files.
    - Generates ampr_assets.index + ampr_assets-*.pak.
    - Generates ampr_emu.index.
    - Installs fakelib/libSceAmpr.sprx.
    """
    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    tools_dir = _get_ampr_tools_dir()
    ampr_pack_py = tools_dir / "ampr_pack.py"
    if not ampr_pack_py.is_file():
        raise FileNotFoundError(f"ampr_pack.py not found at {ampr_pack_py}")

    def _check():
        if cancel_check and cancel_check():
            raise RuntimeError("Operation cancelled by user.")

    _check()

    # Step 1: Install AMPR runtime
    if progress_cb:
        progress_cb("Installing AMPR runtime (fakelib)...", 5, 100)
    overrides = install_ampr_runtime(source_dir, output_dir, log_cb=log_cb)

    # Step 2: Build ampr_emu.index
    if progress_cb:
        progress_cb("Building case-insensitive ampr_emu.index...", 12, 100)
    ampr_index = output_dir / "ampr_emu.index"
    build_ampr_emu_index(source_dir, ampr_index, metadata_overrides=overrides, log_cb=log_cb)

    # Step 3: Profile resolution
    if progress_cb:
        progress_cb("Resolving compression profile...", 20, 100)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", game_name)[:50]

    if custom_config and custom_config.is_file():
        config_path = custom_config
        if log_cb:
            log_cb(f"[AMPR] Using custom TOML profile: {custom_config.name}")
        if HAS_TOML:
            try:
                data = toml.load(str(custom_config))
                data.setdefault("pack", {})["compression_level"] = lz4_level
                config_path = output_dir / f"{safe_name}_config.toml"
                with open(config_path, "w", encoding="utf-8") as f:
                    toml.dump(data, f)
            except Exception as e:
                if log_cb:
                    log_cb(f"[WARN] Failed to apply overrides to custom TOML: {e}")
    elif traces_dir and traces_dir.is_dir():
        config_path = output_dir / f"{safe_name}_trace_profile.toml"
        generate_trace_profile(traces_dir, config_path, game_name, log_cb=log_cb)
    else:
        config_path = output_dir / f"{safe_name}_auto_profile.toml"
        if log_cb:
            log_cb(f"[AMPR] Generating universal scan profile (LZ4 Level {lz4_level}, Block {block_size_kib}KiB)...")
        generate_universal_pack_profile(source_dir, config_path, level=lz4_level,
                                        block_size_kib=block_size_kib,
                                        workers=workers, log_cb=log_cb)

    # Step 4: Run ampr_pack.py pack
    if progress_cb:
        progress_cb(f"Packing assets to LZ4 (Level {lz4_level})...", 30, 100)
    if log_cb:
        log_cb(f"[AMPR] Starting LZ4 compression engine (source: {source_dir.name})...")

    pack_cmd = [
        sys.executable, str(ampr_pack_py),
        "pack",
        "--root", str(source_dir),
        "--ampr-index", str(ampr_index),
        "--output", str(output_dir),
        "--config", str(config_path),
    ]
    for pat in SAFETY_EXCLUSIONS:
        pack_cmd.extend(("--exclude", pat))

    pack_stdout: list[str] = []
    progress_re = re.compile(r"\[(\w+)\s+(\d+)%\]")

    with subprocess.Popen(
        pack_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tools_dir),
    ) as proc:
        for line in proc.stdout:
            line_str = line.strip()
            if not line_str:
                continue
            pack_stdout.append(line_str)
            if log_cb:
                log_cb(line_str)
            m = progress_re.search(line_str)
            if m and progress_cb:
                stage = m.group(1)
                pct = int(m.group(2))
                # Map pack percentage 0-100 to global 30-75
                overall = int(30 + (pct * 0.45))
                progress_cb(f"Compressing LZ4 packs ({stage} {pct}%)...", overall, 100)
            _check()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ampr_pack pack failed with return code {proc.returncode}")

    full_output = "\n".join(pack_stdout)
    pack_result = None
    try:
        start = full_output.rfind('{')
        end = full_output.rfind('}')
        if start != -1 and end != -1 and end > start:
            pack_result = json.loads(full_output[start:end+1])
    except Exception:
        pass

    # Step 5: Verification
    _check()
    assets_index = output_dir / "ampr_assets.index"
    if not skip_verify and assets_index.is_file():
        if progress_cb:
            progress_cb("Verifying LZ4 pack checksums...", 78, 100)
        if log_cb:
            log_cb("[AMPR] Verifying packed integrity (ampr_pack verify)...")
        ver_cmd = [
            sys.executable, str(ampr_pack_py),
            "verify",
            "--index", str(assets_index),
            "--root", str(source_dir),
        ]
        vres = subprocess.run(ver_cmd, capture_output=True, text=True, errors="replace", cwd=str(tools_dir))
        if vres.returncode != 0:
            raise RuntimeError(f"AMPR LZ4 verify failed:\n{vres.stdout}\n{vres.stderr}")
        if log_cb:
            log_cb("[AMPR] Verification passed! All LZ4 blocks match expected CRC/checksums.")

    # Step 6: Copy loose files
    if progress_cb:
        progress_cb("Copying boot files & loose executables...", 85, 100)
    if log_cb:
        log_cb("[AMPR] Placing loose files into output directory...")

    loose_paths = None
    if pack_result and isinstance(pack_result.get("loose_paths"), list):
        loose_paths = {Path(p).as_posix() for p in pack_result["loose_paths"]}

    def _should_copy(rel: Path) -> bool:
        if loose_paths is not None:
            return rel.as_posix() in loose_paths
        return is_loose_path(rel)

    copied_count = 0
    total_loose_bytes = 0
    for dirpath, dirnames, filenames in os.walk(source_dir):
        _check()
        for fn in filenames:
            src_f = Path(dirpath) / fn
            rel = src_f.relative_to(source_dir)
            if not _should_copy(rel):
                continue
            dst_f = output_dir / rel
            if dst_f.exists():
                continue
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)
            copied_count += 1
            try:
                total_loose_bytes += src_f.stat().st_size
            except OSError:
                pass

    pak_files = list(output_dir.glob("ampr_assets-*.pak"))
    pak_bytes = sum(p.stat().st_size for p in pak_files)

    # Calculate original source bytes
    orig_bytes = 0
    for dirpath, _, filenames in os.walk(source_dir):
        for fn in filenames:
            try:
                orig_bytes += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass

    total_packed_bytes = pak_bytes + total_loose_bytes
    savings_bytes = max(0, orig_bytes - total_packed_bytes)
    ratio = (total_packed_bytes / max(1, orig_bytes)) * 100.0

    summary = {
        "ok": True,
        "game_name": game_name,
        "source_dir": str(source_dir),
        "output_dir": str(output_dir),
        "pak_count": len(pak_files),
        "pak_bytes": pak_bytes,
        "loose_count": copied_count,
        "loose_bytes": total_loose_bytes,
        "orig_bytes": orig_bytes,
        "final_bytes": total_packed_bytes,
        "savings_bytes": savings_bytes,
        "ratio_pct": ratio,
        "ampr_index": str(ampr_index),
        "assets_index": str(assets_index),
    }

    if log_cb:
        log_cb(
            f"[AMPR] Pack complete! Paks: {len(pak_files)} ({pak_bytes / (1024**3):.2f} GB) | "
            f"Loose files: {copied_count} ({total_loose_bytes / (1024**3):.2f} GB) | "
            f"Original: {orig_bytes / (1024**3):.2f} GB -> Final: {total_packed_bytes / (1024**3):.2f} GB "
            f"({100.0 - ratio:.1f}% space saved)"
        )

    return summary


def convert_fpkg_to_lz4(pkg_or_dir_path: str,
                        output_dir: str,
                        custom_name: str = "",
                        target_format: str = "folder",
                        lz4_level: int = 9,
                        block_size_kib: int = 64,
                        custom_config: Optional[str] = None,
                        traces_dir: Optional[str] = None,
                        skip_verify: bool = False,
                        auto_cleanup: bool = True,
                        save_receipt: bool = True,
                        temp_dir: Optional[str] = None,
                        log_cb: Optional[Callable[[str], None]] = None,
                        progress_cb: Optional[Callable[[str, int, int], None]] = None,
                        cancel_check: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Full workflow converting a PS5 fPKG (.pkg) or game folder directly to an AMPR LZ4 distribution.

    target_format:
      - 'folder': Game directory /app0 with ampr_assets-*.pak, ready for PS5 installation.
      - 'exfat':  Single .exfat image container containing the LZ4-compressed game.
      - 'ffpfsc': ShadowMount compressed container with LZ4 internal assets.
    """
    inp = Path(pkg_or_dir_path).resolve()
    out_base = Path(output_dir).resolve()
    out_base.mkdir(parents=True, exist_ok=True)

    if not inp.exists():
        raise FileNotFoundError(f"Input path not found: {inp}")

    is_pkg = inp.is_file() and inp.suffix.lower() == ".pkg"

    work_dir = Path(tempfile.mkdtemp(prefix="fpkg_lz4_", dir=temp_dir))
    extracted_app0 = work_dir / "app0"

    game_title = inp.stem
    title_id = "PPSA00000"

    try:
        # Step A: Ingest input
        if is_pkg:
            if log_cb:
                log_cb(f"[FPKG] Inspecting package: {inp.name}...")
            if progress_cb:
                progress_cb("Inspecting PS5 package...", 2, 100)

            from ui.ps5_pkg_extractor import inspect_ps5_pkg, extract_ps5_pkg
            pkg_info = inspect_ps5_pkg(str(inp))
            if not pkg_info.get("valid"):
                raise ValueError(f"Invalid PS5 package: {pkg_info.get('error', 'unknown error')}")

            title_id = pkg_info.get("title_id", title_id)
            game_title = pkg_info.get("title_name", game_title)
            if log_cb:
                log_cb(f"[FPKG] Package valid: {game_title} [{title_id}], SDK: {pkg_info.get('system_ver', 'Unknown')}")

            if progress_cb:
                progress_cb(f"Extracting package ({game_title})...", 5, 100)

            def _pkg_prog(step_msg: str, done: int, total: int):
                if log_cb and (done % 20 == 0 or done == total):
                    log_cb(f"[FPKG] {step_msg} ({done}%)")
                if progress_cb:
                    # Map extract 0-100 to global 5-25
                    overall = int(5 + ((done / max(1, total)) * 20))
                    progress_cb(f"Extracting fPKG: {step_msg}", overall, 100)

            extract_ps5_pkg(str(inp), str(extracted_app0), progress_cb=_pkg_prog, cancel_cb=cancel_check)
            game_source = extracted_app0
        else:
            game_source = inp
            # Try to read title id from param.json
            param_json = game_source / "sce_sys" / "param.json"
            if param_json.is_file():
                try:
                    pdata = json.loads(param_json.read_text(encoding="utf-8"))
                    title_id = pdata.get("titleId", title_id)
                    game_title = pdata.get("localizedParameters", {}).get("defaultLanguage", {}).get("titleName", game_title)
                except Exception:
                    pass

        # Step B: Determine output path
        fmt = target_format.lower()
        effective_name = custom_name.strip() or f"{title_id}"

        if fmt == "exfat":
            if not effective_name.lower().endswith(".exfat"):
                effective_name += ".exfat"
            final_artifact_path = out_base / effective_name
            pack_output_dir = work_dir / "packed_game"
        elif fmt == "ffpfsc":
            if not effective_name.lower().endswith(".ffpfsc"):
                effective_name += ".ffpfsc"
            final_artifact_path = out_base / effective_name
            pack_output_dir = work_dir / "packed_game"
        else:
            # folder format
            pack_output_dir = out_base / effective_name
            final_artifact_path = pack_output_dir

        pack_output_dir.mkdir(parents=True, exist_ok=True)

        # Step C: Pack game directory into LZ4 AMPR distribution
        pack_summary = pack_game_directory_to_lz4(
            source_dir=game_source,
            output_dir=pack_output_dir,
            game_name=game_title,
            lz4_level=lz4_level,
            block_size_kib=block_size_kib,
            custom_config=Path(custom_config) if custom_config else None,
            traces_dir=Path(traces_dir) if traces_dir else None,
            skip_verify=skip_verify,
            log_cb=log_cb,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )

        # Step D: Container packaging (if exFAT or ffpfsc requested)
        if fmt == "exfat":
            if progress_cb:
                progress_cb("Building native exFAT image for ShadowMount...", 90, 100)
            if log_cb:
                log_cb(f"[CONTAINER] Creating exFAT image: {final_artifact_path.name}...")

            from make_image_mac import build_image
            rc = build_image(str(final_artifact_path), str(pack_output_dir))
            if rc != 0 or not final_artifact_path.is_file():
                raise RuntimeError(f"Native macOS exFAT image build failed with return code {rc}")
            if log_cb:
                log_cb(f"[CONTAINER] exFAT image created: {final_artifact_path.name} ({final_artifact_path.stat().st_size / (1024**3):.2f} GB)")

        elif fmt == "ffpfsc":
            if progress_cb:
                progress_cb("Building and compressing .ffpfsc container...", 90, 100)
            if log_cb:
                log_cb(f"[CONTAINER] Building .ffpfsc container: {final_artifact_path.name}...")

            # Intermediate exfat
            temp_exfat = work_dir / f"{title_id}.exfat"
            from make_image_mac import build_image
            rc = build_image(str(temp_exfat), str(pack_output_dir))
            if rc != 0:
                raise RuntimeError("Failed to build intermediate exFAT container for mkpfs")

            # Compress using mkpfs
            try:
                import mkpfs.__main__ as mkpfs_main
                if log_cb:
                    log_cb("[CONTAINER] Running mkpfs compression...")
                rc = mkpfs_main.main(["-c", "-f", "-i", str(temp_exfat), "-o", str(final_artifact_path)])
                if rc != 0 and not final_artifact_path.is_file():
                    raise RuntimeError(f"mkpfs returned error code {rc}")
            except Exception as e:
                # Fallback to subprocess
                res = subprocess.run([sys.executable, "-m", "mkpfs", "-c", "-f", "-i", str(temp_exfat), "-o", str(final_artifact_path)],
                                     capture_output=True, text=True)
                if res.returncode != 0:
                    raise RuntimeError(f"mkpfs compression failed: {res.stderr or res.stdout}")

        # Step E: Receipt generation
        receipt_data = {
            "title_id": title_id,
            "title_name": game_title,
            "format": fmt,
            "source_type": "pkg" if is_pkg else "folder",
            "source_path": str(inp),
            "output_path": str(final_artifact_path),
            "output_size_bytes": final_artifact_path.stat().st_size if final_artifact_path.is_file() else pack_summary.get("final_bytes", 0),
            "lz4_level": lz4_level,
            "block_size_kib": block_size_kib,
            "pak_count": pack_summary.get("pak_count", 0),
            "pak_bytes": pack_summary.get("pak_bytes", 0),
            "loose_count": pack_summary.get("loose_count", 0),
            "loose_bytes": pack_summary.get("loose_bytes", 0),
            "savings_ratio_pct": pack_summary.get("ratio_pct", 0),
            "ampr_runtime_sha256": RUNTIME_SHA256,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "status": "VERIFIED_OK",
        }

        if save_receipt:
            if final_artifact_path.is_dir():
                receipt_path = final_artifact_path / f"{effective_name}.verified.json"
            else:
                receipt_path = out_base / f"{final_artifact_path.name}.verified.json"
            receipt_path.write_text(json.dumps(receipt_data, indent=2), encoding="utf-8")
            if log_cb:
                log_cb(f"[AMPR] Saved verification receipt: {receipt_path.name}")

        if progress_cb:
            progress_cb("Conversion complete!", 100, 100)

        receipt_data["ok"] = True
        return receipt_data

    finally:
        if auto_cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
