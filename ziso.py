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

# Optional GTK imports
try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib, Gdk, Pango
    HAS_GTK = True
except (ImportError, ValueError):
    HAS_GTK = False

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
        fin.close()
        fout.close()


class ZisoGUI(Gtk.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(title="ZISO Converter", **kwargs)
        self.set_default_size(600, 450)

        # HeaderBar
        hb = Gtk.HeaderBar(show_close_button=True)
        hb.set_title("ZISO Converter")
        hb.set_subtitle("PS2 ISO/ZSO Compressor")
        self.set_titlebar(hb)

        # Add buttons
        box_add = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box_add.get_style_context().add_class("linked")
        
        # Add file button
        add_file_btn = Gtk.Button()
        add_file_btn.add(Gtk.Image.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON))
        add_file_btn.set_tooltip_text("Adicionar Arquivos")
        add_file_btn.connect("clicked", self.on_add_clicked)
        box_add.pack_start(add_file_btn, False, False, 0)
        
        # Add folder button
        add_folder_btn = Gtk.Button()
        add_folder_btn.add(Gtk.Image.new_from_icon_name("folder-new-symbolic", Gtk.IconSize.BUTTON))
        add_folder_btn.set_tooltip_text("Adicionar Pasta")
        add_folder_btn.connect("clicked", self.on_add_folder_clicked)
        box_add.pack_start(add_folder_btn, False, False, 0)
        
        hb.pack_start(box_add)

        # Hamburger menu
        menu_btn = Gtk.MenuButton()
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        
        menu = Gtk.Menu()
        clear_item = Gtk.MenuItem(label="Limpar Lista")
        clear_item.connect("activate", self.on_clear_clicked)
        menu.append(clear_item)
        
        about_item = Gtk.MenuItem(label="Sobre")
        about_item.connect("activate", self.on_about_clicked)
        menu.append(about_item)
        
        menu.show_all()
        menu_btn.set_popup(menu)
        hb.pack_end(menu_btn)

        # Main layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(10)
        vbox.set_margin_end(10)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)
        self.add(vbox)

        # File list (TreeView)
        self.store = Gtk.ListStore(str, str, str, str, float, str) # Filename, Size, Type, Progress_str, Progress_val, Output_path
        self.tree = Gtk.TreeView(model=self.store)
        
        renderer_text = Gtk.CellRendererText()
        column_file = Gtk.TreeViewColumn("Arquivo", renderer_text, text=0)
        column_file.set_expand(True)
        self.tree.append_column(column_file)
        
        column_size = Gtk.TreeViewColumn("Tamanho", renderer_text, text=1)
        self.tree.append_column(column_size)
        
        renderer_progress = Gtk.CellRendererProgress()
        column_progress = Gtk.TreeViewColumn("Status", renderer_progress, value=4, text=3)
        column_progress.set_min_width(120)
        self.tree.append_column(column_progress)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.tree)
        vbox.pack_start(scroll, True, True, 0)

        # Format selector
        hbox_fmt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(hbox_fmt, False, False, 0)
        
        hbox_fmt.pack_start(Gtk.Label(label="Formato de Exportação:"), False, False, 0)
        self.combo_format = Gtk.ComboBoxText()
        self.combo_format.append("zso", "Converter para ZSO")
        self.combo_format.append("iso", "Converter para ISO (Descomprimir)")
        self.combo_format.set_active(0)
        hbox_fmt.pack_start(self.combo_format, True, True, 0)

        # Output folder
        hbox_out = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(hbox_out, False, False, 0)
        
        hbox_out.pack_start(Gtk.Label(label="Salvar em:"), False, False, 0)
        
        # Replacement for Gtk.FileChooserButton to ensure Portal usage
        self.btn_select_folder = Gtk.Button(label="Selecionar Pasta...")
        image = Gtk.Image.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
        self.btn_select_folder.set_image(image)
        self.btn_select_folder.connect("clicked", self.on_select_dest_folder)
        hbox_out.pack_start(self.btn_select_folder, False, False, 0)
        
        self.lbl_dest_folder = Gtk.Label(label="<Nenhuma pasta selecionada>")
        self.lbl_dest_folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_dest_folder.set_alignment(0, 0.5)
        # Style the label to look like a placeholder or path
        hbox_out.pack_start(self.lbl_dest_folder, True, True, 0)

        # Dest folder variable storage
        self.destination_folder = None

        # Compression level
        hbox_level = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(hbox_level, False, False, 0)
        
        hbox_level.pack_start(Gtk.Label(label="Nível de Compressão (1-12):"), False, False, 0)
        adj = Gtk.Adjustment(value=9, lower=1, upper=12, step_increment=1)
        self.scale_level = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.scale_level.set_digits(0)
        self.scale_level.set_value_pos(Gtk.PositionType.RIGHT)
        hbox_level.pack_start(self.scale_level, True, True, 0)

        # Convert button
        self.convert_btn = Gtk.Button(label="Converter")
        self.convert_btn.set_sensitive(False)
        self.convert_btn.connect("clicked", self.on_convert_clicked)
        # Style the button a bit
        context = self.convert_btn.get_style_context()
        context.add_class("suggested-action")
        vbox.pack_start(self.convert_btn, False, False, 0)

        self.processing = False

        # Enable Drag & Drop
        # We enable it on the window AND the treeview to cover all bases
        TARGET_TYPE_URI_LIST = 80
        dnd_list = [Gtk.TargetEntry.new("text/uri-list", 0, TARGET_TYPE_URI_LIST)]
        
        self.drag_dest_set(
            Gtk.DestDefaults.ALL,
            dnd_list,
            Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        )
        
        self.connect("drag-data-received", self.on_drag_data_received)
        
        # Also setup treeview as destination just in case
        self.tree.drag_dest_set(
            Gtk.DestDefaults.ALL,
            dnd_list,
            Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        )
        self.tree.connect("drag-data-received", self.on_drag_data_received)

    def on_drag_data_received(self, widget, drag_context, x, y, data, info, time):
        if info == 80: # TARGET_TYPE_URI_LIST
            uris = data.get_uris()
            if uris:
                for uri in uris:
                    try:
                        # GLib.filename_from_uri returns (filename, hostname)
                        # We only want the filename
                        path = GLib.filename_from_uri(uri)[0]
                        self.add_path(path)
                    except Exception as e:
                        print(f"Error parsing URI {uri}: {e}")
                Gtk.drag_finish(drag_context, True, False, time)
                return
        
        Gtk.drag_finish(drag_context, False, False, time)

    def update_ui_state(self, widget=None):
        if self.processing:
            self.convert_btn.set_sensitive(False)
            self.tree.set_sensitive(False)
            self.btn_select_folder.set_sensitive(False)
            return

        has_files = len(self.store) > 0
        has_folder = self.destination_folder is not None
        
        self.convert_btn.set_sensitive(has_files and has_folder)
        self.tree.set_sensitive(True)
        self.btn_select_folder.set_sensitive(True)

    def on_select_dest_folder(self, btn):
        dialog = Gtk.FileChooserNative(
            title="Selecionar Pasta de Destino",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_filename()
            if folder:
                self.destination_folder = folder
                self.lbl_dest_folder.set_text(folder)
                self.btn_select_folder.set_label("Alterar...")
                self.update_ui_state()
        dialog.destroy()

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
        
        filter_all = Gtk.FileFilter()
        filter_all.set_name("Todos os arquivos")
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            filenames = dialog.get_filenames()
            for f in filenames:
                self.add_path(f)
        dialog.destroy()

    def on_add_folder_clicked(self, btn):
        dialog = Gtk.FileChooserNative(
            title="Selecionar Pasta",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_filename()
            self.add_path(folder)
        dialog.destroy()

    def add_path(self, path):
        if os.path.isfile(path):
            self.add_file_to_list(path)
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for f in files:
                    if f.lower().endswith((".iso", ".zso")):
                        self.add_file_to_list(os.path.join(root, f))

    def add_file_to_list(self, filepath):
        # Check for duplicates
        for row in self.store:
            if row[5] == filepath:
                return

        name = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        size_str = self.format_size(size)
        ext = os.path.splitext(filepath)[1].lower()
        
        # Determine likely output (default same folder)
        if ext == ".iso":
            out_path = filepath.rsplit('.', 1)[0] + ".zso"
        else:
            out_path = filepath.rsplit('.', 1)[0] + ".iso"
            
        self.store.append([name, size_str, ext, "Pendente", 0.0, filepath])
        self.update_ui_state()

    def format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return "%3.1f %s" % (size, unit)
            size /= 1024.0
        return "%3.1f TB" % size

    def on_clear_clicked(self, item):
        self.store.clear()
        self.update_ui_state()

    def on_about_clicked(self, item):
        about = Gtk.AboutDialog(transient_for=self)
        about.set_program_name("ZISO Converter")
        about.set_version("2.1")
        about.set_authors(["Virtuous Flame (Original)", "Gabriel (GUI Edition)"])
        about.set_comments("Conversor de ISO de PS2 para o formato comprimido ZSO.")
        about.set_website("https://github.com/codestation/ziso")
        about.run()
        about.destroy()

    def on_convert_clicked(self, btn):
        if self.processing:
            return
        
        self.processing = True
        self.update_ui_state()
        
        target_fmt = self.combo_format.get_active_id()
        level = int(self.scale_level.get_value())
        dest_folder = self.destination_folder
        
        threading.Thread(target=self.process_queue, args=(target_fmt, level, dest_folder), daemon=True).start()

    def process_queue(self, target_fmt, level, dest_folder):
        for row in self.store:
            # row: [name, size, ext, progress_str, progress_val, full_path]
            input_path = row[5]
            ext = row[2]
            
            # Validation: Skip invalid conversions
            if target_fmt == "zso" and ext == ".zso":
                GLib.idle_add(self.update_row_status, row.iter, "Ignorado (Já é ZSO)", 0)
                continue
            if target_fmt == "iso" and ext == ".iso":
                GLib.idle_add(self.update_row_status, row.iter, "Ignorado (Já é ISO)", 0)
                continue

            # Determine output filename
            input_filename = os.path.basename(input_path)
            input_basename = os.path.splitext(input_filename)[0]
            
            if target_fmt == "zso":
                output_filename = input_basename + ".zso"
            else:
                output_filename = input_basename + ".iso"

            if dest_folder:
                output_path = os.path.join(dest_folder, output_filename)
            else:
                # Fallback to same folder as input
                output_path = os.path.join(os.path.dirname(input_path), output_filename)

            def update_progress(block, total, write_pos=None):
                percent = (block / total) * 100
                GLib.idle_add(self.update_row_status, row.iter, f"{percent:.1f}%", percent)

            GLib.idle_add(self.update_row_status, row.iter, "Processando...", 0)
            
            try:
                if target_fmt == "zso":
                    compress_zso(input_path, output_path, level, DEFAULT_BLOCK_SIZE, progress_callback=update_progress)
                else:
                    decompress_zso(input_path, output_path, progress_callback=update_progress)
                
                GLib.idle_add(self.update_row_status, row.iter, "Concluído", 100)
            except Exception as e:
                GLib.idle_add(self.update_row_status, row.iter, f"Erro: {str(e)}", 0)

        GLib.idle_add(self.finish_processing)
    
    def update_row_status(self, iter, status_str, status_val):
        self.store.set_value(iter, 3, status_str)
        self.store.set_value(iter, 4, status_val)

    def finish_processing(self):
        self.processing = False
        self.convert_btn.set_sensitive(True)
        self.tree.set_sensitive(True)
        self.btn_select_folder.set_sensitive(True)


def main():
    if not HAS_GTK or len(sys.argv) > 1:
        # CLI Mode
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
            if not HAS_GTK:
                print("Error: Nível de compressão (-c) é obrigatório no modo CLI.")
                usage()
                sys.exit(-1)
            else:
                # No args provided, but GTK is available, launch GUI
                app = Gtk.Application(application_id="org.ziso.gui")
                app.connect("activate", lambda a: ZisoGUI(application=a).show_all())
                app.run(sys.argv)
                return

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

        print(f"ziso-python 2.0")
        if level == 0:
            print(f"Decompressing {fname_in} to {fname_out}...")
            decompress_zso(fname_in, fname_out)
        else:
            print(f"Compressing {fname_in} to {fname_out} (level {level})...")
            compress_zso(fname_in, fname_out, level, bsize, mp, threshold, align, padding)
        print("\nDone.")

    else:
        # GUI Mode (no args provided)
        app = Gtk.Application(application_id="org.ziso.gui")
        app.connect("activate", lambda a: ZisoGUI(application=a).show_all())
        app.run(sys.argv)


if __name__ == "__main__":
    main()
