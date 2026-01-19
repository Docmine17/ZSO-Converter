#!/usr/bin/env python3
import sys
import os

# Force usage of xdg-desktop-portal if not already set, 
# this ensures native file choosers on KDE/Wayland/etc.
os.environ.setdefault("GTK_USE_PORTAL", "1")

import threading

import lz4.block
from struct import pack, unpack
from multiprocessing import Pool
from getopt import gnu_getopt, GetoptError



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
    print("ziso-python 2.1 by Virtuous Flame (GUI Edition by Gabriel)")
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
    class ZisoGUI(Adw.ApplicationWindow):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.set_title("ZISO Converter")
            self.set_default_size(800, 850)

            # Main Layout: Toolbar View
            toolbar_view = Adw.ToolbarView()
            self.set_content(toolbar_view)

            # Header Bar
            header = Adw.HeaderBar()
            toolbar_view.add_top_bar(header)

            # Add Split Button to Header
            self.split_btn = Adw.SplitButton(icon_name="document-new-symbolic")
            self.split_btn.set_tooltip_text("Adicionar arquivos ou pastas")
            self.split_btn.connect("clicked", self.on_add_clicked)
            header.pack_start(self.split_btn)

            # Dropdown Menu for SplitButton
            menu_add = Gio.Menu()
            menu_add.append("Adicionar Pasta...", "win.add-folder")
            self.split_btn.set_menu_model(menu_add)

            # Register Actions
            action_add_folder = Gio.SimpleAction.new("add-folder", None)
            action_add_folder.connect("activate", self.on_add_folder_action)
            self.add_action(action_add_folder)

            # Menu Button
            menu = Gio.Menu()
            menu.append("Limpar Lista", "app.clear")
            menu.append("Sobre", "app.about")
            
            menu_btn = Gtk.MenuButton()
            menu_btn.set_icon_name("open-menu-symbolic")
            menu_btn.set_menu_model(menu)
            header.pack_end(menu_btn)

            # Main Content Area (Vertical Box)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            toolbar_view.set_content(vbox)

            # 2. Main Content: Manual Clamp for wider content (User request)
            # Adw.PreferencesPage forces a narrow width, so we build our own structure.
            scroll_main = Gtk.ScrolledWindow()
            scroll_main.set_vexpand(True)
            vbox.append(scroll_main)

            clamp_main = Adw.Clamp()
            clamp_main.set_maximum_size(800)   # Wider content
            clamp_main.set_tightening_threshold(600)
            # Margins around the clamp content
            clamp_main.set_margin_top(10)
            clamp_main.set_margin_bottom(10)
            clamp_main.set_margin_start(10)
            clamp_main.set_margin_end(10)
            scroll_main.set_child(clamp_main)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
            clamp_main.set_child(content_box)

            # --- Group 1: File List ---
            # We use a PreferencesGroup to hold the list, ensuring it aligns perfectly with the controls.
            self.list_group = Adw.PreferencesGroup()
            # self.list_group.set_title("Arquivos") # Title removed per user request
            content_box.append(self.list_group)

            # 1. File List Setup
            self.store = Gtk.ListStore(str, str, str, float, str) # Name, Type, Status, Pct, Path
            
            self.tree = Gtk.TreeView(model=self.store)
            self.tree.set_vexpand(True)
            self.tree.set_enable_search(False)
            self.tree.add_css_class("data-table")
            self.tree.set_headers_visible(False)

            # Custom CSS for larger text
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(b"treeview { font-size: 16px; }")
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            # Columns
            self.add_column("Arquivo", 0, expand=True)
            self.add_column("Status", 2)
            
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(self.tree)
            scroll.set_min_content_height(350) # Taller list
            
            # Frame with .card style
            frame = Gtk.Frame()
            frame.add_css_class("card")
            frame.set_child(scroll)

            # Add Frame directly to the Group
            self.list_group.add(frame)


            # --- Group 2: Controls ---
            self.controls_group = Adw.PreferencesGroup()
            content_box.append(self.controls_group)
            
            # Format Selection Row
            row_fmt = Adw.ActionRow(title="Formato de Exportação")
            self.combo_format = Gtk.DropDown.new_from_strings(["Converter para ZSO", "Converter para ISO (Descomprimir)"])
            self.combo_format.set_valign(Gtk.Align.CENTER)
            row_fmt.add_suffix(self.combo_format)
            self.controls_group.add(row_fmt)

            # Compression Level Row
            row_level = Adw.ActionRow(title="Nível de Compressão", subtitle="Maior nível = menor tamanho, mas mais lento")
            adj = Gtk.Adjustment(value=9, lower=1, upper=12, step_increment=1)
            self.scale_level = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
            self.scale_level.set_draw_value(True)
            self.scale_level.set_digits(0)
            self.scale_level.set_hexpand(True)
            self.scale_level.set_size_request(150, -1)
            row_level.add_suffix(self.scale_level)
            self.controls_group.add(row_level)

            # Destination Folder Row
            self.row_folder = Adw.ActionRow(title="Salvar em", subtitle="Pasta de destino (Obrigatória)")
            self.btn_select_folder = Gtk.Button(icon_name="folder-open-symbolic")
            self.btn_select_folder.set_valign(Gtk.Align.CENTER)
            self.btn_select_folder.connect("clicked", self.on_select_dest_folder)
            
            self.row_folder.add_suffix(self.btn_select_folder)
            self.controls_group.add(self.row_folder)

            # 3. Convert Button (Clean layout without ActionBar)
            box_bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box_bottom.set_margin_bottom(20) # Add padding at bottom
            box_bottom.set_halign(Gtk.Align.CENTER)
            
            self.convert_btn = Gtk.Button(label="Converter")
            self.convert_btn.add_css_class("suggested-action")
            self.convert_btn.add_css_class("pill")
            self.convert_btn.set_size_request(200, 50) # Make it big and clickable
            self.convert_btn.set_sensitive(False)
            self.convert_btn.connect("clicked", self.on_convert_clicked)
            
            box_bottom.append(self.convert_btn)
            vbox.append(box_bottom)

            # Drop Target (Drag & Drop)
            drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
            drop_target.connect("drop", self.on_drop)
            self.add_controller(drop_target)

            # Internal State
            self.destination_folder = None
            self.processing = False

        def add_column(self, title, id, expand=False):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=id)
            col.set_expand(expand)
            self.tree.append_column(col)

        def on_drop(self, target, value, x, y):
            if isinstance(value, Gio.File):
                self.add_gio_file(value)
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
                title="Adicionar Arquivos",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
            )
            dialog.set_select_multiple(True)
            
            filter_iso = Gtk.FileFilter()
            filter_iso.set_name("PS2 Images (.iso, .zso)")
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
                title="Selecionar Pasta",
                transient_for=self,
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
                title="Selecionar Pasta de Destino",
                transient_for=self,
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
            for row in self.store:
                if row[4] == filepath:
                    return

            name = os.path.basename(filepath)
            
            self.store.append([name, os.path.splitext(filepath)[1].lower(), "Pendente", 0.0, filepath])
            self.update_ui_state()

        def update_ui_state(self):
            if self.processing:
                self.convert_btn.set_sensitive(False)
                self.controls_group.set_sensitive(False)
                return
            
            has_files = len(self.store) > 0
            has_dest = self.destination_folder is not None
            
            self.convert_btn.set_sensitive(has_files and has_dest)
            self.controls_group.set_sensitive(True)

        def on_convert_clicked(self, btn):
            if self.processing: return
            self.processing = True
            self.update_ui_state()
            
            target_is_zso = (self.combo_format.get_selected() == 0)
            target_fmt = "zso" if target_is_zso else "iso"
            level = int(self.scale_level.get_value())
            
            threading.Thread(target=self.process_queue, args=(target_fmt, level, self.destination_folder), daemon=True).start()

        def process_queue(self, target_fmt, level, dest_folder):
            for row in self.store:
                input_path = row[4]
                ext = row[1]
                
                if (target_fmt == "zso" and ext == ".zso") or (target_fmt == "iso" and ext == ".iso"):
                    GLib.idle_add(self.update_status, row.iter, "Ignorado")
                    continue

                input_filename = os.path.basename(input_path)
                out_name = os.path.splitext(input_filename)[0] + ("." + target_fmt)
                output_path = os.path.join(dest_folder, out_name)

                def progress_cb(block, total, write_pos=None):
                    pct = (block / total) * 100
                    GLib.idle_add(self.update_status, row.iter, f"{pct:.1f}%")

                GLib.idle_add(self.update_status, row.iter, "Processando...")
                try:
                    if target_fmt == "zso":
                        # User requested MP enabled by default in GUI
                        compress_zso(input_path, output_path, level, DEFAULT_BLOCK_SIZE, mp=True, progress_callback=progress_cb)
                    else:
                        decompress_zso(input_path, output_path, progress_callback=progress_cb)
                    GLib.idle_add(self.update_status, row.iter, "Concluído")
                except Exception as e:
                    GLib.idle_add(self.update_status, row.iter, "Erro")
                    print(e)
            
            GLib.idle_add(self.finish_processing)

        def update_status(self, iter, text):
            self.store.set_value(iter, 2, text)

        def finish_processing(self):
            self.processing = False
            self.update_ui_state()

    class ZisoApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="org.ziso.gui", flags=Gio.ApplicationFlags.FLAGS_NONE)

        def do_activate(self):
            win = self.props.active_window
            if not win:
                win = ZisoGUI(application=self)
            win.present()
        
        def do_startup(self):
            Adw.Application.do_startup(self)
            
            action_clear = Gio.SimpleAction.new("clear", None)
            action_clear.connect("activate", self.on_clear)
            self.add_action(action_clear)
            
            action_about = Gio.SimpleAction.new("about", None)
            action_about.connect("activate", self.on_about)
            self.add_action(action_about)

        def on_clear(self, action, param):
            win = self.props.active_window
            if win:
                win.store.clear()
                win.update_ui_state()

        def on_about(self, action, param):
            win = self.props.active_window
            Adw.AboutWindow(
                transient_for=win,
                application_name="ZISO Converter",
                application_icon="org.ziso.gui",
                version="2.2",
                developer_name="Virtuous Flame & Gabriel",
                comments="Modern GTK4/Adwaita GUI for ZISO",
                website="https://github.com/codestation/ziso"
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
        
        print(f"ziso-python 2.1 (CLI)")
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
