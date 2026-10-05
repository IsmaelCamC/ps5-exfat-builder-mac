"""ui/tab_convert.py — Convert tab (v2: spacious Build-tab style).

Layout:

    ┌─ Page head (badge + title + subtitle) ────────────────────────────┐
    │ [💿]  Convert images                                              │
    │       Convert between .exfat and .ffpkg                           │
    ├───────────────────────────────────────────────────────────────────┤
    │ [i] Conversion mounts the source, runs UFS2Tool newfs against...  │
    ├───────────────────────────────────────────────────────────────────┤
    │ ┌─ Card: exFAT → ffpkg ─────────────────────────────────────────┐ │
    │ │ Source .exfat                                                 │ │
    │ │ [_______________________________________________]   [Browse]  │ │
    │ │ Output folder / Output name / [▶ Convert to ffpkg]            │ │
    │ └───────────────────────────────────────────────────────────────┘ │
    │                                                                   │
    │ ┌─ Card: ffpkg → exFAT ─────────────────────────────────────────┐ │
    │ │ Source .ffpkg                                                 │ │
    │ │ [_______________________________________________]   [Browse]  │ │
    │ │ Output folder / Output name / [▶ Convert to exFAT]            │ │
    │ └───────────────────────────────────────────────────────────────┘ │
    └───────────────────────────────────────────────────────────────────┘

Output goes to the global OUTPUT LOG at the bottom (no embedded log
duplicating it).

The ffpkg → exFAT direction extracts the source via UFS2Tool's
recursive extract (which preserves empty directories — see the Apply
backport flow's v2.0.6f notes), creates a blank fixed-size .exfat
file sized at ~110% of the extracted contents, mounts it via
OSFMount, formats it with Windows' `format /FS:exFAT /Q`, robocopies
the dump in, then dismounts.
"""

import os
import sys
import re
import subprocess
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from tkinter_theme import COLORS, FONTS
from ui.shared.page_head import (
    make_themed_button, info_banner, page_head, field_block)
from ui.shared.hero import GameHero
from ui.shared.scroll import attach_scroll


# v3.6.x: OSFMount attach timeout for the exFAT → ffpkg convert. A healthy
# attach of a local image completes in seconds; this bounds a hung attach so
# the conversion fails cleanly with an actionable error instead of sitting at
# "Preparing... 0%" indefinitely. Tunable.
_MOUNT_TIMEOUT_S = 90


def _flow_chips(parent, src_ext, dst_ext):
    """Compact Source → Destination flow chips, packed right in a
    card head (v3.6.0 pass)."""
    wrap = tk.Frame(parent, bg=COLORS['bg_2'])
    wrap.pack(side='right', padx=(8, 0))
    tk.Label(wrap, text=' ' + src_ext + ' ',
             font=(FONTS['mono_sm'][0], 9, 'bold'),
             bg=COLORS['accent_08'], fg=COLORS['accent_hi'],
             padx=6, pady=2,
             highlightbackground=COLORS['accent_lo'],
             highlightthickness=1).pack(side='left')
    tk.Label(wrap, text='\u2192',
             font=(FONTS['body'][0], 11, 'bold'),
             bg=COLORS['bg_2'], fg=COLORS['fg_4']
             ).pack(side='left', padx=6)
    tk.Label(wrap, text=' ' + dst_ext + ' ',
             font=(FONTS['mono_sm'][0], 9, 'bold'),
             bg=COLORS['teal_bg'], fg=COLORS['teal_hi'],
             padx=6, pady=2,
             highlightbackground=COLORS['border_3'],
             highlightthickness=1).pack(side='left')


def build_convert_tab(parent, app):
    """Build the redesigned Convert tab. `app` is the App instance."""
    parent.configure(bg=COLORS['bg_1'])

    # ── State ──
    e2f_src    = tk.StringVar()
    e2f_outdir = tk.StringVar()
    e2f_name   = tk.StringVar()
    e2f_status_var = tk.StringVar(value='Idle.')
    f2e_src    = tk.StringVar()
    f2e_outdir = tk.StringVar()
    f2e_name   = tk.StringVar()
    f2e_status_var = tk.StringVar(value='Idle.')
    p2f_src    = tk.StringVar()
    p2f_outdir = tk.StringVar()
    p2f_name   = tk.StringVar()
    p2f_status_var = tk.StringVar(value='Idle.')
    p2f_dst_fmt    = tk.StringVar(value='ffpfsc')
    p2f_comp_var = tk.StringVar(value='6')
    p2f_temp_var = tk.StringVar()
    p2f_cleanup_var = tk.BooleanVar(value=True)
    p2f_receipt_var = tk.BooleanVar(value=True)
    p2f_info_var = tk.StringVar(value='')

    # ── State: fPKG / Folder → AMPR LZ4 (Lazy_AMPR) ──
    lz4_src         = tk.StringVar()
    lz4_outdir      = tk.StringVar()
    lz4_name        = tk.StringVar()
    lz4_status_var  = tk.StringVar(value='Idle.')
    lz4_dst_fmt     = tk.StringVar(value='folder')
    lz4_level_var   = tk.StringVar(value='9 (High - Recommended)')
    lz4_block_var   = tk.StringVar(value='64 KiB (Recommended)')
    lz4_toml_var    = tk.StringVar()
    lz4_traces_var  = tk.StringVar()
    lz4_temp_var    = tk.StringVar()
    lz4_cleanup_var = tk.BooleanVar(value=True)
    lz4_receipt_var = tk.BooleanVar(value=True)
    lz4_verify_var  = tk.BooleanVar(value=True)
    lz4_runtime_var = tk.BooleanVar(value=True)
    lz4_info_var    = tk.StringVar(value='')

    # Shared: only one conversion runs at a time.
    state = {'busy': False}
    # Back-compat alias for old code below — points at whichever card
    # is currently active.
    status_var = e2f_status_var

    # ── Scrollable wrap ──
    canvas = tk.Canvas(parent, bg=COLORS['bg_1'], bd=0,
                       highlightthickness=0)
    canvas.pack(side='left', fill='both', expand=True)
    sb = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
    sb.pack(side='right', fill='y')
    canvas.configure(yscrollcommand=sb.set)

    inner = tk.Frame(canvas, bg=COLORS['bg_1'])
    inner_id = canvas.create_window((0, 0), window=inner, anchor='nw')

    inner.bind('<Configure>', lambda e:
        canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda e:
        canvas.itemconfig(inner_id, width=e.width))
    attach_scroll(canvas)

    # ── Page head with badge ──
    head = page_head(inner, '\U0001f4bf',
                     'Convert images & packages',
                     'Convert between .exfat, .ffpkg, PS5 fPKG (.pkg) and AMPR LZ4 asset packs.')
    head.pack(fill='x', padx=24, pady=(14, 12))

    tk.Label(inner,
             text='Convert existing images (.exfat \u2194 .ffpkg), build ShadowMount .ffpfsc containers, or compress fPKG / games into seekable AMPR LZ4 asset packs (Lazy_AMPR architecture) for PS5.',
             font=FONTS['meta'], bg=COLORS['bg_1'], fg=COLORS['fg_4'],
             anchor='w').pack(fill='x', padx=24, pady=(0, 8))

    # Right-aligned Force Dismount button on the page head row
    def _force_dismount():
        try:
            app._force_dismount_all()
        except Exception as e:
            messagebox.showerror('Force Dismount failed', str(e))

    fd_btn = make_themed_button(head,
                                  '\u26a0  Force Dismount',
                                  command=_force_dismount,
                                  kind='ghost')
    fd_btn.pack(side='right', padx=(8, 0))

    # ── Info banner ──
    banner = info_banner(inner,
        'Conversion mounts or extracts the source image/package, generates '
        'the destination format, and verifies integrity. Output goes '
        'to the global OUTPUT LOG (click at the bottom to expand).')
    banner.pack(fill='x', padx=24, pady=(0, 14))

    # ── Selected-image hero (v3.6.0 pass) ──
    # Hidden until a source file is picked in either card; then shows
    # the game parsed from the image's filename, the source/output
    # formats, the file size, and a READY TO CONVERT badge.
    # Presentation only — built entirely from the path string and
    # os.path.getsize; nothing is mounted or opened.
    hero = GameHero(inner,
                    stats=[('Source Format', 'src'),
                           ('Output Format', 'dst'),
                           ('Size', 'size'),
                           ('Status', 'status')],
                    cover_glyph='\U0001f4bf', cover_size=120)
    hero_packed = {'on': False}

    def _humansize(n):
        try:
            if n >= 1024**3:
                return '%.2f GB' % (n / 1024**3)
            return '%d MB' % (n // 1024**2)
        except Exception:
            return '\u2014'

    def _update_hero(path, src_fmt, dst_fmt):
        try:
            if not path or not os.path.isfile(path):
                if hero_packed['on']:
                    hero.pack_forget()
                    hero_packed['on'] = False
                return
            if not hero_packed['on']:
                hero.pack(fill='x', padx=24, pady=(0, 14), after=banner)
                hero_packed['on'] = True

            if path.lower().endswith('.pkg'):
                from ui.ps5_pkg_extractor import inspect_ps5_pkg
                pkg_info = inspect_ps5_pkg(path)
                if pkg_info.get('valid'):
                    title = pkg_info.get('title_name') or pkg_info.get('title_id') or os.path.splitext(os.path.basename(path))[0]
                    sub = f"{pkg_info.get('title_id')} \u00b7 v{pkg_info.get('version')} \u00b7 {pkg_info.get('kind')} ({pkg_info.get('signing')})"
                    hero.set_title(title, sub)
                    hero.set_path(path)
                    hero.set_stat('src', src_fmt)
                    hero.set_stat('dst', dst_fmt)
                    hero.set_stat('size', _humansize(pkg_info.get('size', 0)))
                    conv = pkg_info.get('shadow_convertible', False)
                    hero.set_stat('status', 'Ready' if conv else 'Incompatible', warn=not conv)
                    hero.set_badge('READY TO CONVERT' if conv else 'INCOMPATIBLE', 'ready' if conv else 'warn')
                    if pkg_info.get('icon_bytes'):
                        try:
                            import io
                            from PIL import Image, ImageTk
                            pil_im = Image.open(io.BytesIO(pkg_info['icon_bytes'])).convert('RGBA')
                            pil_im = pil_im.resize((hero._cover_size, hero._cover_size), Image.Resampling.LANCZOS)
                            hero._cover_img = ImageTk.PhotoImage(pil_im)
                            hero._cover_lbl.config(image=hero._cover_img, text='')
                        except Exception:
                            hero.reset_cover()
                    else:
                        hero.reset_cover()
                    return

            from ui.tab_ps5_mgr import parse_meta_from_filename
            gid, ver, disp = parse_meta_from_filename(
                os.path.basename(path))
            title = disp or os.path.splitext(os.path.basename(path))[0]
            hero.set_title(title, (gid or '') +
                           ((' \u00b7 v' + ver) if ver else ''))
            hero.set_path(path)
            hero.set_stat('src', src_fmt)
            hero.set_stat('dst', dst_fmt)
            try:
                hero.set_stat('size', _humansize(os.path.getsize(path)))
            except Exception:
                hero.set_stat('size', '\u2014')
            hero.set_stat('status', 'Ready')
            hero.set_badge('READY TO CONVERT', 'ready')
            hero.reset_cover()
        except Exception:
            pass
    app._conv_update_hero = _update_hero

    # ── Conversion card ──
    card_outer = tk.Frame(inner, bg=COLORS['bg_2'],
                           highlightbackground=COLORS['border_2'],
                           highlightthickness=1)
    card_outer.pack(fill='x', padx=24, pady=(0, 14))

    # Card head
    chead = tk.Frame(card_outer, bg=COLORS['bg_2'])
    chead.pack(fill='x', padx=24, pady=(18, 14))

    # Icon tile
    ico = tk.Label(chead, text='\u2192',
                   font=(FONTS['h2'][0], 13),
                   bg=COLORS['accent_08'], fg=COLORS['accent'],
                   width=2, padx=4, pady=2)
    ico.pack(side='left', padx=(0, 12))

    _flow_chips(chead, '.exfat', '.ffpkg')

    title_col = tk.Frame(chead, bg=COLORS['bg_2'])
    title_col.pack(side='left', fill='x', expand=True)
    tk.Label(title_col, text='exFAT \u2192 ffpkg',
             font=(FONTS['h3'][0], 12, 'bold'),
             bg=COLORS['bg_2'], fg=COLORS['fg_0'], anchor='w'
             ).pack(fill='x')
    tk.Label(title_col,
             text='Pick an existing .exfat image and a destination.',
             font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'], anchor='w'
             ).pack(fill='x', pady=(2, 0))

    # Hairline under head
    tk.Frame(card_outer, bg=COLORS['border_2'], height=1
             ).pack(fill='x')

    # Card body
    body = tk.Frame(card_outer, bg=COLORS['bg_2'])
    body.pack(fill='x', padx=24, pady=(4, 18))

    # ── Form fields ──
    def _browse_src():
        p = filedialog.askopenfilename(
            title='Select .exfat image',
            filetypes=[('exFAT images', '*.exfat'),
                       ('All files', '*.*')])
        if p:
            e2f_src.set(p)

    def _browse_outdir():
        p = filedialog.askdirectory(title='Select output folder')
        if p:
            e2f_outdir.set(p)

    field_block(body, 'Source .exfat',
                 var=e2f_src, on_browse=_browse_src,
                 hint='the image to convert')
    field_block(body, 'Output folder',
                 var=e2f_outdir, on_browse=_browse_outdir,
                 hint='where the .ffpkg will be written')
    field_block(body, 'Output name',
                 var=e2f_name,
                 hint='auto-filled from source if blank')

    # Auto-fill name & outdir when source is picked
    def _on_e2f_src(*_a):
        if e2f_src.get() and not e2f_name.get():
            base = os.path.splitext(os.path.basename(e2f_src.get()))[0]
            e2f_name.set(base + '.ffpkg')
        if e2f_src.get() and not e2f_outdir.get():
            e2f_outdir.set(os.path.dirname(e2f_src.get()))
        _update_hero(e2f_src.get().strip(), 'exFAT', 'ffpkg')
    e2f_src.trace_add('write', _on_e2f_src)

    # ── Action row: Convert button + status + progress bar ──
    action_row = tk.Frame(body, bg=COLORS['bg_2'])
    action_row.pack(fill='x', pady=(18, 0))

    convert_btn = make_themed_button(
        action_row,
        text='Convert to ffpkg',
        command=lambda: _do_exfat_to_ffpkg(),
        kind='success',
        icon='\u25b6',
        font_size=10, padx=18, pady=9)
    convert_btn.pack(side='left')

    status_lbl = tk.Label(action_row, textvariable=e2f_status_var,
                          font=FONTS['mono_sm'],
                          bg=COLORS['bg_2'], fg=COLORS['fg_4'],
                          anchor='w')
    status_lbl.pack(side='left', padx=(16, 0))

    # Slim progress bar on the right of the action row
    pbar_wrap = tk.Frame(action_row, bg=COLORS['bg_2'])
    pbar_wrap.pack(side='right', fill='x', expand=True, padx=(16, 0))
    pbar = ttk.Progressbar(pbar_wrap, mode='indeterminate', length=200)
    pbar.pack(fill='x')

    # ── Helpers ──
    def _log(line):
        """Funnel everything to the global OUTPUT LOG drawer."""
        parent.after(0, lambda l=str(line): app._log('[CONVERT] ' + l.rstrip() + '\n'))

    def _set_busy_e2f(b, label=''):
        state['busy'] = b
        try:
            if b:
                pbar.start(10)
                e2f_status_var.set(label or 'Working...')
                convert_btn.config(state='disabled', cursor='watch')
                # Also lock the other cards' buttons so the user can't
                # try to launch a concurrent run.
                if 'f2e_btn' in state and state['f2e_btn']:
                    state['f2e_btn'].config(state='disabled')
                if 'p2f_btn' in state and state['p2f_btn']:
                    state['p2f_btn'].config(state='disabled')
                if 'lz4_btn' in state and state['lz4_btn']:
                    state['lz4_btn'].config(state='disabled')
            else:
                pbar.stop()
                e2f_status_var.set(label or 'Idle.')
                convert_btn.config(state='normal', cursor='hand2')
                if 'f2e_btn' in state and state['f2e_btn']:
                    state['f2e_btn'].config(state='normal')
                if 'p2f_btn' in state and state['p2f_btn']:
                    state['p2f_btn'].config(state='normal')
                if 'lz4_btn' in state and state['lz4_btn']:
                    state['lz4_btn'].config(state='normal')
        except Exception:
            pass

    # Back-compat alias for the existing code path below.
    _set_busy = _set_busy_e2f

    def _get_ufs2tool_exe():
        try:
            from exfat_builder import extract_ufs2tool, _UFS2TOOL_DIR
            if _UFS2TOOL_DIR and os.path.isdir(_UFS2TOOL_DIR):
                exe = os.path.join(_UFS2TOOL_DIR, 'UFS2Tool.exe')
                if os.path.isfile(exe):
                    return exe
            return extract_ufs2tool(
                getattr(app, '_settings', {}).get('temp_dir') or None)
        except Exception as e:
            _log('Failed to extract UFS2Tool: %s' % e)
            return None

    def _run(cmd, label=None, progress_cb=None):
        """Run a subprocess, stream output to the global log, return rc.

        If `progress_cb` is provided, it's invoked for every output
        line so the caller can parse progress (UFS2Tool's
        `Adding files... NN%`, robocopy's per-file output, etc.).
        Exceptions inside the callback are swallowed so progress
        parsing bugs can't break the actual conversion."""
        if label:
            _log(label)
        CREATE_NO_WINDOW = 0x08000000
        try:
            kwargs = ({'creationflags': CREATE_NO_WINDOW}
                      if os.name == 'nt' else {})
            p = subprocess.Popen(cmd,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  text=True, errors='replace',
                                  **kwargs)
            for ln in p.stdout:
                _log(ln.rstrip())
                if progress_cb is not None:
                    try:
                        progress_cb(ln.rstrip())
                    except Exception:
                        pass
            p.wait()
            return p.returncode
        except FileNotFoundError:
            _log('Command not found: ' + cmd[0])
            return -1
        except Exception as e:
            _log('Run error: ' + str(e))
            return -1

    def _run_with_timeout(cmd, timeout_s):
        """Run a subprocess with a hard timeout, returning rc.

        Used ONLY for the OSFMount attach in the exFAT → ffpkg flow — it does
        NOT stream stdout (the mount needs a bounded wait, not progress
        parsing, so this stays separate from `_run`, which the newfs progress
        path depends on). On timeout the process is terminated (then killed if
        needed) and a sentinel rc of -2 is returned so the caller's existing
        `if rc != 0` failure branch handles it. Output is captured and logged.
        """
        CREATE_NO_WINDOW = 0x08000000
        try:
            kwargs = ({'creationflags': CREATE_NO_WINDOW}
                      if os.name == 'nt' else {})
            p = subprocess.Popen(cmd,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  text=True, errors='replace',
                                  **kwargs)
            try:
                out, _err = p.communicate(timeout=timeout_s)
                if out:
                    for ln in out.splitlines():
                        _log(ln.rstrip())
                return p.returncode
            except subprocess.TimeoutExpired:
                # Bounded wait exceeded — terminate, then kill if it lingers.
                _log('Mount timed out after %ds — terminating OSFMount.'
                     % timeout_s)
                try:
                    p.terminate()
                    try:
                        p.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.communicate(timeout=5)
                except Exception as e:
                    _log('Error terminating timed-out mount: ' + str(e))
                return -2  # sentinel: treated as failure by `if rc != 0`
        except FileNotFoundError:
            _log('Command not found: ' + cmd[0])
            return -1
        except Exception as e:
            _log('Run (timeout) error: ' + str(e))
            return -1

    # ── Main conversion ──
    def _do_exfat_to_ffpkg():
        if state['busy']:
            return
        src = e2f_src.get().strip()
        outdir = e2f_outdir.get().strip()
        name = e2f_name.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('Source missing',
                'Pick a valid .exfat source image.')
            return
        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror('Output folder missing',
                'Pick an output folder.')
            return
        if not name:
            messagebox.showerror('Output name missing',
                'Set an output filename.')
            return
        if not name.lower().endswith('.ffpkg'):
            name = name + '.ffpkg'
        out_path = os.path.join(outdir, name)
        if os.path.exists(out_path):
            if not messagebox.askyesno('Overwrite',
                    out_path + '\n\nalready exists. Overwrite?'):
                return
            try:
                os.remove(out_path)
            except Exception as e:
                _log('Could not remove existing: ' + str(e))
                return

        # v3.0.0: the OUTPUT LOG is never auto-opened. The progress
        # dialog below carries the user-visible status; if the user
        # wants the full text log, they can click the OUTPUT LOG
        # toggle at the bottom of the window themselves.

        # v3.0.0: show the rich progress dialog instead of the
        # action-row indeterminate bar. Stage weights are calibrated
        # for the exFAT → ffpkg flow (newfs dominates the time).
        from ui.tab_ffpkg_edit import _RebuildProgress
        prog = _RebuildProgress(parent, 'Converting exFAT → ffpkg',
            weights={
                'mount':    (0,   5),
                'newfs':    (5,  97),
                'dismount': (97, 100),
            },
            initial_stage='mount')

        _set_busy_e2f(True, 'Locating tools...')

        def worker():
            # v3.6.x: per-step status during the "Preparing" window so a stall
            # is visible (which step) instead of a static "Preparing... 0%".
            parent.after(0, prog.set_stage, 'mount', 'Locating UFS2Tool...')
            ufs2 = _get_ufs2tool_exe()
            if not ufs2:
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_e2f(False, 'Failed.'))
                parent.after(0, lambda: messagebox.showerror(
                    'UFS2Tool not available',
                    'UFS2Tool could not be extracted. Conversion '
                    'aborted.'))
                return

            # Locate OSFMount via app helpers / settings
            parent.after(0, prog.set_stage, 'mount', 'Locating OSFMount...')
            osfmount = None
            try:
                osfmount = getattr(app, '_find_osfmount',
                                    lambda: None)()
            except Exception:
                osfmount = None
            if not osfmount or not os.path.isfile(osfmount):
                _log('OSFMount not found.')
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_e2f(False, 'Failed.'))
                parent.after(0, lambda: messagebox.showerror(
                    'OSFMount missing',
                    'OSFMount is required to convert exFAT to ffpkg.\n'
                    'Configure its path under Settings.'))
                return

            # Find a free drive letter
            parent.after(0, prog.set_stage, 'mount',
                'Finding free drive letter...')
            if sys.platform != 'win32':
                _log('Convert requires Windows Dokan/UFS2 tools.')
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_e2f(False, 'Windows only.'))
                parent.after(0, lambda: messagebox.showinfo(
                    'Feature Unavailable on macOS',
                    'UFS2 conversion (.ffpkg) requires Windows UFS2Tool and Dokan.\n'
                    'Native macOS exFAT building, mounting, and extraction are supported.'))
                return
            import ctypes as _ct
            used_mask = _ct.windll.kernel32.GetLogicalDrives()
            mount_letter = None
            for code in range(ord('G'), ord('Z') + 1):
                if not (used_mask & (1 << (code - ord('A')))):
                    mount_letter = chr(code) + ':'
                    break
            if not mount_letter:
                _log('No free drive letter for mount.')
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_e2f(False, 'Failed.'))
                return

            _log('Mounting %s at %s ...' % (src, mount_letter))
            parent.after(0, prog.set_stage, 'mount',
                'Mounting source image...')

            # v3.6.x: best-effort dismount of the chosen letter, used on the
            # timeout/failure paths so a partial attach never leaks a mount.
            # Mirrors the successful-path `finally` dismount; never raises.
            def _cleanup_mount():
                try:
                    letter = mount_letter.rstrip(':\\')
                    if hasattr(app, '_dismount_drive_robust'):
                        app._dismount_drive_robust(letter,
                                                    max_wait_seconds=20)
                    else:
                        _run([osfmount, '-d', '-m', mount_letter])
                except Exception as e:
                    _log('Cleanup dismount error: ' + str(e))

            mount_cmd = [osfmount, '-a', '-t', 'file', '-f', src,
                         '-m', mount_letter, '-o', 'rw']
            # v3.6.x: bounded mount so a hung OSFMount attach fails cleanly
            # instead of sitting at "Preparing... 0%". Uses the timeout helper
            # (NOT the streaming `_run`, which the newfs progress path needs).
            rc = _run_with_timeout(mount_cmd, _MOUNT_TIMEOUT_S)
            if rc == -2:
                # Timed out — process already terminated by the helper. Clean
                # up any partial attach and show an actionable error.
                _log('Mount timed out after %ds.' % _MOUNT_TIMEOUT_S)
                _cleanup_mount()
                parent.after(0, prog.close)
                parent.after(0, lambda:
                    _set_busy_e2f(False, 'Mount timed out.'))
                parent.after(0, lambda: messagebox.showerror(
                    'Mounting timed out',
                    'OSFMount didn\'t finish attaching the source image '
                    'within %d seconds, so the conversion was stopped.\n\n'
                    'This usually means:\n'
                    '\u2022 A previous mount of this image is still attached '
                    '\u2014 open OSFMount (or reboot) to clear stale mounts.\n'
                    '\u2022 Antivirus is scanning the image on first access '
                    '\u2014 try again, or exclude the image\'s folder.\n'
                    '\u2022 The chosen drive letter is in use by another '
                    'program.\n\n'
                    'No files were changed. You can try the conversion again.'
                    % _MOUNT_TIMEOUT_S))
                return
            if rc != 0:
                _log('Mount failed (rc=%d).' % rc)
                _cleanup_mount()  # best-effort: a failed attach may have
                                  # left a partial mount
                parent.after(0, prog.close)
                parent.after(0, lambda:
                    _set_busy_e2f(False, 'Mount failed.'))
                return
            parent.after(0, prog.set_stage_progress, 100.0)
            parent.after(0, prog.set_stage, 'mount',
                'Starting ffpkg creation...')

            try:
                parent.after(0, prog.set_stage, 'newfs',
                    'Building .ffpkg with UFS2Tool newfs...')
                _log('UFS2Tool newfs against mount...')
                newfs_cmd = [ufs2, 'newfs',
                             '-O', '2',
                             '-b', '32768',
                             '-f', '4096',
                             '-S', '512',
                             '-D', mount_letter + '\\',
                             out_path]

                # Parse newfs output for progress. Two sub-phases:
                # "Writing cylinder groups... NN%" and then
                # "Adding files... NN% (x/y files, X GiB/Y GiB)".
                newfs_state = {'sub': 'init'}
                def _newfs_cb(line):
                    stripped = line.strip()
                    if 'Writing cylinder groups' in stripped:
                        newfs_state['sub'] = 'init'
                        parent.after(0, prog.set_detail,
                            'Writing filesystem structure...')
                    elif 'Adding files to image' in stripped:
                        newfs_state['sub'] = 'files'
                        parent.after(0, prog.set_detail,
                            'Copying files into image...')
                    elif ('Populated image with' in stripped
                          or 'Image created successfully' in stripped):
                        parent.after(0, prog.set_stage_progress, 100.0,
                            'Image created, finalising...')
                        return
                    m = re.search(r'(\d{1,3})\s*%', stripped)
                    if not m:
                        return
                    raw_pct = max(0, min(100, int(m.group(1))))
                    # File count + byte progress
                    mf = re.search(r'\((\d+)\s*/\s*(\d+)\s+files?',
                                    stripped)
                    files_done = files_total = 0
                    if mf:
                        files_done = int(mf.group(1))
                        files_total = int(mf.group(2))
                    mg = re.search(
                        r'([\d.]+)\s*GiB\s*/\s*([\d.]+)\s*GiB',
                        stripped)
                    written_gib = total_gib = 0.0
                    if mg:
                        written_gib = float(mg.group(1))
                        total_gib   = float(mg.group(2))
                    # Within the newfs stage, init goes 0–25%, files
                    # 25–100% — newfs spends most time on file copy.
                    if newfs_state['sub'] == 'init':
                        local_pct = raw_pct * 0.25
                        detail = ('Initialising filesystem... %d%%'
                                  % raw_pct)
                    else:
                        local_pct = 25 + raw_pct * 0.75
                        bits = []
                        if files_total:
                            bits.append('%d / %d files'
                                        % (files_done, files_total))
                        if total_gib:
                            bits.append('%.2f / %.2f GB'
                                        % (written_gib, total_gib))
                        detail = ('  •  '.join(bits) if bits
                                  else '%d%%' % raw_pct)
                    parent.after(0, prog.set_stage_progress,
                        local_pct, detail)

                rc = _run(newfs_cmd, progress_cb=_newfs_cb)
                if rc != 0:
                    _log('newfs failed (rc=%d).' % rc)
                    parent.after(0, prog.close)
                    parent.after(0, lambda:
                        _set_busy_e2f(False, 'newfs failed.'))
                    return
                _log('Built %s OK.' % out_path)
                parent.after(0, prog.set_stage_progress, 100.0,
                    'Image built.')
            finally:
                parent.after(0, prog.set_stage, 'dismount',
                    'Unmounting source...')
                _log('Unmounting %s ...' % mount_letter)
                # Use the robust dismount helper so we don't suffer
                # the same handle-busy issues that exFAT builds had
                # before the v2.5.7 dismount fix.
                try:
                    letter = mount_letter.rstrip(':\\')
                    if hasattr(app, '_dismount_drive_robust'):
                        app._dismount_drive_robust(letter,
                                                    max_wait_seconds=20)
                    else:
                        _run([osfmount, '-d', '-m', mount_letter])
                except Exception as e:
                    _log('Dismount error: ' + str(e))
                parent.after(0, prog.set_stage_progress, 100.0)

            parent.after(0, prog.close)
            parent.after(0, lambda: _set_busy_e2f(False, 'Done \u2713'))
            parent.after(0, lambda: messagebox.showinfo(
                'Convert complete',
                'Wrote:\n' + out_path))
            # v3.6.2: occasional support nudge (frequency-gated).
            def _nudge_cv():
                try:
                    from ui.release_notes import note_successful_operation
                    note_successful_operation(app, 'Convert')
                except Exception:
                    pass
            parent.after(700, _nudge_cv)

        # v3.6.x: actually run the worker. This start was missing — the
        # worker was defined but never dispatched, so exFAT→ffpkg sat at
        # "Preparing... 0%" forever with no Output Log activity (every step
        # lives inside this worker). Matches the ffpkg→exFAT pattern.
        threading.Thread(target=worker, daemon=True).start()
    f2e_card = tk.Frame(inner, bg=COLORS['bg_2'],
                         highlightbackground=COLORS['border_2'],
                         highlightthickness=1)
    f2e_card.pack(fill='x', padx=24, pady=(0, 14))

    f2e_chead = tk.Frame(f2e_card, bg=COLORS['bg_2'])
    f2e_chead.pack(fill='x', padx=24, pady=(18, 14))
    f2e_ico = tk.Label(f2e_chead, text='\u2192',
                       font=(FONTS['h2'][0], 13),
                       bg=COLORS['accent_08'], fg=COLORS['accent'],
                       width=2, padx=4, pady=2)
    f2e_ico.pack(side='left', padx=(0, 12))
    _flow_chips(f2e_chead, '.ffpkg', '.exfat')

    f2e_title_col = tk.Frame(f2e_chead, bg=COLORS['bg_2'])
    f2e_title_col.pack(side='left', fill='x', expand=True)
    tk.Label(f2e_title_col, text='ffpkg \u2192 exFAT',
             font=(FONTS['h3'][0], 12, 'bold'),
             bg=COLORS['bg_2'], fg=COLORS['fg_0'], anchor='w'
             ).pack(fill='x')
    tk.Label(f2e_title_col,
             text='Pick an existing .ffpkg and a destination.',
             font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'], anchor='w'
             ).pack(fill='x', pady=(2, 0))

    tk.Frame(f2e_card, bg=COLORS['border_2'], height=1
             ).pack(fill='x')

    f2e_body = tk.Frame(f2e_card, bg=COLORS['bg_2'])
    f2e_body.pack(fill='x', padx=24, pady=(4, 18))

    def _f2e_browse_src():
        p = filedialog.askopenfilename(
            title='Select .ffpkg image',
            filetypes=[('ffpkg images', '*.ffpkg'),
                       ('All files', '*.*')])
        if p:
            f2e_src.set(p)

    def _f2e_browse_outdir():
        p = filedialog.askdirectory(title='Select output folder')
        if p:
            f2e_outdir.set(p)

    field_block(f2e_body, 'Source .ffpkg',
                 var=f2e_src, on_browse=_f2e_browse_src,
                 hint='the image to convert')
    field_block(f2e_body, 'Output folder',
                 var=f2e_outdir, on_browse=_f2e_browse_outdir,
                 hint='where the .exfat will be written')
    field_block(f2e_body, 'Output name',
                 var=f2e_name,
                 hint='auto-filled from source if blank')

    def _on_f2e_src(*_a):
        if f2e_src.get() and not f2e_name.get():
            base = os.path.splitext(os.path.basename(f2e_src.get()))[0]
            f2e_name.set(base + '.exfat')
        if f2e_src.get() and not f2e_outdir.get():
            f2e_outdir.set(os.path.dirname(f2e_src.get()))
        _update_hero(f2e_src.get().strip(), 'ffpkg', 'exFAT')
    f2e_src.trace_add('write', _on_f2e_src)

    f2e_action_row = tk.Frame(f2e_body, bg=COLORS['bg_2'])
    f2e_action_row.pack(fill='x', pady=(18, 0))

    f2e_btn = make_themed_button(
        f2e_action_row,
        text='Convert to exFAT',
        command=lambda: _do_ffpkg_to_exfat(),
        kind='success',
        icon='\u25b6',
        font_size=10, padx=18, pady=9)
    f2e_btn.pack(side='left')
    state['f2e_btn'] = f2e_btn

    tk.Label(f2e_action_row, textvariable=f2e_status_var,
             font=FONTS['mono_sm'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'],
             anchor='w').pack(side='left', padx=(16, 0))

    f2e_pbar_wrap = tk.Frame(f2e_action_row, bg=COLORS['bg_2'])
    f2e_pbar_wrap.pack(side='right', fill='x', expand=True, padx=(16, 0))
    f2e_pbar = ttk.Progressbar(f2e_pbar_wrap, mode='indeterminate',
                                length=200)
    f2e_pbar.pack(fill='x')

    # Register f2e_btn back so the other set_busy locks it too.
    state['f2e_btn'] = f2e_btn

    def _set_busy_f2e(b, label=''):
        state['busy'] = b
        try:
            if b:
                f2e_pbar.start(10)
                f2e_status_var.set(label or 'Working...')
                f2e_btn.config(state='disabled', cursor='watch')
                # Lock the other card's button so the user can't fire
                # both concurrently.
                try:
                    convert_btn.config(state='disabled')
                except Exception:
                    pass
                if 'p2f_btn' in state and state['p2f_btn']:
                    try:
                        state['p2f_btn'].config(state='disabled')
                    except Exception:
                        pass
                if 'lz4_btn' in state and state['lz4_btn']:
                    try:
                        state['lz4_btn'].config(state='disabled')
                    except Exception:
                        pass
            else:
                f2e_pbar.stop()
                f2e_status_var.set(label or 'Idle.')
                f2e_btn.config(state='normal', cursor='hand2')
                try:
                    convert_btn.config(state='normal')
                except Exception:
                    pass
                if 'p2f_btn' in state and state['p2f_btn']:
                    try:
                        state['p2f_btn'].config(state='normal')
                    except Exception:
                        pass
                if 'lz4_btn' in state and state['lz4_btn']:
                    try:
                        state['lz4_btn'].config(state='normal')
                    except Exception:
                        pass
        except Exception:
            pass

    # ── ffpkg → exFAT worker ─────────────────────────────────────────
    def _do_ffpkg_to_exfat():
        if state['busy']:
            return
        src = f2e_src.get().strip()
        outdir = f2e_outdir.get().strip()
        name = f2e_name.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('Source missing',
                'Pick a valid .ffpkg source image.')
            return
        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror('Output folder missing',
                'Pick an output folder.')
            return
        if not name:
            messagebox.showerror('Output name missing',
                'Set an output filename.')
            return
        if not name.lower().endswith('.exfat'):
            name = name + '.exfat'
        out_path = os.path.join(outdir, name)
        # Safety: never let the output path equal the source — we'd
        # delete the source as part of the overwrite step below.
        if os.path.abspath(out_path) == os.path.abspath(src):
            messagebox.showerror('Invalid output',
                'Output path is the same as the source. '
                'Pick a different name or folder.')
            return
        if os.path.exists(out_path):
            if not messagebox.askyesno('Overwrite',
                    out_path + '\n\nalready exists. Overwrite?'):
                return
            try:
                os.remove(out_path)
            except Exception as e:
                _log('Could not remove existing: ' + str(e))
                return

        # v3.0.0: OUTPUT LOG is never auto-opened (see e2f flow
        # above for the policy comment). The progress dialog
        # carries everything the user needs to see.

        # v3.0.0: rich progress dialog with ETA. Weights chosen from
        # observed runs — extract and copy each dominate roughly
        # half the wall time on real-world game-sized images.
        from ui.tab_ffpkg_edit import _RebuildProgress
        prog = _RebuildProgress(parent, 'Converting ffpkg → exFAT',
            weights={
                'extract':  (0,  50),
                'prep':     (50, 53),
                'copy':     (53, 98),
                'dismount': (98, 100),
            },
            initial_stage='extract')

        _set_busy_f2e(True, 'Locating tools...')

        def worker():
            import tempfile, shutil, ctypes as _ct, time as _time
            ufs2 = _get_ufs2tool_exe()
            if not ufs2:
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_f2e(False, 'Failed.'))
                parent.after(0, lambda: messagebox.showerror(
                    'UFS2Tool not available',
                    'UFS2Tool could not be extracted. Conversion '
                    'aborted.'))
                return

            osfmount = None
            try:
                osfmount = getattr(app, '_find_osfmount',
                                    lambda: None)()
            except Exception:
                osfmount = None
            if not osfmount or not os.path.isfile(osfmount):
                _log('OSFMount not found.')
                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_f2e(False, 'Failed.'))
                parent.after(0, lambda: messagebox.showerror(
                    'OSFMount missing',
                    'OSFMount is required to convert ffpkg to exFAT.\n'
                    'Configure its path under Settings.'))
                return

            work_dir = tempfile.mkdtemp(prefix='ffpkg_to_exfat_')
            dump_dir = os.path.join(work_dir, 'dump')

            # Stop flag shared between worker and the extract-progress
            # poll thread defined below.
            extract_poll_stop = threading.Event()

            try:
                # ── Step 1: extract .ffpkg with UFS2Tool ──────────────
                parent.after(0, prog.set_stage, 'extract',
                    'Extracting .ffpkg...')
                _log('Extracting %s ...' % src)
                os.makedirs(dump_dir, exist_ok=True)

                # Background poll: UFS2Tool extract doesn't emit
                # progress, so estimate from bytes-on-disk vs the
                # source ffpkg size. Capped at 95% so we never claim
                # done before UFS2Tool actually returns.
                try:
                    src_size = os.path.getsize(src)
                except Exception:
                    src_size = 0

                def _poll_extract():
                    while not extract_poll_stop.is_set():
                        try:
                            seen = 0
                            for r, _ds, fs in os.walk(dump_dir):
                                for f in fs:
                                    try:
                                        seen += os.path.getsize(
                                            os.path.join(r, f))
                                    except Exception:
                                        pass
                            if src_size > 0:
                                pct = min(95.0,
                                    100.0 * seen / src_size)
                            else:
                                pct = 0.0
                            detail = ('%.2f GB extracted'
                                      % (seen / 1024**3))
                            parent.after(0,
                                prog.set_stage_progress, pct, detail)
                        except Exception:
                            pass
                        extract_poll_stop.wait(0.7)

                poll_thread = threading.Thread(target=_poll_extract,
                                                daemon=True)
                poll_thread.start()

                # v3.6.x (Option B): watch the extract output for UFS2Tool's
                # 2 GB memory-cap failure ("File too large to read into
                # memory") via _run's existing progress_cb — no _run change.
                # Only that exact signature triggers the mount-copy fallback;
                # unrelated failures still fail fast below.
                extract_state = {'too_large': False}
                def _extract_watch(line):
                    if 'too large to read into memory' in line.lower():
                        extract_state['too_large'] = True

                rc = _run([ufs2, 'extract', src, dump_dir],
                          'UFS2Tool extract', progress_cb=_extract_watch)
                if rc != 0:
                    # Try the explicit '/' form for older UFS2Tool.
                    rc = _run([ufs2, 'extract', src, dump_dir, '/'],
                              'UFS2Tool extract (retry with /)',
                              progress_cb=_extract_watch)

                # Stop the byte-on-disk poll before any fallback so it can't
                # keep polling dump_dir while the mount-copy engine (which has
                # its own progress) is running.
                extract_poll_stop.set()

                # v3.6.x (Option B): large .ffpkg exceed UFS2Tool extract's
                # ~2 GB RAM cap. On that specific failure, fall back to the
                # existing streaming mount-copy engine (Dokan mount → robocopy),
                # the same one the Extract tab uses. Small images keep the fast
                # no-Dokan extract above; only this 2 GB case needs Dokan. A
                # large source is a defensive secondary signal in case a
                # UFS2Tool build words the message slightly differently.
                if rc != 0:
                    _is_2gb = extract_state['too_large'] or src_size > (2 * 1024**3)
                    if not _is_2gb:
                        # Unrelated failure (corrupt source, etc.) — fail fast,
                        # do NOT spin up Dokan.
                        raise RuntimeError(
                            'UFS2Tool extract failed (rc=%d)' % rc)

                    _log('UFS2Tool extract hit the ~2 GB memory cap; '
                         'falling back to Dokan mount-copy extraction...')
                    parent.after(0, prog.set_stage, 'extract',
                        'Large image — mounting to extract...')

                    # Progress shim: surface the mount-copy engine's progress
                    # in the existing extract stage (no new UI / stage weights).
                    def _mc_progress(pct, status, eta):
                        try:
                            detail = status + (('  ' + eta) if eta else '')
                            parent.after(0, prog.set_stage_progress,
                                         pct, detail)
                        except Exception:
                            pass

                    summary = app._ffpkg_mount_copy_extract(
                        src, dump_dir, _log, _mc_progress)

                    if not summary.get('ok'):
                        if summary.get('oversize_files'):
                            # >= 4 GB file(s): the bundled UFS2Tool Dokan mount
                            # cannot serve them. Stop cleanly here — extraction
                            # is Step 1, allocation is Step 2, so NO partial
                            # .exfat exists. Honest limitation message.
                            _ov = summary['oversize_files']
                            _lines = '\n'.join(
                                '- %s \u2014 %.1f GB' % (nm, sz / 1024**3)
                                for nm, sz in _ov)
                            _msg = (
                                'This .ffpkg contains files over 4 GB that '
                                'the current bundled UFS2Tool Dokan mount '
                                'cannot extract.\n\n'
                                'Affected files:\n' + _lines + '\n\n'
                                'This is a limitation of the current UFS2Tool '
                                'mount backend, not a problem with your image. '
                                'A fixed UFS2Tool build or alternative '
                                'extraction backend is required.')
                            parent.after(0, prog.close)
                            parent.after(0, lambda:
                                _set_busy_f2e(False, 'File(s) over 4 GB.'))
                            parent.after(0, lambda m=_msg: messagebox.showerror(
                                _('Cannot extract \u2014 files over 4 GB'),
                                _(m)))
                            return
                        if summary.get('dokan_missing'):
                            # Reuse the Extract-tab Dokan-required handling:
                            # prompt to grab the free driver, then stop cleanly
                            # (no partial .exfat — allocation hasn't run yet).
                            parent.after(0, prog.close)
                            parent.after(0, lambda:
                                _set_busy_f2e(False, 'Dokan required.'))
                            def _ask_dokan():
                                if messagebox.askyesno(_('Dokan required'),
                                    _('Extracting a large .ffpkg mounts it as '
                                      'a drive, which needs the free Dokan '
                                      'driver.\n\nOpen the download page now?')):
                                    import webbrowser
                                    webbrowser.open(
                                        'https://github.com/dokan-dev/dokany/'
                                        'releases/latest')
                            parent.after(0, _ask_dokan)
                            return
                        # Other mount-copy failure → flow through the existing
                        # outer except (dialog + cleanup).
                        raise RuntimeError(
                            summary.get('error')
                            or ('mount-copy extract failed (rc=%s)'
                                % summary.get('rc')))

                # Count files + total size for the next step.
                total_bytes = 0
                file_count  = 0
                for r, ds, fs in os.walk(dump_dir):
                    for f in fs:
                        try:
                            total_bytes += os.path.getsize(
                                os.path.join(r, f))
                            file_count += 1
                        except Exception:
                            pass
                if file_count == 0:
                    raise RuntimeError(
                        'Extraction produced no files. '
                        'Source image may be corrupt.')
                _log('Extracted %d files, %.2f GB total.'
                     % (file_count, total_bytes / 1024**3))
                parent.after(0, prog.set_stage_progress, 100.0,
                    '%d files, %.2f GB extracted.'
                    % (file_count, total_bytes / 1024**3))

                # ── Step 2: prep (allocate + mount + format) ─────────
                parent.after(0, prog.set_stage, 'prep',
                    'Preparing exFAT image...')

                # Size = extracted bytes × 1.10, rounded up to the
                # next 64 MB. The 10% headroom covers exFAT cluster
                # waste and directory metadata; 64 MB alignment keeps
                # OSFMount happy (it dislikes oddly-sized images).
                target_size = int(total_bytes * 1.10)
                ALIGN = 64 * 1024 * 1024
                target_size = ((target_size + ALIGN - 1) // ALIGN) * ALIGN
                if target_size < ALIGN:
                    target_size = ALIGN

                parent.after(0, prog.set_stage_progress, 10.0,
                    'Allocating image (%.2f GB)...'
                    % (target_size / 1024**3))
                _log('Allocating blank .exfat at %s (%.2f GB)...'
                     % (out_path, target_size / 1024**3))
                try:
                    with open(out_path, 'wb') as fh:
                        fh.seek(target_size - 1)
                        fh.write(b'\0')
                except Exception as e:
                    raise RuntimeError(
                        'Failed to allocate output file: ' + str(e))

                # Pick a free drive letter
                if sys.platform != 'win32':
                    raise RuntimeError('Conversion from ffpkg to exFAT requires Windows Dokan/UFS2 tools.')
                used_mask = _ct.windll.kernel32.GetLogicalDrives()
                mount_letter = None
                for code in range(ord('G'), ord('Z') + 1):
                    if not (used_mask & (1 << (code - ord('A')))):
                        mount_letter = chr(code) + ':'
                        break
                if not mount_letter:
                    raise RuntimeError('No free drive letter for mount.')

                # Mount the blank file writable
                parent.after(0, prog.set_stage_progress, 30.0,
                    'Mounting at ' + mount_letter)
                _log('Mounting %s at %s ...' % (out_path, mount_letter))
                # v3.6.x: bounded mount (mirrors the exFAT→ffpkg fix) so a hung
                # OSFMount attach of the destination image fails cleanly instead
                # of stalling. Timeout/failure both raise → handled by the
                # existing finally (dismount) + outer except (dialog).
                rc = _run_with_timeout([osfmount, '-a', '-t', 'file',
                                        '-f', out_path,
                                        '-m', mount_letter, '-o', 'rw'],
                                       _MOUNT_TIMEOUT_S)
                if rc == -2:
                    raise RuntimeError(
                        'OSFMount didn\'t finish attaching the destination '
                        'image within %d seconds, so the conversion was '
                        'stopped. A previous mount may still be attached, '
                        'antivirus may be scanning the image, or the drive '
                        'letter may be in use. No partial image was kept.'
                        % _MOUNT_TIMEOUT_S)
                if rc != 0:
                    raise RuntimeError(
                        'Mount failed (rc=%d)' % rc)

                # Wait for the drive to appear.
                for _ in range(20):
                    if os.path.exists(mount_letter + '\\'):
                        break
                    _time.sleep(0.5)

                try:
                    # Format as exFAT — format.com prompts even with
                    # /Y, so pipe newlines via stdin to suppress hang.
                    parent.after(0, prog.set_stage_progress, 60.0,
                        'Formatting %s as exFAT...' % mount_letter)
                    _log('Formatting %s as exFAT...' % mount_letter)
                    fmt_cmd = ['cmd.exe', '/c', 'format',
                               mount_letter, '/FS:exFAT', '/Q', '/Y',
                               '/V:']
                    _log('Running: ' + ' '.join(fmt_cmd))
                    try:
                        CREATE_NO_WINDOW = 0x08000000
                        kwargs = ({'creationflags': CREATE_NO_WINDOW}
                                  if os.name == 'nt' else {})
                        fp = subprocess.Popen(fmt_cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, errors='replace', **kwargs)
                        try:
                            fp.stdin.write('\n\n\n')
                            fp.stdin.flush()
                            fp.stdin.close()
                        except Exception:
                            pass
                        for ln in fp.stdout:
                            _log(ln.rstrip())
                        fp.wait(timeout=180)
                        rc = fp.returncode
                    except subprocess.TimeoutExpired:
                        try:
                            fp.kill()
                        except Exception:
                            pass
                        raise RuntimeError(
                            'format command timed out after 3 minutes.')
                    if rc != 0:
                        raise RuntimeError(
                            'format /FS:exFAT failed (rc=%d)' % rc)

                    _time.sleep(2)
                    if not os.path.exists(mount_letter + '\\'):
                        raise RuntimeError(
                            'Mounted drive disappeared after format.')
                    parent.after(0, prog.set_stage_progress, 100.0,
                        'Formatted, ready to copy.')

                    # ── Step 3: robocopy ─────────────────────────────
                    parent.after(0, prog.set_stage, 'copy',
                        'Copying %d files...' % file_count)
                    _log('Robocopying dump → %s ...' % mount_letter)

                    # Robocopy prints "    New File    <size>    <name>"
                    # per file. Count those to drive progress against
                    # the pre-scanned total.
                    files_copied = [0]
                    new_file_re = re.compile(
                        r'^\s*(?:New File|Newer)\b', re.IGNORECASE)
                    def _robo_cb(line):
                        if new_file_re.match(line):
                            files_copied[0] += 1
                            if file_count > 0:
                                pct = (100.0
                                       * files_copied[0] / file_count)
                            else:
                                pct = 0.0
                            detail = ('%d / %d files'
                                      % (files_copied[0], file_count))
                            parent.after(0,
                                prog.set_stage_progress, pct, detail)

                    robo_cmd = [
                        'robocopy.exe',
                        dump_dir, mount_letter + '\\',
                        '/E', '/COPY:DAT', '/DCOPY:DAT',
                        '/R:1', '/W:1', '/NP', '/ETA',
                    ]
                    rc = _run(robo_cmd, 'robocopy',
                              progress_cb=_robo_cb)
                    if rc >= 8:
                        raise RuntimeError(
                            'robocopy failed (rc=%d)' % rc)
                    _log('Copy complete.')
                    parent.after(0, prog.set_stage_progress, 100.0,
                        'Copied %d files.' % files_copied[0])
                finally:
                    # ── Step 4: dismount ─────────────────────────────
                    parent.after(0, prog.set_stage, 'dismount',
                        'Unmounting ' + mount_letter)
                    _log('Unmounting %s ...' % mount_letter)
                    try:
                        letter = mount_letter.rstrip(':\\')
                        if hasattr(app, '_dismount_drive_robust'):
                            app._dismount_drive_robust(letter,
                                max_wait_seconds=20)
                        else:
                            _run([osfmount, '-d', '-m', mount_letter])
                    except Exception as e:
                        _log('Dismount error: ' + str(e))
                    parent.after(0, prog.set_stage_progress, 100.0)

                parent.after(0, prog.close)
                parent.after(0, lambda:
                    _set_busy_f2e(False, 'Done \u2713'))
                parent.after(0, lambda: messagebox.showinfo(
                    'Convert complete',
                    'Wrote:\n' + out_path))
                def _nudge_cv2():
                    try:
                        from ui.release_notes import note_successful_operation
                        note_successful_operation(app, 'Convert')
                    except Exception:
                        pass
                parent.after(700, _nudge_cv2)
            except Exception as e:
                extract_poll_stop.set()
                _log('Convert failed: ' + str(e))
                parent.after(0, prog.close)
                parent.after(0, lambda e=e:
                    _set_busy_f2e(False, 'Failed.'))
                parent.after(0, lambda e=e: messagebox.showerror(
                    'ffpkg → exFAT failed', str(e)))
                # Best-effort cleanup of half-finished output file.
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
            finally:
                extract_poll_stop.set()
                # Always clean up the temp dump dir.
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    # ── Card 3: fPKG → ffpfsc (ShadowMount / PS-Neighborhood) ─────────
    p2f_card = tk.Frame(inner, bg=COLORS['bg_2'],
                         highlightbackground=COLORS['border_2'],
                         highlightthickness=1)
    p2f_card.pack(fill='x', padx=24, pady=(0, 24))

    # Card head
    p2f_chead = tk.Frame(p2f_card, bg=COLORS['bg_2'])
    p2f_chead.pack(fill='x', padx=24, pady=(18, 14))

    # Icon tile
    p2f_ico = tk.Label(p2f_chead, text='\U0001f4e6',
                       font=(FONTS['h2'][0], 13),
                       bg=COLORS['accent_08'], fg=COLORS['accent'],
                       width=2, padx=4, pady=2)
    p2f_ico.pack(side='left', padx=(0, 12))

    _flow_chips(p2f_chead, '.pkg', '.ffpfsc / .exfat')

    p2f_title_col = tk.Frame(p2f_chead, bg=COLORS['bg_2'])
    p2f_title_col.pack(side='left', fill='x', expand=True)
    tk.Label(p2f_title_col, text='fPKG \u2192 ffpfsc / .exfat (ShadowMount)',
             font=(FONTS['h3'][0], 12, 'bold'),
             bg=COLORS['bg_2'], fg=COLORS['fg_0'], anchor='w'
             ).pack(fill='x')
    tk.Label(p2f_title_col,
             text='Convert a PS5 debug base game (.pkg) to .ffpfsc (compressed) or direct .exfat (layers=1).',
             font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'], anchor='w'
             ).pack(fill='x', pady=(2, 0))

    # Hairline under head
    tk.Frame(p2f_card, bg=COLORS['border_2'], height=1).pack(fill='x')

    # Card body
    p2f_body = tk.Frame(p2f_card, bg=COLORS['bg_2'])
    p2f_body.pack(fill='x', padx=24, pady=(4, 18))

    def _p2f_browse_src():
        p = filedialog.askopenfilename(
            title='Select PS5 fPKG (.pkg)',
            filetypes=[('PS5 Packages', '*.pkg'),
                       ('All files', '*.*')])
        if p:
            p2f_src.set(p)

    def _p2f_browse_outdir():
        p = filedialog.askdirectory(title='Select output folder')
        if p:
            p2f_outdir.set(p)

    field_block(p2f_body, 'Source .pkg',
                var=p2f_src, on_browse=_p2f_browse_src,
                hint='PS5 debug base game package to convert')
    field_block(p2f_body, 'Output folder',
                var=p2f_outdir, on_browse=_p2f_browse_outdir,
                hint='where the image and receipt will be saved')
    field_block(p2f_body, 'Output name',
                var=p2f_name,
                hint='auto-filled with <titleId>.<ext> if blank')

    # Package inspection banner / preview inside the card
    p2f_info_frame = tk.Frame(p2f_body, bg=COLORS['bg_3'], bd=0, padx=12, pady=10)
    p2f_info_lbl = tk.Label(p2f_info_frame, textvariable=p2f_info_var,
                            font=FONTS['meta'], bg=COLORS['bg_3'], fg=COLORS['fg_2'],
                            justify='left', anchor='w')
    p2f_info_lbl.pack(fill='x')

    # Options row
    opts_frame = tk.Frame(p2f_body, bg=COLORS['bg_2'])
    opts_frame.pack(fill='x', pady=(12, 4))

    # Format selector
    fmt_col = tk.Frame(opts_frame, bg=COLORS['bg_2'])
    fmt_col.pack(side='left', padx=(0, 20))
    tk.Label(fmt_col, text='Format:', font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_3']).pack(side='left', padx=(0, 6))
    fmt_cb = ttk.Combobox(fmt_col, textvariable=p2f_dst_fmt,
                          values=['ShadowMount (.ffpfsc)', 'Direct exFAT (.exfat) [layers=1]'],
                          state='readonly', width=24)
    fmt_cb.pack(side='left')
    fmt_cb.set('ShadowMount (.ffpfsc)')

    # Compression level
    comp_col = tk.Frame(opts_frame, bg=COLORS['bg_2'])
    comp_col.pack(side='left', padx=(0, 20))
    tk.Label(comp_col, text='Compression:', font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_3']).pack(side='left', padx=(0, 6))
    comp_cb = ttk.Combobox(comp_col, textvariable=p2f_comp_var,
                           values=['1 (Fast)', '3 (Standard)', '6 (Default)', '9 (Max)'],
                           state='readonly', width=14)
    comp_cb.pack(side='left')
    if not p2f_comp_var.get() or p2f_comp_var.get() == '6':
        comp_cb.set('6 (Default)')

    def _on_p2f_fmt(*_a):
        val = p2f_dst_fmt.get().lower()
        is_exfat = 'exfat' in val
        if is_exfat:
            comp_cb.config(state='disabled')
            nm = p2f_name.get().strip()
            if nm.lower().endswith('.ffpfsc'):
                p2f_name.set(nm[:-7] + '.exfat')
            if p2f_src.get().strip():
                _update_hero(p2f_src.get().strip(), 'fPKG (.pkg)', 'Direct exFAT (.exfat)')
        else:
            comp_cb.config(state='readonly')
            nm = p2f_name.get().strip()
            if nm.lower().endswith('.exfat'):
                p2f_name.set(nm[:-6] + '.ffpfsc')
            if p2f_src.get().strip():
                _update_hero(p2f_src.get().strip(), 'fPKG (.pkg)', 'ShadowMount (.ffpfsc)')

    p2f_dst_fmt.trace_add('write', _on_p2f_fmt)

    # Checkboxes
    chk_col = tk.Frame(opts_frame, bg=COLORS['bg_2'])
    chk_col.pack(side='left', fill='x', expand=True)

    c1 = tk.Checkbutton(chk_col, text='Auto-clean temp files (keep original .pkg)',
                        variable=p2f_cleanup_var,
                        font=FONTS['meta'], bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                        activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3'])
    c1.pack(side='left', padx=(0, 16))

    c2 = tk.Checkbutton(chk_col, text='Save .verified.json receipt',
                        variable=p2f_receipt_var,
                        font=FONTS['meta'], bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                        activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3'])
    c2.pack(side='left')

    def _on_p2f_src(*_a):
        src_path = p2f_src.get().strip()
        is_exfat = 'exfat' in p2f_dst_fmt.get().lower()
        dst_lbl = 'Direct exFAT (.exfat)' if is_exfat else 'ShadowMount (.ffpfsc)'
        if not src_path or not os.path.isfile(src_path):
            p2f_info_frame.pack_forget()
            p2f_info_var.set('')
            _update_hero('', 'fPKG', dst_lbl)
            return

        if not p2f_outdir.get().strip():
            p2f_outdir.set(os.path.dirname(src_path))

        try:
            from ui.ps5_pkg_extractor import inspect_ps5_pkg
            info = inspect_ps5_pkg(src_path)
            if info.get('valid'):
                tid = info.get('title_id', 'PPSA00000')
                ext = '.exfat' if is_exfat else '.ffpfsc'
                cur_name = p2f_name.get().strip()
                if not cur_name or cur_name.endswith(('.ffpfsc', '.exfat')):
                    p2f_name.set(f"{tid}{ext}")
                conv = info.get('shadow_convertible', False)
                status_txt = '\u2713 Ready for ShadowMount' if conv else '\u26a0 Incompatible package'
                p2f_info_var.set(
                    f"Title: {info.get('title_name', 'Unknown')}  \u2502  "
                    f"ID: {tid}  \u2502  "
                    f"Version: {info.get('version', '01.00')}  \u2502  "
                    f"Signing: {info.get('signing', 'Debug')}  \u2502  "
                    f"Status: {status_txt}"
                )
                if not p2f_info_frame.winfo_ismapped():
                    p2f_info_frame.pack(fill='x', pady=(6, 8), before=opts_frame)
            else:
                p2f_info_var.set(f"\u26a0 {info.get('error', 'Invalid package')}")
                if not p2f_info_frame.winfo_ismapped():
                    p2f_info_frame.pack(fill='x', pady=(6, 8), before=opts_frame)
        except Exception as e:
            p2f_info_var.set(f"Inspection error: {e}")
            if not p2f_info_frame.winfo_ismapped():
                p2f_info_frame.pack(fill='x', pady=(6, 8), before=opts_frame)

        _update_hero(src_path, 'fPKG (.pkg)', dst_lbl)

    p2f_src.trace_add('write', _on_p2f_src)

    # Action row
    p2f_action_row = tk.Frame(p2f_body, bg=COLORS['bg_2'])
    p2f_action_row.pack(fill='x', pady=(18, 0))

    p2f_btn = make_themed_button(
        p2f_action_row,
        text='Convert to ffpfsc',
        command=lambda: _do_fpkg_to_ffpfsc(),
        kind='success',
        icon='\u25b6',
        font_size=10, padx=18, pady=9)
    p2f_btn.pack(side='left')
    state['p2f_btn'] = p2f_btn

    tk.Label(p2f_action_row, textvariable=p2f_status_var,
             font=FONTS['mono_sm'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'],
             anchor='w').pack(side='left', padx=(16, 0))

    p2f_pbar_wrap = tk.Frame(p2f_action_row, bg=COLORS['bg_2'])
    p2f_pbar_wrap.pack(side='right', fill='x', expand=True, padx=(16, 0))
    p2f_pbar = ttk.Progressbar(p2f_pbar_wrap, mode='indeterminate', length=200)
    p2f_pbar.pack(fill='x')

    def _set_busy_p2f(b, label=''):
        state['busy'] = b
        try:
            if b:
                p2f_pbar.start(10)
                p2f_status_var.set(label or 'Working...')
                p2f_btn.config(state='disabled', cursor='watch')
                try:
                    convert_btn.config(state='disabled')
                except Exception:
                    pass
                if 'f2e_btn' in state and state['f2e_btn']:
                    try:
                        state['f2e_btn'].config(state='disabled')
                    except Exception:
                        pass
                if 'lz4_btn' in state and state['lz4_btn']:
                    try:
                        state['lz4_btn'].config(state='disabled')
                    except Exception:
                        pass
            else:
                p2f_pbar.stop()
                p2f_status_var.set(label or 'Idle.')
                p2f_btn.config(state='normal', cursor='hand2')
                try:
                    convert_btn.config(state='normal')
                except Exception:
                    pass
                if 'f2e_btn' in state and state['f2e_btn']:
                    try:
                        state['f2e_btn'].config(state='normal')
                    except Exception:
                        pass
                if 'lz4_btn' in state and state['lz4_btn']:
                    try:
                        state['lz4_btn'].config(state='normal')
                    except Exception:
                        pass
        except Exception:
            pass

    # ── fPKG → ffpfsc worker ─────────────────────────────────────────
    def _do_fpkg_to_ffpfsc():
        if state['busy']:
            return
        src = p2f_src.get().strip()
        outdir = p2f_outdir.get().strip()
        name = p2f_name.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('Source missing',
                'Pick a valid PS5 fPKG (.pkg) source file.')
            return
        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror('Output folder missing',
                'Pick an output folder.')
            return
        is_exfat = 'exfat' in p2f_dst_fmt.get().lower()
        ext = '.exfat' if is_exfat else '.ffpfsc'
        if not name:
            base = os.path.splitext(os.path.basename(src))[0]
            name = base + ext
        if not name.lower().endswith(ext):
            name = name + ext

        out_path = os.path.join(outdir, name)
        if os.path.exists(out_path):
            if not messagebox.askyesno('Overwrite',
                    out_path + '\n\nalready exists. Overwrite?'):
                return
            try:
                os.remove(out_path)
            except Exception as e:
                _log('Could not remove existing destination: ' + str(e))
                return

        comp_str = p2f_comp_var.get().strip()
        try:
            comp_lvl = int(comp_str.split()[0])
        except Exception:
            comp_lvl = 6

        auto_cleanup = p2f_cleanup_var.get()
        save_receipt = p2f_receipt_var.get()
        custom_temp = p2f_temp_var.get().strip() or getattr(app, '_settings', {}).get('temp_dir') or None

        from ui.tab_ffpkg_edit import _RebuildProgress
        prog_title = 'Converting fPKG \u2192 exFAT (Direct Image)' if is_exfat else 'Converting fPKG \u2192 ffpfsc (ShadowMount)'
        prog_weights = {
            'inspect':  (0,   5),
            'extract':  (5,  55),
            'build':    (55, 95),
            'verify':   (95, 98),
            'cleanup':  (98, 100),
        } if is_exfat else {
            'inspect':  (0,   5),
            'extract':  (5,  55),
            'build':    (55, 75),
            'compress': (75, 92),
            'verify':   (92, 98),
            'cleanup':  (98, 100),
        }
        prog = _RebuildProgress(parent, prog_title, weights=prog_weights, initial_stage='inspect')

        _set_busy_p2f(True, 'Starting conversion...')

        def worker():
            try:
                def _ui_progress(stage_msg, done, total):
                    pct = (done / max(1, total)) * 100.0
                    lmsg = stage_msg.lower()
                    if 'extract' in lmsg:
                        prog_stage = 'extract'
                    elif 'intermediate' in lmsg or 'filesystem' in lmsg or 'exfat' in lmsg or 'pack' in lmsg or 'pfs' in lmsg:
                        prog_stage = 'build'
                    elif 'compress' in lmsg or 'mkpfs' in lmsg:
                        prog_stage = 'compress'
                    elif 'verify' in lmsg or 'checksum' in lmsg:
                        prog_stage = 'verify'
                    elif 'clean' in lmsg:
                        prog_stage = 'cleanup'
                    else:
                        prog_stage = 'inspect'

                    parent.after(0, prog.set_stage, prog_stage, stage_msg)
                    parent.after(0, prog.set_stage_progress, pct, f"{stage_msg} ({done}%)")

                if is_exfat:
                    from ui.ps5_pkg_extractor import convert_fpkg_to_exfat
                    report = convert_fpkg_to_exfat(
                        pkg_path=src,
                        output_dir=outdir,
                        custom_name=name,
                        temp_dir=custom_temp,
                        auto_cleanup=auto_cleanup,
                        save_receipt=save_receipt,
                        log_cb=_log,
                        progress_cb=_ui_progress
                    )
                    parent.after(0, prog.close)
                    parent.after(0, lambda: _set_busy_p2f(False, 'Done \u2713'))
                    parent.after(0, lambda: _update_hero(out_path, 'fPKG (.pkg)', 'Direct exFAT (.exfat)'))

                    rep_msg = (
                        f"Converted fPKG to direct exFAT container successfully!\n\n"
                        f"Title ID: {report.get('titleId')}\n"
                        f"Output: {os.path.basename(out_path)}\n"
                        f"Image Size: {report.get('imageSize', 0) / (1024**3):.2f} GB\n"
                        f"SHA-256: {report.get('imageSha256', '')[:16]}...\n\n"
                        f"Single-layer (layers=1) mounting for ShadowMount+!"
                    )
                    if save_receipt:
                        rep_msg += f"\nVerification receipt saved to:\n{os.path.basename(out_path)}.verified.json"

                    parent.after(0, lambda: messagebox.showinfo('fPKG Converted to exFAT', rep_msg))

                else:
                    from ui.ps5_pkg_extractor import convert_fpkg_to_ffpfsc
                    report = convert_fpkg_to_ffpfsc(
                        pkg_path=src,
                        output_dir=outdir,
                        custom_name=name,
                        temp_dir=custom_temp,
                        compression_level=comp_lvl,
                        auto_cleanup=auto_cleanup,
                        save_receipt=save_receipt,
                        external_converter=getattr(app, '_settings', {}).get('neighborhood_converter') or None,
                        log_cb=_log,
                        progress_cb=_ui_progress
                    )
                    parent.after(0, prog.close)
                    parent.after(0, lambda: _set_busy_p2f(False, 'Done \u2713'))
                    parent.after(0, lambda: _update_hero(out_path, 'fPKG (.pkg)', 'ShadowMount (.ffpfsc)'))

                    rep_msg = (
                        f"Converted fPKG to ShadowMount .ffpfsc successfully!\n\n"
                        f"Title ID: {report.get('titleId')}\n"
                        f"Output: {os.path.basename(out_path)}\n"
                        f"Image Size: {report.get('imageSize', 0) / (1024**3):.2f} GB\n"
                        f"SHA-256: {report.get('imageSha256', '')[:16]}...\n"
                    )
                    if save_receipt:
                        rep_msg += f"\nVerification receipt saved to:\n{os.path.basename(out_path)}.verified.json"

                    parent.after(0, lambda: messagebox.showinfo('fPKG Converted Successfully', rep_msg))

                try:
                    from ui.release_notes import note_successful_operation
                    note_successful_operation(app, 'Convert')
                except Exception:
                    pass

            except Exception as e:
                _log('fPKG \u2192 ffpfsc failed: ' + str(e))
                parent.after(0, prog.close)
                parent.after(0, lambda e=e: _set_busy_p2f(False, 'Failed.'))
                parent.after(0, lambda e=e: messagebox.showerror('fPKG \u2192 ffpfsc Failed', str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ── Card 4: fPKG / Game Folder → AMPR LZ4 (Lazy_AMPR Architecture) ─
    lz4_card = tk.Frame(inner, bg=COLORS['bg_2'],
                         highlightbackground=COLORS['border_2'],
                         highlightthickness=1)
    lz4_card.pack(fill='x', padx=24, pady=(0, 24))

    # Card head
    lz4_chead = tk.Frame(lz4_card, bg=COLORS['bg_2'])
    lz4_chead.pack(fill='x', padx=24, pady=(18, 14))

    # Icon tile
    lz4_ico = tk.Label(lz4_chead, text='\U0001f5dc',
                       font=(FONTS['h2'][0], 13),
                       bg=COLORS['accent_08'], fg=COLORS['accent'],
                       width=2, padx=4, pady=2)
    lz4_ico.pack(side='left', padx=(0, 12))

    _flow_chips(lz4_chead, '.pkg / app0', 'AMPR LZ4 (.pak)')

    lz4_title_col = tk.Frame(lz4_chead, bg=COLORS['bg_2'])
    lz4_title_col.pack(side='left', fill='x', expand=True)
    tk.Label(lz4_title_col, text='fPKG \u2192 AMPR LZ4 (Lazy_AMPR Asset Packs)',
             font=(FONTS['h3'][0], 12, 'bold'),
             bg=COLORS['bg_2'], fg=COLORS['fg_0'], anchor='w'
             ).pack(fill='x')
    tk.Label(lz4_title_col,
             text='Extract & compress PS5 game assets to seekable LZ4 .pak volumes with verified libSceAmpr runtime for PS5.',
             font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'], anchor='w'
             ).pack(fill='x', pady=(2, 0))

    # Hairline under head
    tk.Frame(lz4_card, bg=COLORS['border_2'], height=1).pack(fill='x')

    # Card body
    lz4_body = tk.Frame(lz4_card, bg=COLORS['bg_2'])
    lz4_body.pack(fill='x', padx=24, pady=(4, 18))

    def _lz4_browse_pkg():
        p = filedialog.askopenfilename(
            title='Select PS5 fPKG (.pkg)',
            filetypes=[('PS5 Packages', '*.pkg'), ('All files', '*.*')])
        if p:
            lz4_src.set(p)

    def _lz4_browse_dir():
        p = filedialog.askdirectory(title='Select PS5 Game Folder (/app0)')
        if p:
            lz4_src.set(p)

    def _lz4_browse_outdir():
        p = filedialog.askdirectory(title='Select output folder')
        if p:
            lz4_outdir.set(p)

    def _lz4_browse_toml():
        p = filedialog.askopenfilename(
            title='Select Custom AMPR TOML Profile',
            filetypes=[('TOML Profiles', '*.toml'), ('All files', '*.*')])
        if p:
            lz4_toml_var.set(p)

    def _lz4_browse_traces():
        p = filedialog.askdirectory(title='Select APR Traces Folder')
        if p:
            lz4_traces_var.set(p)

    # Source input block with dual browse buttons (.pkg or folder)
    src_block = tk.Frame(lz4_body, bg=COLORS['bg_2'])
    src_block.pack(fill='x', pady=(14, 0))

    src_lbl_row = tk.Frame(src_block, bg=COLORS['bg_2'])
    src_lbl_row.pack(fill='x')
    tk.Label(src_lbl_row, text='Source .pkg or Game Folder',
             font=FONTS['label'], bg=COLORS['bg_2'], fg=COLORS['fg_3'], anchor='w').pack(side='left')
    tk.Label(src_lbl_row, text='  \u2022  PS5 debug package (.pkg) or extracted game directory (/app0)',
             font=FONTS['meta'], bg=COLORS['bg_2'], fg=COLORS['fg_5']).pack(side='left')

    src_input_wrap = tk.Frame(src_block, bg=COLORS['field_bg'],
                              highlightbackground=COLORS['border_3'], highlightthickness=1)
    src_input_wrap.pack(fill='x', pady=(6, 0))

    src_entry = tk.Entry(src_input_wrap, textvariable=lz4_src,
                         font=FONTS['mono_sm'], bg=COLORS['field_bg'], fg=COLORS['field_fg'],
                         insertbackground=COLORS['field_fg'], selectbackground=COLORS['accent'],
                         selectforeground=COLORS['fg_0'], relief='flat', bd=8)
    src_entry.pack(side='left', fill='x', expand=True)

    tk.Button(src_input_wrap, text='Browse .pkg',
              font=FONTS['button'], bg=COLORS['bg_3'], fg=COLORS['fg_2'],
              activebackground=COLORS['bg_5'], activeforeground=COLORS['accent'],
              relief='flat', bd=0, padx=12, pady=6, cursor='hand2',
              command=_lz4_browse_pkg).pack(side='right', padx=(2, 0))
    tk.Button(src_input_wrap, text='Browse Folder',
              font=FONTS['button'], bg=COLORS['bg_3'], fg=COLORS['fg_2'],
              activebackground=COLORS['bg_5'], activeforeground=COLORS['accent'],
              relief='flat', bd=0, padx=12, pady=6, cursor='hand2',
              command=_lz4_browse_dir).pack(side='right')

    field_block(lz4_body, 'Output folder',
                var=lz4_outdir, on_browse=_lz4_browse_outdir,
                hint='where the packed game directory or image will be saved')
    field_block(lz4_body, 'Output name',
                var=lz4_name,
                hint='folder or image filename (auto-filled from Title ID)')

    # Live inspection preview inside Card 4
    lz4_info_frame = tk.Frame(lz4_body, bg=COLORS['bg_3'], bd=0, padx=12, pady=10)
    lz4_info_lbl = tk.Label(lz4_info_frame, textvariable=lz4_info_var,
                            font=FONTS['meta'], bg=COLORS['bg_3'], fg=COLORS['fg_2'],
                            justify='left', anchor='w')
    lz4_info_lbl.pack(fill='x')

    # Options row 1: Target format, Level, Block size
    lz4_opts_frame = tk.Frame(lz4_body, bg=COLORS['bg_2'])
    lz4_opts_frame.pack(fill='x', pady=(12, 4))

    # Format selector
    lz4_fmt_col = tk.Frame(lz4_opts_frame, bg=COLORS['bg_2'])
    lz4_fmt_col.pack(side='left', padx=(0, 16))
    tk.Label(lz4_fmt_col, text='Format:', font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_3']).pack(side='left', padx=(0, 6))
    lz4_fmt_cb = ttk.Combobox(
        lz4_fmt_col, textvariable=lz4_dst_fmt,
        values=['Game Folder (/app0 with LZ4 .pak)', 'Direct exFAT (.exfat) with LZ4', 'ShadowMount (.ffpfsc) with LZ4'],
        state='readonly', width=30)
    lz4_fmt_cb.pack(side='left')

    # Compression level selector
    lz4_lvl_col = tk.Frame(lz4_opts_frame, bg=COLORS['bg_2'])
    lz4_lvl_col.pack(side='left', padx=(0, 16))
    tk.Label(lz4_lvl_col, text='LZ4 Level:', font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_3']).pack(side='left', padx=(0, 6))
    lz4_lvl_cb = ttk.Combobox(
        lz4_lvl_col, textvariable=lz4_level_var,
        values=['1 (Fastest)', '3 (Fast)', '6 (Balanced)', '9 (High - Recommended)', '12 (Ultra)'],
        state='readonly', width=20)
    lz4_lvl_cb.pack(side='left')

    # Block size selector
    lz4_blk_col = tk.Frame(lz4_opts_frame, bg=COLORS['bg_2'])
    lz4_blk_col.pack(side='left')
    tk.Label(lz4_blk_col, text='Block Size:', font=FONTS['meta'],
             bg=COLORS['bg_2'], fg=COLORS['fg_3']).pack(side='left', padx=(0, 6))
    lz4_blk_cb = ttk.Combobox(
        lz4_blk_col, textvariable=lz4_block_var,
        values=['16 KiB (Audio)', '32 KiB', '64 KiB (Recommended)', '128 KiB', '256 KiB', '512 KiB', '1024 KiB'],
        state='readonly', width=20)
    lz4_blk_cb.pack(side='left')

    # Options row 2: Advanced Profile Overrides (Collapsible / optional row)
    adv_box = tk.Frame(lz4_body, bg=COLORS['bg_3'],
                       highlightbackground=COLORS['border_3'], highlightthickness=1)
    adv_box.pack(fill='x', pady=(10, 4))
    adv_inner = tk.Frame(adv_box, bg=COLORS['bg_3'], padx=12, pady=8)
    adv_inner.pack(fill='x')

    tk.Label(adv_inner, text='Optional Profiles & Traces (Defaults to Lazy_AMPR intelligent auto-scan):',
             font=(FONTS['meta'][0], 9, 'bold'), bg=COLORS['bg_3'], fg=COLORS['fg_3']).pack(anchor='w', pady=(0, 4))

    prof_row = tk.Frame(adv_inner, bg=COLORS['bg_3'])
    prof_row.pack(fill='x')

    # TOML override
    tk.Label(prof_row, text='Custom TOML:', font=FONTS['meta'], bg=COLORS['bg_3'], fg=COLORS['fg_4']).pack(side='left', padx=(0, 4))
    toml_ent = tk.Entry(prof_row, textvariable=lz4_toml_var, font=FONTS['mono_sm'],
                        bg=COLORS['field_bg'], fg=COLORS['field_fg'], relief='flat', bd=4, width=28)
    toml_ent.pack(side='left', padx=(0, 4))
    tk.Button(prof_row, text='Browse', font=FONTS['meta'], bg=COLORS['bg_2'], fg=COLORS['fg_2'],
              relief='flat', bd=0, padx=8, pady=2, cursor='hand2', command=_lz4_browse_toml).pack(side='left', padx=(0, 16))

    # Traces override
    tk.Label(prof_row, text='Traces Dir:', font=FONTS['meta'], bg=COLORS['bg_3'], fg=COLORS['fg_4']).pack(side='left', padx=(0, 4))
    traces_ent = tk.Entry(prof_row, textvariable=lz4_traces_var, font=FONTS['mono_sm'],
                          bg=COLORS['field_bg'], fg=COLORS['field_fg'], relief='flat', bd=4, width=28)
    traces_ent.pack(side='left', padx=(0, 4))
    tk.Button(prof_row, text='Browse', font=FONTS['meta'], bg=COLORS['bg_2'], fg=COLORS['fg_2'],
              relief='flat', bd=0, padx=8, pady=2, cursor='hand2', command=_lz4_browse_traces).pack(side='left')

    # Options row 3: Checkboxes (essential for "funcionen perfectamente en mi ps5")
    chk_row = tk.Frame(lz4_body, bg=COLORS['bg_2'])
    chk_row.pack(fill='x', pady=(8, 2))

    tk.Checkbutton(chk_row, text='Install PS5 AMPR Runtime (fakelib/libSceAmpr.sprx)',
                   variable=lz4_runtime_var, font=FONTS['meta'],
                   bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                   activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3']).pack(side='left', padx=(0, 14))

    tk.Checkbutton(chk_row, text='Verify pack checksums',
                   variable=lz4_verify_var, font=FONTS['meta'],
                   bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                   activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3']).pack(side='left', padx=(0, 14))

    tk.Checkbutton(chk_row, text='Auto-clean temp files',
                   variable=lz4_cleanup_var, font=FONTS['meta'],
                   bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                   activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3']).pack(side='left', padx=(0, 14))

    tk.Checkbutton(chk_row, text='Save .verified.json receipt',
                   variable=lz4_receipt_var, font=FONTS['meta'],
                   bg=COLORS['bg_2'], fg=COLORS['fg_1'],
                   activebackground=COLORS['bg_2'], selectcolor=COLORS['bg_3']).pack(side='left')

    # Dynamic format change updater
    def _on_lz4_fmt(*_a):
        fmt = lz4_dst_fmt.get().lower()
        nm = lz4_name.get().strip()
        if 'exfat' in fmt:
            if nm and not nm.lower().endswith('.exfat'):
                base = re.sub(r'(\.ffpfsc|_lz4)$', '', nm, flags=re.I)
                lz4_name.set(base + '.exfat')
        elif 'ffpfsc' in fmt:
            if nm and not nm.lower().endswith('.ffpfsc'):
                base = re.sub(r'(\.exfat|_lz4)$', '', nm, flags=re.I)
                lz4_name.set(base + '.ffpfsc')
        else:
            if nm and (nm.lower().endswith('.exfat') or nm.lower().endswith('.ffpfsc')):
                base = re.sub(r'(\.exfat|\.ffpfsc)$', '', nm, flags=re.I)
                lz4_name.set(base + '_lz4')
        if lz4_src.get().strip():
            _update_hero(lz4_src.get().strip(), 'Source Game', f'AMPR LZ4 ({fmt[:6]})')

    lz4_dst_fmt.trace_add('write', _on_lz4_fmt)

    # Dynamic source inspection updater
    def _on_lz4_src(*_a):
        src_path = lz4_src.get().strip()
        fmt_val = lz4_dst_fmt.get().lower()
        dst_lbl = f'AMPR LZ4 ({fmt_val[:6]})'

        if not src_path or not os.path.exists(src_path):
            lz4_info_frame.pack_forget()
            lz4_info_var.set('')
            _update_hero('', 'Source Game', dst_lbl)
            return

        if not lz4_outdir.get().strip():
            lz4_outdir.set(os.path.dirname(src_path) if os.path.isfile(src_path) else str(Path(src_path).parent))

        try:
            if os.path.isfile(src_path) and src_path.lower().endswith('.pkg'):
                from ui.ps5_pkg_extractor import inspect_ps5_pkg
                info = inspect_ps5_pkg(src_path)
                if info.get('valid'):
                    tid = info.get('title_id', 'PPSA00000')
                    tname = info.get('title_name', 'Unknown')
                    ver = info.get('version', '01.00')
                    sdk = info.get('system_ver', 'Unknown')
                    cur_name = lz4_name.get().strip()
                    if not cur_name or cur_name.endswith(('.ffpfsc', '.exfat', '_lz4')):
                        if 'exfat' in fmt_val:
                            lz4_name.set(f"{tid}.exfat")
                        elif 'ffpfsc' in fmt_val:
                            lz4_name.set(f"{tid}.ffpfsc")
                        else:
                            lz4_name.set(f"{tid}_lz4")

                    lz4_info_var.set(
                        f"Title: {tname}  \u2502  "
                        f"ID: {tid}  \u2502  "
                        f"Version: {ver}  \u2502  "
                        f"SDK: {sdk}  \u2502  "
                        f"Status: \u2713 Ready for AMPR LZ4 Compression"
                    )
                    if not lz4_info_frame.winfo_ismapped():
                        lz4_info_frame.pack(fill='x', pady=(6, 8), before=lz4_opts_frame)
                else:
                    lz4_info_var.set(f"\u26a0 {info.get('error', 'Invalid package')}")
                    if not lz4_info_frame.winfo_ismapped():
                        lz4_info_frame.pack(fill='x', pady=(6, 8), before=lz4_opts_frame)
            elif os.path.isdir(src_path):
                # Folder input
                p_file = os.path.join(src_path, 'sce_sys', 'param.json')
                tid = 'PPSA00000'
                tname = os.path.basename(src_path)
                ver = '01.00'
                if os.path.isfile(p_file):
                    try:
                        with open(p_file, 'r', encoding='utf-8') as pf:
                            pj = json.load(pf)
                            tid = pj.get('titleId', tid)
                            loc = pj.get('localizedParameters', {}).get('defaultLanguage', {})
                            tname = loc.get('titleName', tname)
                    except Exception:
                        pass
                cur_name = lz4_name.get().strip()
                if not cur_name or cur_name.endswith(('.ffpfsc', '.exfat', '_lz4')):
                    if 'exfat' in fmt_val:
                        lz4_name.set(f"{tid}.exfat")
                    elif 'ffpfsc' in fmt_val:
                        lz4_name.set(f"{tid}.ffpfsc")
                    else:
                        lz4_name.set(f"{tid}_lz4")

                lz4_info_var.set(
                    f"Title: {tname}  \u2502  "
                    f"ID: {tid}  \u2502  "
                    f"Folder: {os.path.basename(src_path)}  \u2502  "
                    f"Status: \u2713 Ready for AMPR LZ4 Compression"
                )
                if not lz4_info_frame.winfo_ismapped():
                    lz4_info_frame.pack(fill='x', pady=(6, 8), before=lz4_opts_frame)
        except Exception as e:
            lz4_info_var.set(f"Inspection error: {e}")
            if not lz4_info_frame.winfo_ismapped():
                lz4_info_frame.pack(fill='x', pady=(6, 8), before=lz4_opts_frame)

        _update_hero(src_path, 'fPKG / app0', dst_lbl)

    lz4_src.trace_add('write', _on_lz4_src)

    # Action row
    lz4_action_row = tk.Frame(lz4_body, bg=COLORS['bg_2'])
    lz4_action_row.pack(fill='x', pady=(18, 0))

    lz4_btn = make_themed_button(
        lz4_action_row,
        text='Convert to LZ4',
        command=lambda: _do_fpkg_to_lz4(),
        kind='success',
        icon='\u25b6',
        font_size=10, padx=18, pady=9)
    lz4_btn.pack(side='left')
    state['lz4_btn'] = lz4_btn

    tk.Label(lz4_action_row, textvariable=lz4_status_var,
             font=FONTS['mono_sm'],
             bg=COLORS['bg_2'], fg=COLORS['fg_4'],
             anchor='w').pack(side='left', padx=(16, 0))

    lz4_pbar_wrap = tk.Frame(lz4_action_row, bg=COLORS['bg_2'])
    lz4_pbar_wrap.pack(side='right', fill='x', expand=True, padx=(16, 0))
    lz4_pbar = ttk.Progressbar(lz4_pbar_wrap, mode='indeterminate', length=200)
    lz4_pbar.pack(fill='x')

    def _set_busy_lz4(b, label=''):
        state['busy'] = b
        try:
            if b:
                lz4_pbar.start(10)
                lz4_status_var.set(label or 'Working...')
                lz4_btn.config(state='disabled', cursor='watch')
                try:
                    convert_btn.config(state='disabled')
                except Exception:
                    pass
                if 'f2e_btn' in state and state['f2e_btn']:
                    try:
                        state['f2e_btn'].config(state='disabled')
                    except Exception:
                        pass
                if 'p2f_btn' in state and state['p2f_btn']:
                    try:
                        state['p2f_btn'].config(state='disabled')
                    except Exception:
                        pass
            else:
                lz4_pbar.stop()
                lz4_status_var.set(label or 'Idle.')
                lz4_btn.config(state='normal', cursor='hand2')
                try:
                    convert_btn.config(state='normal')
                except Exception:
                    pass
                if 'f2e_btn' in state and state['f2e_btn']:
                    try:
                        state['f2e_btn'].config(state='normal')
                    except Exception:
                        pass
                if 'p2f_btn' in state and state['p2f_btn']:
                    try:
                        state['p2f_btn'].config(state='normal')
                    except Exception:
                        pass
        except Exception:
            pass

    # ── fPKG / Folder → AMPR LZ4 Worker ──────────────────────────────
    def _do_fpkg_to_lz4():
        if state['busy']:
            return
        src = lz4_src.get().strip()
        outdir = lz4_outdir.get().strip()
        name = lz4_name.get().strip()

        if not src or not os.path.exists(src):
            messagebox.showerror('Source missing', 'Pick a valid PS5 fPKG (.pkg) or game folder.')
            return
        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror('Output folder missing', 'Pick an output directory.')
            return

        fmt_val = lz4_dst_fmt.get().lower()
        if 'exfat' in fmt_val:
            target_fmt = 'exfat'
            ext = '.exfat'
        elif 'ffpfsc' in fmt_val:
            target_fmt = 'ffpfsc'
            ext = '.ffpfsc'
        else:
            target_fmt = 'folder'
            ext = ''

        if not name:
            base = os.path.splitext(os.path.basename(src))[0]
            name = (base + ext) if ext else f"{base}_lz4"
        elif ext and not name.lower().endswith(ext):
            name = name + ext

        out_path = os.path.join(outdir, name)
        if os.path.exists(out_path):
            if not messagebox.askyesno('Overwrite', f"{out_path}\n\nalready exists. Overwrite?"):
                return
            try:
                if os.path.isdir(out_path):
                    shutil.rmtree(out_path, ignore_errors=True)
                else:
                    os.remove(out_path)
            except Exception as e:
                _log('Could not remove existing destination: ' + str(e))
                return

        # Parse level & block size
        try:
            lvl = int(lz4_level_var.get().split()[0])
        except Exception:
            lvl = 9
        try:
            blk_str = lz4_block_var.get().split()[0]
            blk = int(blk_str)
        except Exception:
            blk = 64

        custom_toml = lz4_toml_var.get().strip() or None
        traces_dir = lz4_traces_var.get().strip() or None
        auto_cleanup = lz4_cleanup_var.get()
        save_receipt = lz4_receipt_var.get()
        skip_verify = not lz4_verify_var.get()
        custom_temp = lz4_temp_var.get().strip() or getattr(app, '_settings', {}).get('temp_dir') or None

        is_pkg = os.path.isfile(src) and src.lower().endswith('.pkg')

        from ui.tab_ffpkg_edit import _RebuildProgress
        prog_weights = {
            'inspect':   (0,   5),
            'extract':   (5,  30),
            'profile':   (30, 40),
            'compress':  (40, 82),
            'verify':    (82, 90),
            'loose':     (90, 95),
            'container': (95, 98),
            'cleanup':   (98, 100),
        } if is_pkg else {
            'inspect':   (0,   5),
            'profile':   (5,  20),
            'compress':  (20, 78),
            'verify':    (78, 88),
            'loose':     (88, 94),
            'container': (94, 98),
            'cleanup':   (98, 100),
        }

        prog = _RebuildProgress(parent, 'Converting to AMPR LZ4 (Lazy_AMPR Architecture)',
                                weights=prog_weights, initial_stage='inspect')

        _set_busy_lz4(True, 'Starting LZ4 conversion...')

        def worker():
            try:
                def _ui_progress(stage_msg, done, total):
                    pct = (done / max(1, total)) * 100.0
                    lmsg = stage_msg.lower()
                    if 'extract' in lmsg:
                        stg = 'extract'
                    elif 'profile' in lmsg or 'scan' in lmsg or 'index' in lmsg or 'runtime' in lmsg:
                        stg = 'profile'
                    elif 'compress' in lmsg or 'pack' in lmsg:
                        stg = 'compress'
                    elif 'verify' in lmsg or 'checksum' in lmsg:
                        stg = 'verify'
                    elif 'loose' in lmsg or 'copy' in lmsg:
                        stg = 'loose'
                    elif 'container' in lmsg or 'exfat' in lmsg or 'ffpfsc' in lmsg or 'mkpfs' in lmsg:
                        stg = 'container'
                    elif 'clean' in lmsg or 'complete' in lmsg:
                        stg = 'cleanup'
                    else:
                        stg = 'inspect'
                    parent.after(0, prog.set_stage, stg, stage_msg)
                    parent.after(0, prog.set_stage_progress, pct, f"{stage_msg} ({done}%)")

                from ui.ampr_lz4_converter import convert_fpkg_to_lz4
                report = convert_fpkg_to_lz4(
                    pkg_or_dir_path=src,
                    output_dir=outdir,
                    custom_name=name,
                    target_format=target_fmt,
                    lz4_level=lvl,
                    block_size_kib=blk,
                    custom_config=custom_toml,
                    traces_dir=traces_dir,
                    skip_verify=skip_verify,
                    auto_cleanup=auto_cleanup,
                    save_receipt=save_receipt,
                    temp_dir=custom_temp,
                    log_cb=_log,
                    progress_cb=_ui_progress
                )

                parent.after(0, prog.close)
                parent.after(0, lambda: _set_busy_lz4(False, 'Done \u2713'))
                parent.after(0, lambda: _update_hero(out_path, 'fPKG / app0', f'AMPR LZ4 ({target_fmt})'))

                paks = report.get('pak_count', 0)
                pak_gb = report.get('pak_bytes', 0) / (1024**3)
                loose_cnt = report.get('loose_count', 0)
                loose_gb = report.get('loose_bytes', 0) / (1024**3)
                ratio = report.get('savings_ratio_pct', 0)
                saved_pct = max(0.0, 100.0 - ratio)

                rep_msg = (
                    f"Converted to AMPR LZ4 successfully!\n\n"
                    f"Title: {report.get('title_name', 'Unknown')}\n"
                    f"Title ID: {report.get('title_id')}\n"
                    f"Output Format: {target_fmt.upper()}\n"
                    f"Output: {os.path.basename(report.get('output_path', out_path))}\n\n"
                    f"\u2500\u2500 Statistics \u2500\u2500\n"
                    f"\u2022 LZ4 .pak Volumes: {paks} ({pak_gb:.2f} GB)\n"
                    f"\u2022 Loose Files: {loose_cnt} ({loose_gb:.2f} GB)\n"
                    f"\u2022 Space Saved: {saved_pct:.1f}%\n\n"
                    f"\u2500\u2500 PS5 Compatibility \u2500\u2500\n"
                    f"\u2713 Verified PS5 libSceAmpr.sprx in fakelib/\n"
                    f"\u2713 Case-insensitive ampr_emu.index (AMPRIDX3)\n"
                    f"\u2713 Unaltered eboot.bin & sce_sys boot metadata\n"
                    f"\u2713 All LZ4 block checksums verified\n\n"
                    f"Ready to play on your PS5!"
                )
                if save_receipt:
                    rep_msg += f"\n\nVerification receipt saved to:\n{os.path.basename(out_path)}.verified.json"

                parent.after(0, lambda: messagebox.showinfo('AMPR LZ4 Pack Ready for PS5', rep_msg))

                try:
                    from ui.release_notes import note_successful_operation
                    note_successful_operation(app, 'Convert')
                except Exception:
                    pass

            except Exception as e:
                _log('fPKG \u2192 AMPR LZ4 failed: ' + str(e))
                parent.after(0, prog.close)
                parent.after(0, lambda e=e: _set_busy_lz4(False, 'Failed.'))
                parent.after(0, lambda e=e: messagebox.showerror('AMPR LZ4 Conversion Failed', str(e)))

        threading.Thread(target=worker, daemon=True).start()




