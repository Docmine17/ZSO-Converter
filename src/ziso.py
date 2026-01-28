#!/usr/bin/env python3
import sys
import os
import threading
import locale

# Copyright (c) 2011 by Virtuous Flame
# Based BOOSTER 1.01 CSO Compressor
# Adapted for codestation's ZSO format
# GUI Implementation (GTK4) by Gabriel
#
# GNU General Public Licence (GPL)
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 59 Temple
# Place, Suite 330, Boston, MA  02111-1307  USA
#

__author__ = "Virtuous Flame & Gabriel"
__license__ = "GPL"
__version__ = "1.0.2"

import lz4.block
from struct import pack, unpack
from multiprocessing import Pool
from getopt import gnu_getopt, GetoptError
import gettext

# Setup translation
APP_ID = "org.ziso.gui"
LOCALE_DIR = "/app/share/locale" if os.path.exists("/app") else os.path.join(os.path.dirname(__file__), "..", "locale")

try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    print("Warning: Failed to set locale to default.")

gettext.bindtextdomain(APP_ID, LOCALE_DIR)
gettext.textdomain(APP_ID)
_ = gettext.gettext



ZISO_MAGIC = 0x4F53495A
DEFAULT_ALIGN = 0
DEFAULT_BLOCK_SIZE = 0x800
COMPRESS_THREHOLD_DEFAULT = 95
DEFAULT_PADDING = br'X'

MP_DEFAULT = False
MP_NR = 1024 * 16


def lz4_compress(plain, level=9):
    mode = "high_compression" if level > 1 else "default"
    return lz4.block.compress(plain, mode=mode, compression=level, store_size=False)


def lz4_compress_mp(i):
    plain = i[0]
    level = i[1]
    mode = "high_compression" if level > 1 else "default"
    return lz4.block.compress(plain, mode=mode, compression=level, store_size=False)


def lz4_decompress(compressed, block_size):
    decompressed = None
    while True:
        try:
            decompressed = lz4.block.decompress(
                compressed, uncompressed_size=block_size)
            break
        except lz4.block.LZ4BlockError:
            compressed = compressed[:-1]
    return decompressed


def usage():
    print("ziso-python (GTK4) 1.0 by Virtuous Flame & Gabriel")
    print("Usage: ziso [-c level] [-m] [-t percent] [-h] infile outfile")
    print("  -c level: 1-12 compress ISO to ZSO, 1 for standard compression, >1 for high compression")
    print("              0 decompress ZSO to ISO")
    print("  -b size:  2048-8192, specify block size (2048 by default)")
    print("  -m Use multiprocessing acceleration for compressing")
    print("  -t percent Compression Threshold (1-100)")
    print("  -a align Padding alignment 0=small/slow 6=fast/large")
    print("  -p pad Padding byte")
    print("  -h this help")


def open_input_output(fname_in, fname_out):
    try:
        fin = open(fname_in, "rb")
    except IOError:
        raise IOError("Can't open %s" % (fname_in))

    try:
        fout = open(fname_out, "wb")
    except IOError:
        fin.close()
        raise IOError("Can't create %s" % (fname_out))

    return fin, fout


def seek_and_read(fin, offset, size):
    fin.seek(offset)
    return fin.read(size)


def read_zso_header(fin):
    # ZSO header has 0x18 bytes
    data = seek_and_read(fin, 0, 0x18)
    magic, header_size, total_bytes, block_size, ver, align = unpack(
        'IIQIbbxx', data)
    return magic, header_size, total_bytes, block_size, ver, align


def generate_zso_header(magic, header_size, total_bytes, block_size, ver, align):
    data = pack('IIQIbbxx', magic, header_size,
                total_bytes, block_size, ver, align)
    return data


def decompress_zso(fname_in, fname_out, progress_callback=None):
    fin, fout = open_input_output(fname_in, fname_out)
    try:
        magic, header_size, total_bytes, block_size, ver, align = read_zso_header(
            fin)

        if magic != ZISO_MAGIC or block_size == 0 or total_bytes == 0 or header_size != 24 or ver > 1:
            raise ValueError("ziso file format error")

        total_block = total_bytes // block_size
        index_buf = []

        for _ in range(total_block + 1):
            index_buf.append(unpack('I', fin.read(4))[0])

        block = 0
        percent_period = total_block/100
        percent_cnt = 0

        while block < total_block:
            percent_cnt += 1
            if progress_callback:
                progress_callback(block, total_block)
            elif percent_cnt >= percent_period and percent_period != 0:
                percent_cnt = 0
                print("decompress %d%%\r" %
                      (block / percent_period), file=sys.stderr, end='\r')

            index = index_buf[block]
            plain = index & 0x80000000
            index &= 0x7fffffff
            read_pos = index << (align)

            if plain:
                read_size = block_size
            else:
                index2 = index_buf[block+1] & 0x7fffffff
                # Have to read more bytes if align was set
                read_size = (index2-index) << (align)
                if block == total_block - 1:
                    read_size = total_bytes - read_pos

            zso_data = seek_and_read(fin, read_pos, read_size)

            if plain:
                dec_data = zso_data
            else:
                dec_data = lz4_decompress(zso_data, block_size)

            if (len(dec_data) != block_size):
                raise ValueError("Decompression error at block %d" % block)

            fout.write(dec_data)
            block += 1
    finally:
        fin.close()
        fout.close()


def set_align(fout, write_pos, align, padding_byte=DEFAULT_PADDING):
    if write_pos % (1 << align):
        align_len = (1 << align) - write_pos % (1 << align)
        fout.write(padding_byte * align_len)
        write_pos += align_len

    return write_pos


def compress_zso(fname_in, fname_out, level, bsize, mp=False, threshold=95, align_val=None, padding_byte=DEFAULT_PADDING, progress_callback=None):
    fin, fout = open_input_output(fname_in, fname_out)
    try:
        fin.seek(0, os.SEEK_END)
        total_bytes = fin.tell()
        fin.seek(0)

        magic, header_size, block_size, ver, align = ZISO_MAGIC, 0x18, bsize, 1, DEFAULT_ALIGN

        # We have to use alignment on any ZSO files which > 2GB, for MSB bit of index as the plain indicator
        # If we don't then the index can be larger than 2GB, which its plain indicator was improperly set
        if align_val is None:
            align = total_bytes // 2 ** 31
        else:
            align = align_val

        header = generate_zso_header(
            magic, header_size, total_bytes, block_size, ver, align)
        fout.write(header)

        total_block = total_bytes // block_size
        index_buf = [0 for i in range(total_block + 1)]

        fout.write(b"\x00\x00\x00\x00" * len(index_buf))

        write_pos = fout.tell()
        percent_period = total_block/100
        percent_cnt = 0

        pool = None
        if mp:
            pool = Pool()

        block = 0
        while block < total_block:
            if mp:
                percent_cnt += min(total_block - block, MP_NR)
            else:
                percent_cnt += 1

            if progress_callback:
                progress_callback(block, total_block, write_pos)
            elif percent_cnt >= percent_period and percent_period != 0:
                percent_cnt = 0
                if block == 0:
                    print("compress %3d%% avarage rate %3d%%\r" % (
                        block / percent_period, 0), file=sys.stderr, end='\r')
                else:
                    print("compress %3d%% avarage rate %3d%%\r" % (
                        block / percent_period, 100*write_pos/(block*block_size)), file=sys.stderr, end='\r')

            if mp:
                iso_data = [(fin.read(block_size), level)
                            for i in range(min(total_block - block, MP_NR))]
                zso_data_all = pool.map_async(
                    lz4_compress_mp, iso_data).get(9999999)

                for i, zso_data in enumerate(zso_data_all):
                    write_pos = set_align(fout, write_pos, align, padding_byte)
                    index_buf[block] = write_pos >> align

                    if 100 * len(zso_data) / len(iso_data[i][0]) >= min(threshold, 100):
                        zso_data = iso_data[i][0]
                        index_buf[block] |= 0x80000000  # Mark as plain
                    elif index_buf[block] & 0x80000000:
                        raise ValueError("Align error, you have to increase align by 1")

                    fout.write(zso_data)
                    write_pos += len(zso_data)
                    block += 1
            else:
                iso_data = fin.read(block_size)
                zso_data = lz4_compress(iso_data, level)

                write_pos = set_align(fout, write_pos, align, padding_byte)
                index_buf[block] = write_pos >> align

                if 100 * len(zso_data) / len(iso_data) >= threshold:
                    zso_data = iso_data
                    index_buf[block] |= 0x80000000  # Mark as plain
                elif index_buf[block] & 0x80000000:
                    raise ValueError("Align error, you have to increase align by 1")

                fout.write(zso_data)
                write_pos += len(zso_data)
                block += 1

        # Last position (total size)
        index_buf[block] = write_pos >> align

        # Update index block
        fout.seek(len(header))
        for i in index_buf:
            idx = pack('I', i)
            fout.write(idx)

    finally:
        if 'fin' in locals(): fin.close()
        if 'fout' in locals(): fout.close()
# =============================================================================
# GUI Implementation (GTK4 + LibAdwaita)
# =============================================================================

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, GLib, Gio, GObject, Gdk
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

if HAS_GUI:
    class ZisoGUI:
        def __init__(self, app):
            self.app = app
            
            # Locate UI file
            ui_path = os.path.join(os.path.dirname(__file__), "window.ui")
            if not os.path.exists(ui_path):
                # Fallback for flatpak/installed location if needed
                if os.path.exists("/app/share/ziso/window.ui"):
                    ui_path = "/app/share/ziso/window.ui"
            
            # Load UI
            self.builder = Gtk.Builder()
            try:
                self.builder.add_from_file(ui_path)
            except Exception as e:
                print(f"Error loading UI: {e}")
                sys.exit(1)

            # Get Objects
            self.window = self.builder.get_object("window")
            self.window.set_application(app)
            
            self.window.set_application(app)
            
            self.file_list = self.builder.get_object("file_list")
            self.stack_view = self.builder.get_object("stack_view") # GtkStack
            
            self.split_btn = self.builder.get_object("split_btn")
            self.controls_group = self.builder.get_object("controls_group")
            # AdwToggleGroup
            self.toggle_format = self.builder.get_object("toggle_format")
            self.scale_level = self.builder.get_object("scale_level")
            self.row_folder = self.builder.get_object("row_folder")
            self.btn_select_folder = self.builder.get_object("btn_select_folder")
            self.convert_btn = self.builder.get_object("convert_btn")

            # Connect Signals
            self.split_btn.connect("clicked", self.on_add_clicked)
            self.btn_select_folder.connect("clicked", self.on_select_dest_folder)
            self.convert_btn.connect("clicked", self.on_convert_clicked)

            # Window Actions
            action_add_folder = Gio.SimpleAction.new("add-folder", None)
            action_add_folder.connect("activate", self.on_add_folder_action)
            self.window.add_action(action_add_folder)

            # Drop Target (Drag & Drop)
            drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop_target.connect("drop", self.on_drop)
            self.window.add_controller(drop_target)

            # Internal State
            self.destination_folder = None
            self.processing = False

        def present(self):
            self.window.present()

        def on_drop(self, target, value, x, y):
            if isinstance(value, Gdk.FileList):
                files = value.get_files()
                for f in files:
                    self.add_gio_file(f)
                return True
            return False

        def add_gio_file(self, gfile):
            try:
                info = gfile.query_info("standard::name,standard::type", Gio.FileQueryInfoFlags.NONE, None)
                if info.get_file_type() == Gio.FileType.DIRECTORY:
                    # Recursive add
                    enumerator = gfile.enumerate_children("standard::name,standard::type", Gio.FileQueryInfoFlags.NONE, None)
                    while True:
                        file_info = enumerator.next_file(None)
                        if file_info is None:
                            break
                        child = enumerator.get_child(file_info)
                        self.add_gio_file(child)
                else:
                    path = gfile.get_path()
                    if path and path.lower().endswith((".iso", ".zso")):
                        self.add_file_to_list(path)
            except Exception as e:
                print(f"Error reading file: {e}")

        def on_add_clicked(self, btn):
            dialog = Gtk.FileChooserNative(
                title=_("Add Files"),
                transient_for=self.window,
                action=Gtk.FileChooserAction.OPEN,
            )
            dialog.set_select_multiple(True)
            
            filter_iso = Gtk.FileFilter()
            filter_iso.set_name(_("PS2 Images (.iso, .zso)"))
            filter_iso.add_pattern("*.iso")
            filter_iso.add_pattern("*.ISO")
            filter_iso.add_pattern("*.zso")
            filter_iso.add_pattern("*.ZSO")
            dialog.add_filter(filter_iso)
            
            def on_response(d, response):
                if response == Gtk.ResponseType.ACCEPT:
                    files = d.get_files()
                    for f in files:
                        self.add_gio_file(f)
                d.destroy()

            dialog.connect("response", on_response)
            dialog.show()

        def on_add_folder_action(self, action, param):
            dialog = Gtk.FileChooserNative(
                title=_("Select Folder"),
                transient_for=self.window,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            
            def on_response(d, response):
                if response == Gtk.ResponseType.ACCEPT:
                    f = d.get_file()
                    self.add_gio_file(f)
                d.destroy()

            dialog.connect("response", on_response)
            dialog.show()

        def on_select_dest_folder(self, btn):
            dialog = Gtk.FileChooserNative(
                title=_("Select Destination Folder"),
                transient_for=self.window,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            
            def on_response(d, response):
                if response == Gtk.ResponseType.ACCEPT:
                    f = d.get_file()
                    path = f.get_path()
                    if path:
                        self.destination_folder = path
                        self.row_folder.set_subtitle(path)
                        self.update_ui_state()
                d.destroy()

            dialog.connect("response", on_response)
            dialog.show()

        def add_file_to_list(self, filepath):
            # Duplicate check
            child = self.file_list.get_first_child()
            while child:
                if getattr(child, "filepath", None) == filepath:
                    return
                child = child.get_next_sibling()

            name = os.path.basename(filepath)
            
            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(_("Pending"))
            row.filepath = filepath
            row.file_ext = os.path.splitext(filepath)[1].lower()
            
            # Remove button
            btn_remove = Gtk.Button(icon_name="user-trash-symbolic")
            btn_remove.add_css_class("flat")
            btn_remove.set_valign(Gtk.Align.CENTER)
            btn_remove.connect("clicked", lambda b: self.remove_row(row))
            row.add_suffix(btn_remove)
            row.btn_remove = btn_remove # keep ref
            
            # Status Icon (hidden initially or showing pending state if desired, but let's just use subtitle for pending)
            # We will add the custom checkmark later when done
            
            self.file_list.append(row)
            self.update_ui_state()

        def remove_row(self, row):
            if self.processing: return
            self.file_list.remove(row)
            self.update_ui_state()

        def update_ui_state(self):
            # Only block global controls, not the list itself (so it remains scrollable)
            if self.processing:
                self.convert_btn.set_sensitive(False)
                self.controls_group.set_sensitive(False)
                # self.file_list.set_sensitive(False) # Keep list interactive
            else:
                self.controls_group.set_sensitive(True)
                
            count = 0
            child = self.file_list.get_first_child()
            while child:
                count += 1
                # Disable remove button if processing
                if hasattr(child, 'btn_remove'):
                    child.btn_remove.set_sensitive(not self.processing)
                    
                child = child.get_next_sibling()

            # Enable file_list always (for scrolling)
            self.file_list.set_sensitive(True)

            has_files = count > 0
            
            # Toggle Stack View
            if count == 0:
                self.stack_view.set_visible_child_name("empty")
            else:
                self.stack_view.set_visible_child_name("list")
            
            has_dest = self.destination_folder is not None
            
            if not self.processing:
                self.convert_btn.set_sensitive(has_files and has_dest)

        def process_queue(self, target_fmt, level, dest_folder):
            
            icon_path = os.path.join(os.path.dirname(__file__), "..", "data", "check-round-outline2-symbolic.svg")
            
            # Iterate using a list copy of children to avoid issues (though we aren't modifying structure)
            # But we need to iterate in thread, safe way is to get items first? 
            # No, we cannot touch widgets in thread. We need to iterate in main thread or similar?
            # Actually, `store` was thread-safe-ish or we were just lucky. 
            # GTK4 widgets are NOT thread safe. We must NOT access them in this thread.
            # We need to gather data first or implement a safer queue mechanism.
            
            # However, for simplicity and matching previous structure:
            # We will use GLib.idle_add to get the list of files to process *before* starting heavy work,
            # OR we can just assume `process_queue` is running in a thread and we shouldn't touch widgets.
            
            # Refactor: Get all file data BEFORE thread start?
            # The previous code iterated `self.store` in the thread. That was technically unsafe but worked sometimes.
            # `self.store` is a GtkTreeModel. 
            # We MUST NOT iterate Gtk widgets in a background thread.
            
            # FIX: We passed `process_queue` to Thread. 
            # We should gather tasks first.
            pass # Replaces below
        
        # New helper to gather tasks safely on main thread
        def get_tasks(self):
            tasks = []
            child = self.file_list.get_first_child()
            while child:
                tasks.append({
                    "row": child, 
                    "filepath": child.filepath, 
                    "ext": child.file_ext
                })
                child = child.get_next_sibling()
            return tasks

        def process_queue(self, target_fmt, level, dest_folder):
             # This is running in a thread. 
             # We can't access self.file_list children properties directly here safely if they were widgets.
             # BUT `self.store` was data. `AdwActionRow` is a widget.
             # We should rely on a pre-calculated list of tasks passed to this method, 
             # OR use GLib.invoke to get them (but that blocks).
             
             # Better approach: The `on_convert_clicked` should generate the list of tasks.
             pass 

        # Redefining on_convert_clicked to fix the thread safety issue introduced by moving to Widgets
        def on_convert_clicked(self, btn):
            if self.processing: return
            self.processing = True
            self.update_ui_state()
            
            active_name = self.toggle_format.get_active_name()
            target_fmt = "zso" if active_name == "zso" else "iso"
            level = int(self.scale_level.get_value())
            
            # Gather tasks from widgets (Main Thread - Safe)
            tasks = self.get_tasks()
            
            threading.Thread(target=self.process_queue_safe, args=(tasks, target_fmt, level, self.destination_folder), daemon=True).start()

        def process_queue_safe(self, tasks, target_fmt, level, dest_folder):
            for task in tasks:
                row = task["row"]
                input_path = task["filepath"]
                ext = task["ext"]
                
                if (target_fmt == "zso" and ext == ".zso") or (target_fmt == "iso" and ext == ".iso"):
                    GLib.idle_add(self.update_row_status, row, _("Ignored"), "warning")
                    continue

                input_filename = os.path.basename(input_path)
                out_name = os.path.splitext(input_filename)[0] + ("." + target_fmt)
                output_path = os.path.join(dest_folder, out_name)

                def progress_cb(block, total, write_pos=None):
                    pct = (block / total) * 100
                    GLib.idle_add(self.update_row_progress, row, f"{pct:.0f}%")

                GLib.idle_add(self.update_row_starting, row)
                try:
                    if target_fmt == "zso":
                        compress_zso(input_path, output_path, level, DEFAULT_BLOCK_SIZE, mp=True, progress_callback=progress_cb)
                    else:
                        decompress_zso(input_path, output_path, progress_callback=progress_cb)
                    GLib.idle_add(self.update_row_status, row, _("Completed"), "success")
                except Exception as e:
                    GLib.idle_add(self.update_row_status, row, _("Error"), "error")
                    print(e)
            
            GLib.idle_add(self.finish_processing)

        def update_row_starting(self, row):
            # Set subtitle to initial percentage
            row.set_subtitle("0%")
            
        def update_row_progress(self, row, text):
            row.set_subtitle(text)

        def update_row_status(self, row, text, css_class=None):
            row.set_subtitle(text)
            
            # Reset classes
            row.remove_css_class("success")
            row.remove_css_class("warning")
            row.remove_css_class("error")
            
            if css_class:
                row.add_css_class(css_class)
            
            if css_class:
                row.add_css_class(css_class)

        def finish_processing(self):
            self.processing = False
            self.update_ui_state()

    class ZisoApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="org.ziso.gui", flags=Gio.ApplicationFlags.FLAGS_NONE)
            self.gui = None

        def do_activate(self):
            if not self.gui:
                self.gui = ZisoGUI(self)
            self.gui.present()
        
        def do_startup(self):
            Adw.Application.do_startup(self)
            
            action_clear = Gio.SimpleAction.new("clear", None)
            action_clear.connect("activate", self.on_clear)
            self.add_action(action_clear)
            
            action_about = Gio.SimpleAction.new("about", None)
            action_about.connect("activate", self.on_about)
            self.add_action(action_about)

        def on_clear(self, action, param):
            if self.gui:
                if self.gui.processing:
                    return # Block clear if processing
                child = self.gui.file_list.get_first_child()
                while child:
                    next_child = child.get_next_sibling()
                    self.gui.file_list.remove(child)
                    child = next_child
                self.gui.update_ui_state()

        def on_about(self, action, param):
            win = self.get_active_window()
            Adw.AboutWindow(
                transient_for=win,
                application_name=_("ZSO Converter"),
                application_icon="org.ziso.gui",
                version=__version__,
                developer_name="Docmine17",
                comments=_("Modern GTK4/Adwaita GUI for ZSO"),
                website="https://github.com/Docmine17/ZSO-Converter/",
                issue_url="https://github.com/Docmine17/ZSO-Converter/issues"
            ).present()


def parse_args():
    try:
        optlist, args = gnu_getopt(sys.argv[1:], "c:b:mt:a:p:h")
    except GetoptError as err:
        print(str(err))
        usage()
        sys.exit(-1)

    level = None
    bsize = DEFAULT_BLOCK_SIZE
    mp = MP_DEFAULT
    threshold = COMPRESS_THREHOLD_DEFAULT
    align = None
    padding = DEFAULT_PADDING

    for o, a in optlist:
        if o == '-c':
            level = int(a)
        elif o == '-b':
            bsize = int(a)
        elif o == '-m':
            mp = True
        elif o == '-t':
            threshold = min(int(a), 100)
        elif o == '-a':
            align = int(a)
        elif o == '-p':
            padding = bytes(a[0], encoding='utf8')
        elif o == '-h':
            usage()
            sys.exit(0)

    if level is None:
        print("Error: Nível de compressão (-c) é obrigatório no modo CLI.")
        usage()
        sys.exit(-1)

    try:
        fname_in = args[0]
        fname_out = args[1]
    except IndexError:
        print("Error: Você deve especificar os arquivos de entrada e saída.")
        usage()
        sys.exit(-1)

    if bsize % 2048 != 0:
        print("Error: Tamanho do bloco inválido. Deve ser múltiplo de 2048.")
        sys.exit(-1)
    
    return level, bsize, fname_in, fname_out, mp, threshold, align, padding


def main():
    if len(sys.argv) > 1:
        # CLI Mode
        level, bsize, fname_in, fname_out, mp, threshold, align, padding = parse_args()
        
        print(f"ziso-python 1.0 (CLI)")
        if level == 0:
            print(f"Decompressing {fname_in} to {fname_out}...")
            decompress_zso(fname_in, fname_out)
        else:
            print(f"Compressing {fname_in} to {fname_out} (level {level})...")
            compress_zso(fname_in, fname_out, level, bsize, mp, threshold, align, padding)
        print("\nDone.")

    elif HAS_GUI:
        # GUI Mode
        app = ZisoApp()
        sys.exit(app.run(sys.argv))
    else:
        usage()
        print("\nError: No GUI backend available (Gtk 4.0/Adw 1) and no CLI arguments provided.")
        sys.exit(1)

if __name__ == "__main__":
    main()
