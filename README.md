# 🎮 exFAT Image Builder

### The Ultimate Cross-Platform Windows & macOS Toolkit for Building, Editing, Converting & Deploying PS5 Game Images

Build • Edit • Convert • Backport • Deploy • AMPR Toolchain

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11%20%7C%20macOS%20%28Apple%20Silicon%20%26%20Intel%29-blue?style=flat-square)
![Version](https://img.shields.io/badge/Version-v3.6.4-brightgreen?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11-yellow?style=flat-square)
![License](https://img.shields.io/badge/License-GPL%2FMIT%20Homebrew-lightgrey?style=flat-square)
![CI/CD](https://img.shields.io/badge/Builds-macOS%20DMG%20%2B%20Windows%20EXE-success?style=flat-square)

---

## 🚀 Overview

**exFAT Image Builder** is an all-in-one desktop application for preparing, editing, converting, and deploying PS5 game images for homebrew loaders like **ShadowMount+** and **MicroMount**.

Whether you need a sector-accurate **exFAT image**, a compressed **FFPFSC (PFS)** container, an **FFPKG (UFS2)** package, or want to build custom **AMPR** assets and backport games, exFAT Image Builder provides a unified graphical interface across **Windows** and **macOS**.

> ⚠️ **Homebrew & Personal Backup Tool**  
> This software is intended strictly for games and content **you legally own and dumped yourself**.  
> It **does not** download games, decrypt retail packages, bypass DRM, or provide copyrighted material.

---

## 🌟 What's New in v3.6.4

### 🍏 Native macOS Engine & Official DMG Release

* **Zero External Dependencies**: Replaces Windows-specific utilities (OSFMount, Dokan, Robocopy) with macOS-native tools (`hdiutil`, `newfs_exfat`).
* **Strict 512-Byte Sector Alignment**: Automatically enforces 512-byte sector geometry required by the PS5 kernel to mount images via ShadowMount+/MicroMount.
* **AppleDouble & Metadata Cleaning**: Automatically strips `._*` resource forks and `.DS_Store` files (`COPYFILE_DISABLE=1`) to avoid filesystem clutter and corruption.
* **Optimized Dark Theme**: Integrated `tkmacosx` ensures all buttons, headers, and UI elements look crisp and native without macOS Aqua contrast glitches.
* **Apple Silicon & Intel**: Full universal support for M1, M2, M3, M4 and x86_64 Macs running macOS 12 Monterey through macOS 15 Sequoia.
* **Native Installer**: Distributed as a drag-and-drop `.dmg` installer and `.zip` bundle.

### ⚡ AMPR Toolchain & LZ4 Block Compression

* **Comprehensive AMPR Suite**: Integrated asset pack builder, packer, profile manager, and converter toolchain (`ui/ampr_tools/`).
* **LZ4 Converter**: Multi-level LZ4 block compression (`lz4.block` / `lz4.frame`) with configurable block sizes and TOML packaging specifications.
* **Direct fPKG Conversion**: Convert PS5 fPKG packages directly to `.exfat` and `.ffpfsc` with automated TitleID (`param.json`) detection and target path parsing.

### 🔄 Enhanced Image Conversion & Extraction

* **Direct Cross-Format Pipeline**: Instant conversion between `.exfat`, `.ffpkg`, and `.ffpfsc` without manual repack steps.
* **Safe Extraction**: Improved extraction engine with directory tree preservation and automatic target directory collision handling.

### 🤖 Automated Multi-Platform CI/CD

* **GitHub Actions Workflow**: Dual build matrix that automatically generates both the **macOS `.dmg`** installer and the **Windows `.exe`** executable on every release tag, with automatic SHA-256 checksum generation.

---

## 📦 Supported Formats & Workflows

| Format | Build | Convert | Extract | Mount on PS5 | Platform Support |
| :--- | :---: | :---: | :---: | :---: | :---: |
| 📀 **.exfat** | ✅ | ✅ | ✅ | ShadowMount+ / MicroMount | Windows & macOS |
| 📦 **.ffpkg** | ✅ | ✅ | ✅ | UFS2 Package Mount | Windows & macOS |
| 🗜 **.ffpfsc** *(Compressed PFS)* | ✅ | ✅ | ✅ | ShadowMount+ / MicroMount | Windows & macOS |
| 📁 **.ffpfs** *(Uncompressed PFS)* | ✅ | — | ✅ | ShadowMount+ / MicroMount | Windows & macOS |
| 🎮 **fPKG** *(Package Dump)* | — | ✅ (to exFAT / PFS) | ✅ | Extractor / Converter | Windows & macOS |
| 📦 **AMPR / LZ4** | ✅ | ✅ | ✅ | Asset Streaming / Custom Packs | Windows & macOS |

---

## 🛠 System Requirements

### macOS

* macOS 12 (Monterey) or newer (Sonoma / Sequoia supported).
* Architecture: Apple Silicon (ARM64) or Intel (x86_64).
* **No third-party drivers needed**: Everything runs using macOS native storage tools and the bundled `mkpfs` engine.

### Windows

* Windows 10 / 11 (64-bit).
* **[OSFMount](https://www.osforensics.com/tools/mount-disk-images.html)**: Required for `.exfat` virtual drive mounting and creation.
* **[Dokan v2](https://github.com/dokan-dev/dokany/releases)**: Required for mounting `.ffpkg` images (v1 is incompatible).
* **.NET 8 Runtime**: Recommended for FFPKG tools.

---

## 💻 Installation & Quick Start

### 🍏 On macOS

1. Download the latest **`exFAT-Image-Builder-v3.6.4-macOS.dmg`** from [Releases](https://github.com/IsmaelCamC/ps5-exfat-builder-mac/releases).
2. Open the DMG and drag **exFAT Image Builder** into your `/Applications` folder.
3. Launch the app from Launchpad or Applications.

> **Note for macOS Gatekeeper (First Launch):**  
> If macOS alerts that the developer is unverified:  
> Right-click (or `Ctrl + click`) on `exFAT Image Builder.app` → select **Open** → click **Open**.  
> Alternatively, run in Terminal:  
>
> ```bash
> xattr -cr "/Applications/exFAT Image Builder.app"
> ```

### 🪟 On Windows

1. Download **`exFAT-Image-Builder-Windows.zip`** (or `exFAT Image Builder.exe`) from [Releases](https://github.com/IsmaelCamC/ps5-exfat-builder-mac/releases).
2. Extract and run `exFAT Image Builder.exe` (no installation required).
3. Ensure **OSFMount** is installed for `.exfat` operations.

---

## 🏗 Running & Building from Source

### Prerequisites

* Python 3.11 installed.

### macOS Build

```bash
# Clone repository
git clone https://github.com/IsmaelCamC/ps5-exfat-builder-mac.git
cd ps5-exfat-builder-mac

# Run app directly
python3 -m pip install -r requirements.txt # or install via build.sh
python3 exfat_builder.py

# Build macOS .app and .dmg installer
./build.sh
./release.sh
```

*Output lands in `dist/release/exFAT-Image-Builder-v3.6.4-macOS.dmg`.*

### Windows Build

```cmd
# In Command Prompt / PowerShell:
build.bat
```

*Output lands in `dist\exFAT Image Builder.exe`.*

---

## ✨ Features Checklist

* **📦 Unified Build Engine**:
  * Single-click building of `.exfat`, `.ffpkg`, and `.ffpfsc` images.
  * Batch queue: queue multiple dump conversions and builds sequentially.
  * Automatic `param.json` detection (TitleID, game name, app version).
  * 5-phase live build progress card with per-file speeds, ETA, and CPU/RAM metrics.

* **🔄 Conversion & Extraction**:
  * Direct format-to-format conversion (`exFAT` ⇄ `ffpkg` ⇄ `ffpfsc`).
  * Direct fPKG to mountable image conversion.
  * Recursive extraction with directory integrity preservation.

* **⚡ AMPR & Asset Pack Tools**:
  * Build custom AMPR asset packs with LZ4 compression profiles.
  * Configurable block sizes, compression levels, and TOML profiling.

* **🔥 Backport Toolkit**:
  * Automated SDK version detection and backport patcher.
  * Fakelib injector, language stripper, and file restoration.

* **🎮 PS5 Remote Integration**:
  * Built-in FTP client & file explorer for internal/external PS5 storage.
  * Remote Content Manager, Payload sender, and Live Kernel Log viewer.
  * Configuration editors for ShadowMount+ and MicroMount.

* **📚 Collection Library**:
  * Multi-directory scanner with automatic cover art fetching and metadata display.
  * Batch renaming tool with PPSA confidence scoring.

---

## ❤️ Credits & Acknowledgments

Special thanks to the PS5 homebrew community and tool authors:

* **Nazky**
* **BestPig**
* **SvenGDK**
* **drakmor**
* **PSBrew**
* **john-tornblom**
* **ps5-payload-dev**
* **idlesauce**
* **NookieAI**
* **stonemodder**

See [THIRD_PARTY_NOTICES.md](file:///Users/ismaelcam/Development/ps5-exfat-builder/ps5-exfat-builder-mac/THIRD_PARTY_NOTICES.md) for full licensing information.

---

## ⚠ Disclaimer

This software is strictly for managing content you personally own and dumped. It is **not affiliated with or endorsed by Sony Interactive Entertainment**. Use at your own discretion.
